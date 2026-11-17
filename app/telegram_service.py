import asyncio, re
from datetime import datetime, timezone
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError
from sqlalchemy.exc import IntegrityError
from .config import settings
from .db import SessionLocal
from .models import TgAccount, Customer, Message
from .security import encrypt_session, decrypt_session

_clients: dict[int, TelegramClient] = {}
_pending: dict[str, dict] = {}


def mask_phone(phone: str) -> str:
    digits = re.sub(r'\D','',phone)
    return ('+' + digits[:2] + '***' + digits[-3:]) if len(digits) > 5 else '***'

async def begin_login(phone: str):
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        raise RuntimeError('Set TELEGRAM_API_ID and TELEGRAM_API_HASH in Railway Variables')
    client = TelegramClient(StringSession(), int(settings.telegram_api_id), settings.telegram_api_hash)
    await client.connect()
    sent = await client.send_code_request(phone)
    key = sent.phone_code_hash
    _pending[key] = {'client': client, 'phone': phone, 'phone_code_hash': key, 'stage': 'code'}
    return key

async def finish_login(code_hash: str, code: str = '', password: str | None = None):
    item = _pending.get(code_hash)
    if not item:
        raise ValueError('Telegram verification session expired or was lost. Click “发送验证码” again and use the newest code.')
    client = item['client']
    try:
        # Telegram requires the one-time code first. Only after Telegram raises
        # SessionPasswordNeededError may the 2FA password be submitted.
        if item.get('stage') == 'password':
            if not password:
                return {'needs_password': True}
            await client.sign_in(password=password)
        else:
            await client.sign_in(
                phone=item['phone'],
                code=code,
                phone_code_hash=item['phone_code_hash']
            )
    except SessionPasswordNeededError:
        item['stage'] = 'password'
        return {'needs_password': True, 'stage': 'password'}
    if not await client.is_user_authorized():
        raise ValueError('Telegram authorization did not complete. Check the code and try again.')
    me = await client.get_me()
    session = client.session.save()
    db = SessionLocal()
    try:
        account = TgAccount(
            label=(me.first_name or 'Telegram') + (f' {me.last_name}' if me.last_name else ''),
            phone_masked=mask_phone(item['phone']),
            session_ciphertext=encrypt_session(session),
            status='connected'
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        _clients[account.id] = client
        _pending.pop(code_hash, None)
        attach_handlers(account.id, client)
        asyncio.create_task(sync_dialogs(account.id, client))
        return {'account_id': account.id, 'label': account.label, 'phone_masked': account.phone_masked}
    except Exception:
        db.rollback()
        await client.disconnect()
        raise
    finally:
        db.close()


def attach_handlers(account_id: int, client: TelegramClient):
    @client.on(events.NewMessage)
    async def on_message(event):
        try:
            sender = await event.get_sender()
            if getattr(sender, 'bot', False): return
            peer_id = str(event.chat_id)
            db = SessionLocal()
            try:
                customer = db.query(Customer).filter_by(account_id=account_id, telegram_peer_id=peer_id).first()
                if not customer:
                    name = ' '.join(filter(None,[getattr(sender,'first_name',''),getattr(sender,'last_name','')]))
                    customer = Customer(account_id=account_id, telegram_peer_id=peer_id, telegram_access_hash=str(getattr(sender,'access_hash','') or ''), real_username=getattr(sender,'username','') or '', real_name=name, phone=getattr(sender,'phone','') or '', alias=f'Customer-{peer_id[-5:]}')
                    db.add(customer); db.commit(); db.refresh(customer)
                mid = str(event.id)
                if not db.query(Message).filter_by(customer_id=customer.id, telegram_message_id=mid).first():
                    db.add(Message(customer_id=customer.id, telegram_message_id=mid, direction='in', body=event.raw_text or '', sent_at=event.date or datetime.now(timezone.utc), sender_label='Customer'))
                    db.commit()
            finally: db.close()
        except Exception as e: print('telegram incoming handler:', repr(e))

async def sync_dialogs(account_id: int, client: TelegramClient):
    db = SessionLocal()
    try:
        dialogs = await client.get_dialogs(limit=None)
        for dialog in dialogs:
            entity = dialog.entity
            if getattr(entity, 'bot', False) or not getattr(entity, 'id', None): continue
            peer_id = str(dialog.id)
            name = ' '.join(filter(None,[getattr(entity,'first_name',''),getattr(entity,'last_name','')])) or getattr(entity,'title','') or 'Telegram user'
            customer = db.query(Customer).filter_by(account_id=account_id, telegram_peer_id=peer_id).first()
            if not customer:
                customer = Customer(account_id=account_id, telegram_peer_id=peer_id, telegram_access_hash=str(getattr(entity,'access_hash','') or ''), real_username=getattr(entity,'username','') or '', real_name=name, phone=getattr(entity,'phone','') or '', alias=f'Customer-{peer_id[-5:]}')
                db.add(customer); db.commit(); db.refresh(customer)
            async for msg in client.iter_messages(entity, limit=1000, reverse=True):
                if not msg or not msg.id: continue
                if db.query(Message).filter_by(customer_id=customer.id, telegram_message_id=str(msg.id)).first(): continue
                db.add(Message(customer_id=customer.id, telegram_message_id=str(msg.id), direction='out' if msg.out else 'in', body=msg.message or '', sent_at=msg.date or datetime.now(timezone.utc), sender_label='Agent' if msg.out else 'Customer'))
                try: db.commit()
                except IntegrityError: db.rollback()
    except Exception as e: print('dialog sync:', repr(e))
    finally: db.close()

async def restore_clients():
    db = SessionLocal()
    rows = db.query(TgAccount).filter(TgAccount.status == 'connected').all()
    for row in rows:
        try:
            client = TelegramClient(StringSession(decrypt_session(row.session_ciphertext)), int(settings.telegram_api_id), settings.telegram_api_hash)
            await client.connect()
            if not await client.is_user_authorized(): row.status = 'reauthorization_required'; db.commit(); await client.disconnect(); continue
            _clients[row.id] = client
            attach_handlers(row.id, client)
            asyncio.create_task(sync_dialogs(row.id, client))
        except Exception as e:
            row.status = 'error'; db.commit(); print('restore account:', row.id, repr(e))
    db.close()

async def send_message(account_id: int, customer: Customer, text: str):
    client = _clients.get(account_id)
    if not client: raise RuntimeError('Telegram account is offline; reconnect from admin panel')
    peer = await client.get_input_entity(int(customer.telegram_peer_id))
    sent = await client.send_message(peer, text)
    return sent
