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

# Telegram work is isolated from HTTP request lifetimes. Each account is synced
# at most once at a time, with a small global concurrency cap for Hobby resources.
_ACCOUNT_RESTORE_SEMAPHORE = asyncio.Semaphore(3)
_ACCOUNT_SYNC_SEMAPHORE = asyncio.Semaphore(2)
_ACCOUNT_SYNC_TASKS: dict[int, asyncio.Task] = {}
_ACCOUNT_SYNC_STATUS: dict[int, dict] = {}
_BACKGROUND_TASKS: set[asyncio.Task] = set()
_AVATAR_SYNC_TASKS: dict[int, asyncio.Task] = {}
_ACCOUNT_SYNC_WANTS_HISTORY: set[int] = set()
_AVATAR_SYNC_SEMAPHORE = asyncio.Semaphore(3)
_ACCOUNT_SEND_SEMAPHORES: dict[int, asyncio.Semaphore] = {}
_ACCOUNT_INCOMING_SEMAPHORES: dict[int, asyncio.Semaphore] = {}


def _track_task(task: asyncio.Task):
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


def queue_account_sync(account_id: int, *, sync_messages: bool = False) -> dict:
    """Queue a deduplicated Telegram account sync and return without blocking HTTP."""
    current = _ACCOUNT_SYNC_TASKS.get(account_id)
    if current and not current.done():
        if sync_messages:
            _ACCOUNT_SYNC_WANTS_HISTORY.add(account_id)
        return {"queued": True, "already_running": True, "status": _ACCOUNT_SYNC_STATUS.get(account_id, {}).get("status", "queued")}

    async def runner():
        _ACCOUNT_SYNC_STATUS[account_id] = {"status": "queued", "started_at": None, "finished_at": None, "error": None}
        async with _ACCOUNT_SYNC_SEMAPHORE:
            client = _clients.get(account_id)
            if not client or not client.is_connected():
                _ACCOUNT_SYNC_STATUS[account_id] = {"status": "offline", "started_at": None, "finished_at": None, "error": "Telegram client is not connected"}
                return
            _ACCOUNT_SYNC_STATUS[account_id]["status"] = "running"
            _ACCOUNT_SYNC_STATUS[account_id]["started_at"] = datetime.now(timezone.utc).isoformat()
            try:
                result = await sync_dialogs(account_id, client, sync_messages=False)
                if sync_messages or account_id in _ACCOUNT_SYNC_WANTS_HISTORY:
                    _ACCOUNT_SYNC_WANTS_HISTORY.discard(account_id)
                    history = await sync_dialog_messages(account_id, client)
                    result.update(history)
                _ACCOUNT_SYNC_STATUS[account_id] = {
                    "status": "completed", "started_at": _ACCOUNT_SYNC_STATUS[account_id]["started_at"],
                    "finished_at": datetime.now(timezone.utc).isoformat(), "error": None,
                    "customer_count": result.get("customer_count", 0),
                    "messages_synced": result.get("messages_synced", 0)
                }
            except asyncio.CancelledError:
                _ACCOUNT_SYNC_STATUS[account_id] = {"status": "cancelled", "started_at": None, "finished_at": datetime.now(timezone.utc).isoformat(), "error": "cancelled"}
                raise
            except Exception as exc:
                _ACCOUNT_SYNC_STATUS[account_id] = {"status": "error", "started_at": _ACCOUNT_SYNC_STATUS[account_id].get("started_at"), "finished_at": datetime.now(timezone.utc).isoformat(), "error": str(exc)[:300]}
                print("background Telegram account sync failed:", account_id, repr(exc))

    task = _track_task(asyncio.create_task(runner()))
    _ACCOUNT_SYNC_TASKS[account_id] = task
    return {"queued": True, "already_running": False, "status": "queued"}


def account_sync_status(account_id: int) -> dict:
    return dict(_ACCOUNT_SYNC_STATUS.get(account_id, {"status": "idle"}))


def upload_root():
    path = os.path.abspath(settings.upload_dir)
    os.makedirs(path, exist_ok=True)
    return path

def message_media_kind(message):
    if not getattr(message, 'media', None):
        return ''
    if getattr(message, 'photo', None):
        return 'image'
    if getattr(message, 'video', None) or getattr(message, 'video_note', None):
        return 'video'
    if getattr(message, 'voice', None) or getattr(message, 'audio', None):
        return 'audio'
    file_info = getattr(message, 'file', None)
    mime = (getattr(file_info, 'mime_type', None) or '').lower()
    if mime.startswith('image/'):
        return 'image'
    if mime.startswith('video/'):
        return 'video'
    if mime.startswith('audio/'):
        return 'audio'
    return 'file'


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
            elif getattr(message, 'video', None) or getattr(message, 'video_note', None):
                ext = '.mp4'
            elif mime.lower().startswith('image/'):
                ext = mimetypes.guess_extension(mime) or '.jpg'
            elif mime.lower().startswith('video/'):
                ext = mimetypes.guess_extension(mime) or '.mp4'
            elif mime.lower().startswith('audio/'):
                ext = mimetypes.guess_extension(mime) or '.ogg'
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
        # Import customer/dialog metadata automatically; historical media/history
        # remains an explicit admin action so a new login cannot stall the API.
        queue_account_sync(account.id, sync_messages=False)
        return {'account_id': account.id, 'label': account.label, 'phone_masked': account.phone_masked}
    except Exception:
        db.rollback()
        await client.disconnect()
        raise
    finally:
        db.close()


async def _process_incoming_message(account_id: int, event):
    try:
        # Outgoing messages sent by this CRM are persisted by their API routes.
        if event.out:
            return
        sender = await event.get_sender()
        if getattr(sender, 'bot', False):
            return

        peer_id = str(event.chat_id)
        name = ' '.join(filter(None, [getattr(sender, 'first_name', ''), getattr(sender, 'last_name', '')])).strip()
        username = getattr(sender, 'username', '') or ''
        phone = getattr(sender, 'phone', '') or ''
        mid = str(event.id)

        # First perform only the customer lookup/upsert. Close the DB session
        # before any Telegram media/avatar download (network I/O can take a while).
        with SessionLocal() as db:
            customer = db.query(Customer).filter_by(account_id=account_id, telegram_peer_id=peer_id).first()
            if not customer:
                customer = Customer(
                    account_id=account_id, telegram_peer_id=peer_id,
                    telegram_access_hash=str(getattr(sender, 'access_hash', '') or ''),
                    real_username=username, real_name=name, phone=phone,
                    alias=customer_label(sender)
                )
                db.add(customer)
                db.commit()
                db.refresh(customer)
                customer_id = customer.id
                avatar_missing = True
            else:
                customer.real_username = username
                customer.real_name = name
                customer.phone = phone
                customer.telegram_access_hash = str(getattr(sender, 'access_hash', '') or customer.telegram_access_hash)
                customer.alias = customer_label(sender)
                customer_id = customer.id
                avatar_missing = not bool(customer.avatar_path)
                db.commit()

            existing = db.query(Message).filter_by(customer_id=customer_id, telegram_message_id=mid).first()
            existing_missing_media = bool(existing and getattr(event.message, 'media', None) and (
                not existing.media_path or not os.path.isfile(os.path.join(upload_root(), existing.media_path))
            ))
            message_exists = existing is not None

        # All Telegram network operations happen with no SQL connection checked out.
        media_path = ''
        if getattr(event.message, 'media', None) and (not message_exists or existing_missing_media):
            media_path = await save_message_media(account_id, event.message, customer_id)

        # Persist the message first. Profile-photo downloads are deliberately
        # excluded from the inbound-message critical path.
        with SessionLocal() as db:
            customer = db.get(Customer, customer_id)
            existing = db.query(Message).filter_by(customer_id=customer_id, telegram_message_id=mid).first()
            if existing:
                if existing_missing_media:
                    existing.media_type = message_media_kind(event.message)
                    existing.media_pending = not bool(media_path)
                    if media_path:
                        existing.media_path = os.path.relpath(media_path, upload_root())
            else:
                db.add(Message(
                    customer_id=customer_id, telegram_message_id=mid, direction='in',
                    body=event.raw_text or '',
                    media_path=os.path.relpath(media_path, upload_root()) if media_path else '',
                    media_type=message_media_kind(event.message),
                    media_pending=bool(getattr(event.message, 'media', None) and not media_path),
                    sent_at=event.date or datetime.now(timezone.utc), sender_label='Customer'
                ))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()  # Another sync/handler may have inserted the same Telegram message.
    except Exception as e:
        print('telegram incoming handler:', repr(e))

def attach_handlers(account_id: int, client: TelegramClient):
    @client.on(events.NewMessage)
    async def on_message(event):
        semaphore = _ACCOUNT_INCOMING_SEMAPHORES.setdefault(account_id, asyncio.Semaphore(6))
        async with semaphore:
            await _process_incoming_message(account_id, event)


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


async def _sync_missing_avatars(account_id: int, avatar_jobs):
    """Download photos after dialog metadata is committed; never delay inbox indexing."""
    async with _AVATAR_SYNC_SEMAPHORE:
        for entity, customer_id, peer_id in avatar_jobs:
            try:
                avatar_stub = SimpleNamespace(telegram_peer_id=peer_id, avatar_path='')
                await asyncio.wait_for(save_avatar(account_id, avatar_stub, entity), timeout=20)
                if avatar_stub.avatar_path:
                    with SessionLocal() as db:
                        row = db.get(Customer, customer_id)
                        if row and not row.avatar_path:
                            row.avatar_path = avatar_stub.avatar_path
                            db.commit()
            except Exception as exc:
                print('telegram avatar background sync:', account_id, customer_id, repr(exc))


async def sync_dialogs(account_id: int, client: TelegramClient, sync_messages: bool = True):
    """Index Telegram dialogs using short DB sessions; never hold a connection during Telegram I/O."""
    created = 0
    updated = 0
    eligible = []
    avatar_jobs = []
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

        with SessionLocal() as db:
            customer = db.query(Customer).filter_by(account_id=account_id, telegram_peer_id=peer_id).first()
            if not customer:
                customer = Customer(
                    account_id=account_id, telegram_peer_id=peer_id,
                    telegram_access_hash=str(getattr(entity, 'access_hash', '') or ''),
                    real_username=username, real_name=name,
                    phone=getattr(entity, 'phone', '') or '',
                    alias='Saved Messages' if is_saved_messages else customer_label(entity),
                    dialog_folder=folder_id, is_saved_messages=is_saved_messages
                )
                db.add(customer)
                try:
                    db.commit()
                    db.refresh(customer)
                    created += 1
                except IntegrityError:
                    db.rollback()
                    customer = db.query(Customer).filter_by(account_id=account_id, telegram_peer_id=peer_id).first()
            else:
                customer.real_username = username
                customer.real_name = name
                customer.phone = getattr(entity, 'phone', '') or ''
                customer.telegram_access_hash = str(getattr(entity, 'access_hash', '') or customer.telegram_access_hash)
                customer.alias = 'Saved Messages' if is_saved_messages else customer_label(entity)
                customer.dialog_folder = folder_id
                customer.is_saved_messages = is_saved_messages
                db.commit()
                updated += 1
            customer_id = customer.id if customer else None
            avatar_missing = not bool(customer and customer.avatar_path)

        if customer_id is None:
            continue
        eligible.append((entity, customer_id))

        # Queue photos separately. Customer names/IDs are indexed first so staff
        # can start working while slower Telegram photo downloads continue.
        if avatar_missing:
            avatar_jobs.append((entity, customer_id, peer_id))

    if avatar_jobs:
        existing_avatar_task = _AVATAR_SYNC_TASKS.get(account_id)
        if not existing_avatar_task or existing_avatar_task.done():
            avatar_task = _track_task(asyncio.create_task(_sync_missing_avatars(account_id, avatar_jobs)))
            _AVATAR_SYNC_TASKS[account_id] = avatar_task

    with SessionLocal() as db:
        archived_count = db.query(Customer).filter_by(account_id=account_id, dialog_folder=1).count()
        saved_messages_included = db.query(Customer).filter_by(account_id=account_id, is_saved_messages=True).count() > 0

    result = {
        'account_id': account_id,
        'dialog_count': len(dialogs),
        'customer_count': len(eligible),
        'created_count': created,
        'updated_count': updated,
        'archived_count': archived_count,
        'saved_messages_included': saved_messages_included,
        'messages_synced': 0,
        'history_pending': not sync_messages
    }
    if sync_messages:
        history = await sync_dialog_messages(account_id, client, eligible)
        result.update(history)
        result['history_pending'] = False
    return result

async def sync_dialog_messages(account_id: int, client: TelegramClient, dialog_rows=None):
    """Import Telegram history without holding a DB connection during network I/O."""
    synced = 0
    media_recovered = 0
    media_failed = 0
    errors = []

    if dialog_rows is None:
        dialogs = await collect_dialogs(client)
        dialog_rows = []
        with SessionLocal() as db:
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
            # Telethon iteration and media downloads occur without an open SQL session.
            async for msg in client.iter_messages(entity, limit=None, reverse=True):
                if not msg or not msg.id:
                    continue
                message_id = str(msg.id)
                with SessionLocal() as db:
                    existing = db.query(Message).filter_by(
                        customer_id=customer_id, telegram_message_id=message_id
                    ).first()
                    existing_id = existing.id if existing else None
                    existing_path = existing.media_path if existing else ''

                existing_file = os.path.join(upload_root(), existing_path) if existing_path else ''
                needs_media = bool(getattr(msg, 'media', None) and (
                    not existing_id or not existing_file or not os.path.isfile(existing_file)
                ))
                media_path = await save_message_media(account_id, msg, customer_id) if needs_media else ''

                with SessionLocal() as db:
                    existing = db.query(Message).filter_by(
                        customer_id=customer_id, telegram_message_id=message_id
                    ).first()
                    if existing:
                        if needs_media:
                            existing.media_type = message_media_kind(msg)
                            existing.media_pending = not bool(media_path)
                            if media_path:
                                existing.media_path = os.path.relpath(media_path, upload_root())
                                media_recovered += 1
                            else:
                                media_failed += 1
                            db.commit()
                        continue

                    db.add(Message(
                        customer_id=customer_id,
                        telegram_message_id=message_id,
                        direction='out' if msg.out else 'in',
                        body=msg.message or '',
                        media_path=os.path.relpath(media_path, upload_root()) if media_path else '',
                        media_type=message_media_kind(msg),
                        media_pending=bool(getattr(msg, 'media', None) and not media_path),
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
            errors.append(f'{getattr(entity, "id", "unknown")}: {exc}')
            print('telegram history sync:', repr(exc))

    return {'messages_synced': synced, 'media_recovered': media_recovered,
            'media_failed': media_failed, 'history_errors': errors}

async def restore_clients():
    """Restore saved Telegram sessions with bounded concurrency, not serial startup blocking."""
    with SessionLocal() as db:
        rows = [(row.id, row.session_ciphertext) for row in
                db.query(TgAccount).filter(TgAccount.status.in_(('connected', 'error'))).all()]

    async def restore_one(account_id: int, ciphertext: str):
        async with _ACCOUNT_RESTORE_SEMAPHORE:
            for attempt in range(3):
                client = None
                try:
                    client = TelegramClient(
                        StringSession(decrypt_session(ciphertext)),
                        int(settings.telegram_api_id), settings.telegram_api_hash
                    )
                    await asyncio.wait_for(client.connect(), timeout=25)
                    authorized = await asyncio.wait_for(client.is_user_authorized(), timeout=15)
                    if not authorized:
                        await client.disconnect()
                        with SessionLocal() as db:
                            row = db.get(TgAccount, account_id)
                            if row:
                                row.status = 'reauthorization_required'
                                db.commit()
                        return
                    _clients[account_id] = client
                    with SessionLocal() as db:
                        row = db.get(TgAccount, account_id)
                        if row and row.status != 'deleted':
                            row.status = 'connected'
                            db.commit()
                    attach_handlers(account_id, client)
                    return
                except Exception as exc:
                    if client:
                        try:
                            await client.disconnect()
                        except Exception:
                            pass
                    if attempt < 2:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    with SessionLocal() as db:
                        row = db.get(TgAccount, account_id)
                        if row:
                            # Keep the saved credential record; surface a recoverable
                            # connection state instead of deleting or overwriting it.
                            row.status = 'error'
                            db.commit()
                    print('restore account:', account_id, repr(exc))

    await asyncio.gather(*(restore_one(account_id, ciphertext) for account_id, ciphertext in rows))
    print(f'telegram restore complete: {len(_clients)}/{len(rows)} connected')


def _account_send_semaphore(account_id: int):
    return _ACCOUNT_SEND_SEMAPHORES.setdefault(account_id, asyncio.Semaphore(3))


async def send_message(account_id: int, customer: Customer, text: str):
    client = _clients.get(account_id)
    if not client or not client.is_connected():
        raise RuntimeError('Telegram account is offline; reconnect from admin panel')
    async with _account_send_semaphore(account_id):
        peer = await client.get_input_entity(int(customer.telegram_peer_id))
        sent = await client.send_message(peer, text)
        return sent


async def send_media(account_id: int, customer: Customer, file_path: str, caption: str = ''):
    client = _clients.get(account_id)
    if not client or not client.is_connected():
        raise RuntimeError('Telegram account is offline; reconnect from admin panel')
    async with _account_send_semaphore(account_id):
        peer = await client.get_input_entity(int(customer.telegram_peer_id))
        ext = os.path.splitext(file_path)[1].lower()
        is_voice = ext in ('.ogg', '.opus')
        sent = await client.send_file(peer, file_path, caption=caption or None, voice_note=is_voice)
        return sent


async def forward_message(account_id: int, source_customer: Customer, target_customer: Customer, telegram_message_id: str):
    client = _clients.get(account_id)
    if not client or not client.is_connected():
        raise RuntimeError('Telegram account is offline; reconnect from admin panel')
    async with _account_send_semaphore(account_id):
        source_peer = await client.get_input_entity(int(source_customer.telegram_peer_id))
        target_peer = await client.get_input_entity(int(target_customer.telegram_peer_id))
        forwarded = await client.forward_messages(target_peer, int(telegram_message_id), from_peer=source_peer)
        if isinstance(forwarded, list):
            if not forwarded:
                raise RuntimeError('Telegram did not return a forwarded message.')
            return forwarded[0]
        return forwarded


async def close_clients():
    """Gracefully stop sync/photo tasks and disconnect Telegram clients on shutdown."""
    tasks = [task for task in list(_BACKGROUND_TASKS) if not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _BACKGROUND_TASKS.clear()
    clients = list(_clients.values())
    _clients.clear()
    for client in clients:
        try:
            if client.is_connected():
                await client.disconnect()
        except Exception as exc:
            print('telegram client shutdown:', repr(exc))
    # Close any unfinished login challenges as well.
    pending = list(_pending.values())
    _pending.clear()
    for item in pending:
        client = item.get('client')
        try:
            if client and client.is_connected():
                await client.disconnect()
        except Exception as exc:
            print('telegram pending login shutdown:', repr(exc))
