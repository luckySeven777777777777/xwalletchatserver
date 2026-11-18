import asyncio, re, os, mimetypes
from types import SimpleNamespace
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
        root = upload_root()
        path = os.path.join(root, f"avatar_{account_id}_{customer.telegram_peer_id}.jpg")
        saved = await _clients[account_id].download_profile_photo(entity, file=path)
        if saved and os.path.isfile(saved):
            customer.avatar_path = os.path.relpath(saved, root)
        elif os.path.isfile(path):
            customer.avatar_path = os.path.relpath(path, root)
    except Exception as exc:
        print('telegram avatar sync:', repr(exc))

async def save_message_media(account_id: int, message, customer_id: int):
    """Persist Telegram photos, videos, documents and voice notes on the Railway volume."""
    try:
        if not getattr(message, 'media', None):
            return ''
        tg_file = getattr(message, 'file', None)
        size = getattr(tg_file, 'size', None)
        if size and size > 200 * 1024 * 1024:
            print(f'telegram media skipped (over 200 MB): message={getattr(message, "id", "?")} size={size}')
            return ''
        mime = getattr(tg_file, 'mime_type', None) or ''
        ext = getattr(tg_file, 'ext', None) or ''
        if not ext and mime:
            ext = mimetypes.guess_extension(mime) or ''
        if not ext:
            if getattr(message, 'photo', None):
                ext = '.jpg'
            elif getattr(message, 'voice', None) or getattr(message, 'audio', None):
                ext = '.ogg' if getattr(message, 'voice', None) else '.mp3'
            elif getattr(message, 'video', None):
                ext = '.mp4'
            else:
                ext = '.bin'
        ext = re.sub(r'[^.A-Za-z0-9]', '', ext)[:12] or '.bin'
        root = upload_root()
        path = os.path.join(root, f"tg_{account_id}_{customer_id}_{message.id}{ext}")
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
        saved = await message.download_media(file=path)
        if saved and os.path.isfile(saved) and os.path.getsize(saved) > 0:
            return saved
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
        return ''
    except OSError as exc:
        print('telegram media storage error (check Railway Volume free space):', repr(exc))
        return ''
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
                existing = db.query(Message).filter_by(customer_id=customer.id, telegram_message_id=mid).first()
                if existing:
                    # A previous sync may have inserted the text while the volume was full.
                    # Re-fetch media when the saved path is empty OR points to a missing file.
                    existing_path = os.path.join(upload_root(), existing.media_path) if existing.media_path else ''
                    if getattr(event.message, 'media', None) and (not existing_path or not os.path.isfile(existing_path)):
                        recovered = await save_message_media(account_id, event.message, customer.id)
                        if recovered:
                            existing.media_path = os.path.relpath(recovered, upload_root())
                            db.commit()
                else:
                    media_path = await save_message_media(account_id, event.message, customer.id)
                    db.add(Message(customer_id=customer.id, telegram_message_id=mid, direction='in', body=event.raw_text or '', media_path=os.path.relpath(media_path, upload_root()) if media_path else '', sent_at=event.date or datetime.now(timezone.utc), sender_label='Customer'))
                    await save_avatar(account_id, customer, sender)
                    db.commit()
            finally: db.close()
        except Exception as e: print('telegram incoming handler:', repr(e))

async def collect_dialogs(client: TelegramClient):
    """Collect main and archived folders and explicitly include Saved Messages.

    Telethon's folder=0 is the regular chat list and folder=1 is Archived Chats.
    We preserve folder metadata so the admin CRM can distinguish archived chats.
    Saved Messages is an account-private dialog and is marked separately.
    """
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
            peer_key = str(item.id)
            dialogs_by_peer[peer_key] = SimpleNamespace(
                id=item.id,
                entity=item.entity,
                folder_id=int(getattr(item, 'folder_id', folder) or folder),
                is_saved_messages=False
            )

    # Saved Messages may not be returned consistently by get_dialogs() across
    # Telegram account states / Telethon versions. Add it explicitly by self ID.
    try:
        me = await client.get_me()
        if me and getattr(me, 'id', None):
            key = str(me.id)
            dialogs_by_peer[key] = SimpleNamespace(
                id=me.id,
                entity=me,
                folder_id=2,
                is_saved_messages=True
            )
    except Exception as exc:
        print('telegram saved messages discovery:', repr(exc))

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
            is_saved_messages = bool(getattr(dialog, 'is_saved_messages', False))
            folder_id = 2 if is_saved_messages else int(getattr(dialog, 'folder_id', 0) or 0)
            name = 'Saved Messages' if is_saved_messages else ' '.join(filter(None, [
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
                    alias='Saved Messages' if is_saved_messages else customer_label(entity),
                    dialog_folder=folder_id,
                    is_saved_messages=is_saved_messages
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
                customer.alias = 'Saved Messages' if is_saved_messages else customer_label(entity)
                customer.dialog_folder = folder_id
                customer.is_saved_messages = is_saved_messages
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
            'archived_count': db.query(Customer).filter_by(account_id=account_id, dialog_folder=1).count(),
            'saved_messages_included': db.query(Customer).filter_by(account_id=account_id, is_saved_messages=True).count() > 0,
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
    media_recovered = 0
    media_failed = 0
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
                    existing = db.query(Message).filter_by(
                        customer_id=customer_id,
                        telegram_message_id=str(msg.id)
                    ).first()
                    # Older sync runs may have stored the text but failed to save
                    # the attachment (for example when the Railway volume was full).
                    if existing:
                        existing_path = os.path.join(upload_root(), existing.media_path) if existing.media_path else ''
                        if getattr(msg, 'media', None) and (not existing_path or not os.path.isfile(existing_path)):
                            media_path = await save_message_media(account_id, msg, customer_id)
                            if media_path:
                                existing.media_path = os.path.relpath(media_path, upload_root())
                                db.commit()
                                media_recovered += 1
                            else:
                                media_failed += 1
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
                        if media_path:
                            media_recovered += 1
                        elif getattr(msg, 'media', None):
                            media_failed += 1
                    except IntegrityError:
                        db.rollback()
            except Exception as exc:
                db.rollback()
                errors.append(f'{getattr(entity, "id", "unknown")}: {exc}')
                print('telegram history sync:', repr(exc))
        return {'messages_synced': synced, 'media_recovered': media_recovered, 'media_failed': media_failed, 'history_errors': errors}
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
