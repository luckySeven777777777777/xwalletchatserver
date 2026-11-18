import asyncio, re, os, mimetypes
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


def upload_root():
    path = os.path.abspath(settings.upload_dir)
    os.makedirs(path, exist_ok=True)
    return path

def customer_label(entity):
    name = ' '.join(filter(None, [getattr(entity, 'first_name', ''), getattr(entity, 'last_name', '')])).strip()
    username = getattr(entity, 'username', '') or ''
    phone = getattr(entity, 'phone', '') or ''
    return name or (('@' + username) if username else '') or phone or getattr(entity, 'title', '') or 'Telegram User'

async def save_avatar(account_id: int, customer: Customer, entity):
    try:
        path = os.path.join(upload_root(), f"avatar_{account_id}_{customer.telegram_peer_id}.jpg")
        saved = await _clients[account_id].download_profile_photo(entity, file=path)
        if saved:
            customer.avatar_path = os.path.relpath(saved, upload_root())
    except Exception as exc:
        print('telegram avatar sync:', repr(exc))

async def save_message_media(account_id: int, message, customer_id: int):
    try:
        if not getattr(message, 'media', None):
            return ''
        size = getattr(getattr(message, 'file', None), 'size', None)
        if size and size > 50 * 1024 * 1024:
            return ''
        ext = getattr(getattr(message, 'file', None), 'ext', None) or ''
        ext = re.sub(r'[^.A-Za-z0-9]', '', ext)[:12]
        path = os.path.join(upload_root(), f"tg_{account_id}_{customer_id}_{message.id}{ext}")
        saved = await message.download_media(file=path)
        return saved or ''
    except Exception as exc:
        print('telegram media download:', repr(exc))
        return ''



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
            # Outgoing messages sent by this CRM are persisted by their API routes.
            # Ignore Telegram's echo event to avoid duplicate rows.
            if event.out:
                return
            sender = await event.get_sender()
            if getattr(sender, 'bot', False): return
            peer_id = str(event.chat_id)
            db = SessionLocal()
            try:
                name = ' '.join(filter(None,[getattr(sender,'first_name',''),getattr(sender,'last_name','')])).strip()
                username = getattr(sender,'username','') or ''
                customer = db.query(Customer).filter_by(account_id=account_id, telegram_peer_id=peer_id).first()
                if not customer:
                    customer = Customer(account_id=account_id, telegram_peer_id=peer_id, telegram_access_hash=str(getattr(sender,'access_hash','') or ''), real_username=username, real_name=name, phone=getattr(sender,'phone','') or '', alias=customer_label(sender))
                    db.add(customer); db.commit(); db.refresh(customer)
                else:
                    customer.real_username=username
                    customer.real_name=name
                    customer.phone=getattr(sender,'phone','') or ''
                    customer.telegram_access_hash=str(getattr(sender,'access_hash','') or customer.telegram_access_hash)
                    customer.alias=customer_label(sender)
                mid = str(event.id)
                if not db.query(Message).filter_by(customer_id=customer.id, telegram_message_id=mid).first():
                    media_path = await save_message_media(account_id, event.message, customer.id)
                    db.add(Message(customer_id=customer.id, telegram_message_id=mid, direction='in', body=event.raw_text or '', media_path=os.path.relpath(media_path, upload_root()) if media_path else '', sent_at=event.date or datetime.now(timezone.utc), sender_label='Customer'))
                    await save_avatar(account_id, customer, sender)
                    db.commit()
            finally: db.close()
        except Exception as e: print('telegram incoming handler:', repr(e))

async def collect_dialogs(client: TelegramClient):
    """Collect all regular and archived dialogs before returning any success result."""
    dialogs_by_peer = {}
    for folder in (0, 1):
        try:
            folder_dialogs = await client.get_dialogs(limit=None, folder=folder)
        except TypeError:
            if folder == 0:
                folder_dialogs = await client.get_dialogs(limit=None)
            else:
                break
        for item in folder_dialogs:
            dialogs_by_peer[str(item.id)] = item
    return list(dialogs_by_peer.values())


async def sync_dialogs(account_id: int, client: TelegramClient, sync_messages: bool = True):
    """Upsert every dialog first; optionally download message history afterwards.

    When sync_messages=False, this is the fast path used by the admin UI: the
    complete customer list is committed before background message-history work.
    """
    db = SessionLocal()
    created = 0
    updated = 0
    eligible = []
    try:
        dialogs = await collect_dialogs(client)
        for dialog in dialogs:
            entity = dialog.entity
            if getattr(entity, 'bot', False) or not getattr(entity, 'id', None):
                continue
            peer_id = str(dialog.id)
            name = ' '.join(filter(None, [
                getattr(entity, 'first_name', ''), getattr(entity, 'last_name', '')
            ])).strip()
            username = getattr(entity, 'username', '') or ''
            customer = db.query(Customer).filter_by(
                account_id=account_id, telegram_peer_id=peer_id
            ).first()
            if not customer:
                customer = Customer(
                    account_id=account_id,
                    telegram_peer_id=peer_id,
                    telegram_access_hash=str(getattr(entity, 'access_hash', '') or ''),
                    real_username=username,
                    real_name=name,
                    phone=getattr(entity, 'phone', '') or '',
                    alias=customer_label(entity)
                )
                db.add(customer)
                db.flush()
                created += 1
            else:
                customer.real_username = username
                customer.real_name = name
                customer.phone = getattr(entity, 'phone', '') or ''
                customer.telegram_access_hash = str(
                    getattr(entity, 'access_hash', '') or customer.telegram_access_hash
                )
                customer.alias = customer_label(entity)
                updated += 1
            try:
                await save_avatar(account_id, customer, entity)
            except Exception as exc:
                print('telegram avatar sync:', repr(exc))
            eligible.append((entity, customer.id))
        db.commit()
        result = {
            'account_id': account_id,
            'dialog_count': len(dialogs),
            'customer_count': len(eligible),
            'created_count': created,
            'updated_count': updated,
            'messages_synced': 0,
            'history_pending': not sync_messages
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    if sync_messages:
        history = await sync_dialog_messages(account_id, client, eligible)
        result.update(history)
        result['history_pending'] = False
    return result


async def sync_dialog_messages(account_id: int, client: TelegramClient, dialog_rows=None):
    """Import recent/full available history for already indexed customer dialogs."""
    db = SessionLocal()
    synced = 0
    errors = []
    try:
        if dialog_rows is None:
            dialogs = await collect_dialogs(client)
            dialog_rows = []
            for dialog in dialogs:
                entity = dialog.entity
                if getattr(entity, 'bot', False) or not getattr(entity, 'id', None):
                    continue
                customer = db.query(Customer).filter_by(
                    account_id=account_id, telegram_peer_id=str(dialog.id)
                ).first()
                if customer:
                    dialog_rows.append((entity, customer.id))
        for entity, customer_id in dialog_rows:
            try:
                # Import all message history exposed by Telegram for this dialog.
                # This runs in the background after the customer index is committed,
                # so the admin can assign customers without waiting for media/history.
                async for msg in client.iter_messages(entity, limit=None, reverse=True):
                    if not msg or not msg.id:
                        continue
                    if db.query(Message).filter_by(
                        customer_id=customer_id,
                        telegram_message_id=str(msg.id)
                    ).first():
                        continue
                    media_path = await save_message_media(account_id, msg, customer_id)
                    db.add(Message(
                        customer_id=customer_id,
                        telegram_message_id=str(msg.id),
                        direction='out' if msg.out else 'in',
                        body=msg.message or '',
                        media_path=os.path.relpath(media_path, upload_root()) if media_path else '',
                        sent_at=msg.date or datetime.now(timezone.utc),
                        sender_label='Agent' if msg.out else 'Customer'
                    ))
                    try:
                        db.commit()
                        synced += 1
                    except IntegrityError:
                        db.rollback()
            except Exception as exc:
                db.rollback()
                errors.append(f'{getattr(entity, "id", "unknown")}: {exc}')
                print('telegram history sync:', repr(exc))
        return {'messages_synced': synced, 'history_errors': errors}
    finally:
        db.close()

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


async def send_media(account_id: int, customer: Customer, file_path: str, caption: str = ''):
    client = _clients.get(account_id)
    if not client:
        raise RuntimeError('Telegram account is offline; reconnect from admin panel')
    peer = await client.get_input_entity(int(customer.telegram_peer_id))
    ext = os.path.splitext(file_path)[1].lower()
    is_voice = ext in ('.ogg', '.opus')
    sent = await client.send_file(peer, file_path, caption=caption or None, voice_note=is_voice)
    return sent


async def forward_message(account_id: int, source_customer: Customer, target_customer: Customer, telegram_message_id: str):
    client = _clients.get(account_id)
    if not client:
        raise RuntimeError('Telegram account is offline; reconnect from admin panel')
    source_peer = await client.get_input_entity(int(source_customer.telegram_peer_id))
    target_peer = await client.get_input_entity(int(target_customer.telegram_peer_id))
    forwarded = await client.forward_messages(target_peer, int(telegram_message_id), from_peer=source_peer)
    if isinstance(forwarded, list):
        if not forwarded:
            raise RuntimeError('Telegram did not return a forwarded message.')
        return forwarded[0]
    return forwarded
