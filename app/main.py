import asyncio, re, secrets, os, uuid, mimetypes, base64, hmac, json
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Depends, HTTPException, Header, UploadFile, File, Form, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import desc, text, func, inspect
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from .db import Base, engine, get_db, SessionLocal
from .models import User, Team, TgAccount, Customer, Assignment, Message, Tag, AuditLog, ReadState, PresenceSession, EmployeeAlertConfig, EmployeeAlertState, InboxNotification, AttendanceBotConfig, EmployeeTelegramIdentity, EmployeeBreakState
from .security import hash_password, verify_password, create_token, decode_token, create_2fa_challenge, encrypt_session, decrypt_session
from .config import settings
from . import telegram_service as tg
import pyotp, qrcode
from io import BytesIO
from zoneinfo import ZoneInfo

Base.metadata.create_all(bind=engine)

# Add media recovery metadata to existing Railway PostgreSQL/SQLite databases.
# create_all() does not alter an already-created messages table.
def ensure_message_media_columns():
    with engine.begin() as conn:
        columns = {col['name'] for col in inspect(conn).get_columns('messages')}
        if 'media_type' not in columns:
            conn.execute(text("ALTER TABLE messages ADD COLUMN media_type VARCHAR(20) NOT NULL DEFAULT ''"))
        if 'media_pending' not in columns:
            conn.execute(text("ALTER TABLE messages ADD COLUMN media_pending BOOLEAN NOT NULL DEFAULT FALSE"))

ensure_message_media_columns()

# Link each connected Telegram personal account to the employee responsible for it.
# Nullable to preserve all existing Telegram account records during upgrade.
def ensure_telegram_owner_column():
    with engine.begin() as conn:
        columns = {col['name'] for col in inspect(conn).get_columns('telegram_accounts')}
        if 'assigned_user_id' not in columns:
            conn.execute(text("ALTER TABLE telegram_accounts ADD COLUMN assigned_user_id INTEGER"))
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_telegram_accounts_assigned_user_id ON telegram_accounts (assigned_user_id)"))
        except Exception:
            pass

ensure_telegram_owner_column()

# Fine-grained delegated-admin permissions. Superadmins always bypass this matrix.
ADMIN_PERMISSION_KEYS = {
    'users.create_employee', 'users.create_admin', 'users.manage_status',
    'users.delete', 'users.reset_password', 'users.manage_team', 'users.manage_2fa',
    'teams.manage', 'telegram.connect', 'telegram.sync', 'customers.assign',
    'stats.view',
}
LEGACY_ADMIN_PERMISSIONS = sorted(ADMIN_PERMISSION_KEYS)

def user_permissions(user):
    if user.role == 'superadmin':
        return sorted(ADMIN_PERMISSION_KEYS)
    try:
        value = json.loads(user.permissions or '[]')
        return sorted({p for p in value if p in ADMIN_PERMISSION_KEYS}) if isinstance(value, list) else []
    except Exception:
        return []

def require_permission(actor, permission):
    if actor.role == 'superadmin':
        return
    if actor.role != 'admin' or permission not in user_permissions(actor):
        raise HTTPException(403, f'Missing administrator permission: {permission}')

def require_any_permission(actor, *permissions):
    if actor.role == 'superadmin':
        return
    allowed=set(user_permissions(actor))
    if actor.role != 'admin' or not (allowed & set(permissions)):
        raise HTTPException(403, 'Missing administrator permission: ' + ' or '.join(permissions))

# Backward-compatible migration: add stable public six-digit account IDs without
# changing the internal primary keys used by assignments and foreign keys.
with engine.begin() as conn:
    columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)").fetchall()} if engine.dialect.name == "sqlite" else {row[0] for row in conn.exec_driver_sql("SELECT column_name FROM information_schema.columns WHERE table_name='users'").fetchall()}
    if "public_id" not in columns:
        conn.exec_driver_sql("ALTER TABLE users ADD COLUMN public_id VARCHAR(6)")
    if "totp_secret" not in columns:
        conn.exec_driver_sql("ALTER TABLE users ADD COLUMN totp_secret VARCHAR(64)")
    if "totp_enabled" not in columns:
        conn.exec_driver_sql("ALTER TABLE users ADD COLUMN totp_enabled BOOLEAN NOT NULL DEFAULT FALSE")
    # TOTP Fernet ciphertext is about 140 characters; VARCHAR(64) is too short.
    if engine.dialect.name == 'postgresql':
        conn.exec_driver_sql("ALTER TABLE users ALTER COLUMN totp_secret TYPE VARCHAR(255)")
    conn.exec_driver_sql("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_public_id ON users (public_id)")
    if "created_by_user_id" not in columns:
        conn.exec_driver_sql("ALTER TABLE users ADD COLUMN created_by_user_id INTEGER")
    if "permissions" not in columns:
        conn.exec_driver_sql("ALTER TABLE users ADD COLUMN permissions TEXT NOT NULL DEFAULT '[]'")
        # Preserve the existing behavior of already-created delegated admins.
        conn.exec_driver_sql("UPDATE users SET permissions = '" + json.dumps(LEGACY_ADMIN_PERMISSIONS) + "' WHERE role = 'admin'")
    customer_columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(customers)").fetchall()} if engine.dialect.name == "sqlite" else {row[0] for row in conn.exec_driver_sql("SELECT column_name FROM information_schema.columns WHERE table_name='customers'").fetchall()}
    if "avatar_path" not in customer_columns:
        conn.exec_driver_sql("ALTER TABLE customers ADD COLUMN avatar_path VARCHAR(500) NOT NULL DEFAULT ''")
    if "dialog_folder" not in customer_columns:
        conn.exec_driver_sql("ALTER TABLE customers ADD COLUMN dialog_folder INTEGER NOT NULL DEFAULT 0")
    if "is_saved_messages" not in customer_columns:
        conn.exec_driver_sql("ALTER TABLE customers ADD COLUMN is_saved_messages BOOLEAN NOT NULL DEFAULT FALSE")

    # Additive migration for older PostgreSQL deployments. create_all() does not
    # add columns to an existing table, so this must run before presence queries.
    presence_columns = (
        {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(presence_sessions)").fetchall()}
        if engine.dialect.name == "sqlite"
        else {row[0] for row in conn.exec_driver_sql(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'presence_sessions'"
        ).fetchall()}
    )
    if "channel" not in presence_columns:
        conn.exec_driver_sql(
            "ALTER TABLE presence_sessions "
            "ADD COLUMN channel VARCHAR(40) NOT NULL DEFAULT 'Telegram'"
        )

    message_columns = (
        {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(messages)").fetchall()}
        if engine.dialect.name == 'sqlite'
        else {row[0] for row in conn.exec_driver_sql(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'messages'"
        ).fetchall()}
    )
    if 'transcript' not in message_columns:
        conn.exec_driver_sql("ALTER TABLE messages ADD COLUMN transcript TEXT NOT NULL DEFAULT ''")

def make_public_user_id(db):
    for _ in range(200):
        candidate = str(secrets.randbelow(900000) + 100000)
        if not db.query(User).filter_by(public_id=candidate).first():
            return candidate
    raise RuntimeError("Could not allocate a unique six-digit public user ID")

with SessionLocal() as _migration_db:
    for _user in _migration_db.query(User).filter(User.public_id.is_(None)).all():
        _user.public_id = make_public_user_id(_migration_db)
    _migration_db.commit()

app = FastAPI(title='X-Wallet Chat Management', version='7.7.2')
# Memory cache prevents connected-account avatar refresh from failing on a full Railway volume.
_TELEGRAM_ACCOUNT_AVATAR_CACHE: dict[int, bytes] = {}
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins.split(','), allow_credentials=False, allow_methods=['*'], allow_headers=['*'])
app.mount('/static', StaticFiles(directory='static'), name='static')

class LoginIn(BaseModel): username: str; password: str
class TwoFactorVerifyIn(BaseModel): challenge_token: str; code: str
class TwoFactorCodeIn(BaseModel): code: str
class ForwardIn(BaseModel): target_customer_id: int
class UserIn(BaseModel): username: str; password: str = Field(min_length=10); role: str = 'chatting'; team_id: int | None = None; auto_team: bool = True; permissions: list[str] = Field(default_factory=list)
class UserPermissionsIn(BaseModel): permissions: list[str] = Field(default_factory=list)
class TeamIn(BaseModel): name: str
class PhoneIn(BaseModel): phone: str
class CodeIn(BaseModel): phone_code_hash: str; code: str; password: str | None = None
class AssignIn(BaseModel): customer_id: int; user_id: int
class BulkAssignIn(BaseModel): customer_ids: list[int]; user_id: int; account_id: int
class SendIn(BaseModel): text: str = Field(min_length=1, max_length=4096)
class TagIn(BaseModel): key: str; value: str; scope: str = 'private'
class EmployeeAlertConfigIn(BaseModel): bot_token: str = ''; chat_ids: list[str] = Field(default_factory=list); enabled: bool = True
class AttendanceIdentityIn(BaseModel): user_id: int; telegram_user_id: str
class AttendanceBotConfigIn(BaseModel): bot_token: str = ''; bot_id: str = ''; group_chat_id: str = ''; enabled: bool = True; employees: list[AttendanceIdentityIn] = Field(default_factory=list)

UPLOAD_ROOT = os.path.abspath(settings.upload_dir)
os.makedirs(UPLOAD_ROOT, exist_ok=True)
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
ALLOWED_MEDIA_EXTENSIONS = {'.jpg','.jpeg','.png','.gif','.webp','.bmp','.heic','.tif','.tiff','.mp4','.mov','.m4v','.webm','.mkv','.avi','.mpg','.mpeg','.mp3','.m4a','.ogg','.oga','.opus','.wav','.aac','.flac','.aiff','.3gp'}

def utc_iso(value):
    if value is None: return None
    if value.tzinfo is None: value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()

def touch_presence(db: Session, user: User, session_key: str, *, login: bool = False, logout: bool = False):
    key = (session_key or 'default')[:120]
    now_utc = datetime.now(timezone.utc)
    row = db.query(PresenceSession).filter_by(user_id=user.id, session_key=key).first()
    if row is None:
        row = PresenceSession(user_id=user.id, session_key=key, login_at=now_utc, last_seen=now_utc, is_active=True)
        db.add(row)
    else:
        row.last_seen = now_utc
        if login:
            row.login_at = now_utc
            row.logout_at = None
            row.is_active = True
        if logout:
            row.logout_at = now_utc
            row.is_active = False
    if logout and row is None:
        return
    user.last_seen = now_utc
    db.commit()
    return row

def media_kind(path):
    ext = os.path.splitext(path or '')[1].lower()
    if ext in {'.jpg','.jpeg','.png','.gif','.webp','.bmp','.heic','.tif','.tiff'}: return 'image'
    if ext in {'.mp4','.mov','.m4v','.webm','.mkv','.avi','.mpg','.mpeg'}: return 'video'
    if ext in {'.mp3','.m4a','.ogg','.oga','.opus','.wav','.aac','.flac','.aiff','.3gp'}: return 'audio'
    mime = mimetypes.guess_type(path or '')[0] or ''
    if mime.startswith('image/'): return 'image'
    if mime.startswith('video/'): return 'video'
    if mime.startswith('audio/'): return 'audio'
    return 'file'

def safe_upload_path(relative):
    root_path = os.path.realpath(UPLOAD_ROOT)
    full = os.path.realpath(os.path.join(root_path, relative or ''))
    if not full.startswith(root_path + os.sep) or not os.path.isfile(full):
        raise HTTPException(404, 'Media file not found')
    return full

@app.on_event('startup')
async def startup():
    db = SessionLocal()
    try:
        if settings.admin_password and not db.query(User).filter_by(username=settings.admin_username).first():
            db.add(User(username=settings.admin_username, public_id=make_public_user_id(db), password_hash=hash_password(settings.admin_password), role='superadmin', active=True)); db.commit()
    finally: db.close()
    app.state.employee_alert_task = asyncio.create_task(employee_offline_alert_loop())
    app.state.attendance_bot_task = asyncio.create_task(attendance_bot_loop())
    if settings.telegram_api_id and settings.telegram_api_hash and settings.session_encryption_key:
        # Await restoration so Sync buttons cannot race startup and report a false
        # 'not connected' state while the account is still being rehydrated.
        await tg.restore_clients()

@app.on_event('shutdown')
async def shutdown_employee_alerts():
    for name in ('employee_alert_task', 'attendance_bot_task'):
        task = getattr(app.state, name, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


@app.get('/')
def home(): return FileResponse('static/index.html')

@app.get('/superadmin-login')
def superadmin_login_page(): return FileResponse('static/index.html')

@app.get('/login/{username}')
def employee_login_link(username: str):
    # Chatting employee link; username is prefilled but never grants access by itself.
    return FileResponse('static/index.html')

@app.get('/admin-login/{username}')
def administrator_login_link(username: str):
    # Dedicated Administrator login link. Credentials are still required;
    # the path only selects the correct role-specific login experience.
    return FileResponse('static/index.html')

@app.get('/finding/{username}')
def finding_login_link(username: str):
    # Finding employee link uses a distinct route and brand, but still requires credentials.
    return FileResponse('static/index.html')

@app.post('/api/auth/login')
def login(data: LoginIn, request: Request, db: Session = Depends(get_db)):
    # Employee links may preserve mixed-case usernames. Resolve usernames
    # case-insensitively and trim only the username (never the password).
    submitted_username = data.username.strip()
    user = db.query(User).filter(func.lower(User.username) == submitted_username.lower()).first()

    # Recovery path for the environment-configured bootstrap super administrator.
    # This only works when the submitted password exactly matches Railway's
    # ADMIN_PASSWORD; it cannot reactivate employee or ordinary admin accounts.
    configured_admin = (
        user is not None
        and user.role == 'superadmin'
        and data.username == settings.admin_username
        and bool(settings.admin_password)
        and hmac.compare_digest(data.password, settings.admin_password)
    )
    if configured_admin and not user.active:
        user.active = True
        db.commit()
    password_ok = False
    if user is not None:
        try:
            password_ok = verify_password(data.password, user.password_hash)
        except Exception as exc:
            # Keep the public response generic, but leave a useful server-side
            # diagnostic instead of silently treating a corrupt hash as a typo.
            print(f"auth password verification error for user_id={user.id}: {type(exc).__name__}: {exc}")
    if not user or not user.active or not password_ok:
        reason = 'user_not_found' if user is None else ('account_inactive' if not user.active else 'password_mismatch')
        print(f"auth rejected: username={submitted_username!r}; reason={reason}")
        # If the bootstrap credential is valid but its database hash was changed,
        # restore only this configured superadmin's password hash.
        if configured_admin:
            user.password_hash = hash_password(settings.admin_password)
            user.active = True
            db.commit()
        else:
            raise HTTPException(401, 'Invalid username or password. Ask an administrator to verify the account is active and reset its password if needed.')
    if user.totp_enabled:
        return {'requires_2fa': True, 'challenge_token': create_2fa_challenge(user.id), 'username': user.username}
    session_key = request.headers.get('x-session-key') or secrets.token_urlsafe(24)
    touch_presence(db, user, session_key, login=True)
    audit(db, user, 'auth.login', 'user', user.id, f'role={user.role}')
    return {'token': create_token(user.id,user.role), 'session_key': session_key, 'user': {'id':user.id,'public_id':user.public_id,'username':user.username,'role':user.role,'team_id':user.team_id,'totp_enabled':user.totp_enabled}}

@app.post('/api/auth/2fa/verify')
def verify_login_2fa(data: TwoFactorVerifyIn, request: Request, db: Session = Depends(get_db)):
    payload = decode_token(data.challenge_token)
    if not payload or payload.get('purpose') != '2fa_login':
        raise HTTPException(401, 'Two-factor login challenge expired. Please sign in again.')
    user = db.get(User, int(payload['sub']))
    if not user or not user.active or not user.totp_enabled or not user.totp_secret:
        raise HTTPException(401, 'Two-factor account is unavailable.')
    if not pyotp.TOTP(decrypt_session(user.totp_secret)).verify(data.code.strip(), valid_window=1):
        raise HTTPException(401, 'Invalid 2FA code. Check the code and try again.')
    session_key = request.headers.get('x-session-key') or secrets.token_urlsafe(24)
    touch_presence(db, user, session_key, login=True)
    audit(db, user, 'auth.login_2fa', 'user', user.id, f'role={user.role}')
    return {'token': create_token(user.id,user.role), 'session_key': session_key, 'user': {'id':user.id,'public_id':user.public_id,'username':user.username,'role':user.role,'team_id':user.team_id,'totp_enabled':True}}

def current_user(authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith('Bearer '): raise HTTPException(401,'Login required')
    payload = decode_token(authorization[7:])
    if not payload or payload.get('purpose') is not None: raise HTTPException(401,'Invalid or expired session')
    user = db.get(User, int(payload['sub']))
    if not user or not user.active: raise HTTPException(401,'Account disabled')
    return user

def admin(user: User = Depends(current_user)):
    if user.role not in ('superadmin','admin'): raise HTTPException(403,'Administrator only')
    return user

def superadmin(user: User = Depends(current_user)):
    if user.role != 'superadmin': raise HTTPException(403, 'Super administrator only')
    return user

def audit(db, actor, action, typ='', target='', detail=''):
    # Auditing must never turn a successfully completed primary operation into
    # a misleading HTTP 500. Failures are logged and the business transaction
    # remains committed.
    try:
        db.add(AuditLog(actor_user_id=actor.id if actor else None, action=action,
                        target_type=typ, target_id=str(target), detail=detail))
        db.commit()
    except Exception as exc:
        db.rollback()
        print('audit write failed:', repr(exc))

def can_access(db: Session, user: User, customer_id: int):
    customer = db.get(Customer, customer_id)
    if not customer: return False
    # Saved Messages is the connected Telegram account owner's private archive.
    # Never expose it to employee roles, even if a stale assignment exists.
    if customer.is_saved_messages and user.role not in ('superadmin', 'admin'): return False
    if user.role in ('superadmin','admin'): return True
    return db.query(Assignment).filter_by(customer_id=customer_id,user_id=user.id,active=True).first() is not None

def safe_customer(c: Customer, user: User | None = None):
    admin_view = user is not None and user.role in ('superadmin','admin')
    if admin_view:
        label = c.real_name or (('@'+c.real_username) if c.real_username else '') or c.phone or c.alias or 'Telegram User'
        if label.startswith('Customer-'):
            label = 'Telegram User'
    else:
        label = f'Customer-{c.id:05d}'
    return {'id':c.id,'alias':label,'created_at':utc_iso(c.created_at)}

def redact(text: str):
    # Best-effort privacy filter. It cannot reliably identify all personal data, names, photos, voice notes or vCards.
    text = re.sub(r'(?<!\w)@[A-Za-z][A-Za-z0-9_]{4,}', '[TELEGRAM_USERNAME_HIDDEN]', text)
    text = re.sub(r'(?<!\w)\+?\d[\d ()-]{7,}\d', '[PHONE_NUMBER_HIDDEN]', text)
    return text

@app.get('/api/me')
def me(user: User = Depends(current_user)):
    return {'id':user.id,'public_id':user.public_id,'username':user.username,'role':user.role,'team_id':user.team_id,'totp_enabled':user.totp_enabled,'permissions':user_permissions(user)}

@app.post('/api/presence/heartbeat')
def presence_heartbeat(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    key = request.headers.get('x-session-key') or 'default'
    touch_presence(db, user, key)
    return {'ok': True, 'server_time': utc_iso(datetime.now(timezone.utc))}

@app.post('/api/presence/channel')
def presence_channel(data: dict, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    channel = str(data.get('channel') or 'Telegram')[:40]
    allowed = {'Telegram', 'WhatsApp', 'Instagram', 'TikTok', 'LINE'}
    if channel not in allowed:
        raise HTTPException(400, 'Unsupported social channel')
    key = request.headers.get('x-session-key') or 'default'
    row = db.query(PresenceSession).filter_by(user_id=user.id, session_key=key).first()
    if not row:
        row = PresenceSession(user_id=user.id, session_key=key, channel=channel)
        db.add(row)
    row.channel = channel
    row.last_seen = datetime.now(timezone.utc)
    row.is_active = True
    db.commit()
    return {'ok': True, 'channel': channel}

@app.post('/api/presence/logout')
def presence_logout(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    key = request.headers.get('x-session-key') or 'default'
    row = db.query(PresenceSession).filter_by(user_id=user.id, session_key=key, is_active=True).first()
    if row:
        row.logout_at = datetime.now(timezone.utc)
        row.last_seen = row.logout_at
        row.is_active = False
        db.commit()
    audit(db, user, 'auth.logout', 'user', user.id)
    return {'ok': True}

@app.post('/api/security/2fa/setup')
def setup_2fa(db: Session = Depends(get_db), user: User = Depends(admin)):
    if user.totp_enabled:
        raise HTTPException(409, '2FA is already enabled on this account.')
    secret = pyotp.random_base32()
    try:
        user.totp_secret = encrypt_session(secret)
        db.commit()
    except Exception as exc:
        db.rollback()
        print('2FA setup storage error:', repr(exc))
        raise HTTPException(503, '2FA secret could not be stored. Check PostgreSQL users.totp_secret column is VARCHAR(255) and SESSION_ENCRYPTION_KEY is a valid unchanged Fernet key.')
    account_label = f"X-Wallet · {user.username}"
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.username, issuer_name='X-Wallet')
    qr = qrcode.QRCode(box_size=4, border=2)
    qr.add_data(uri); qr.make(fit=True)
    image = qr.make_image(fill_color='black', back_color='white')
    buffer = BytesIO(); image.save(buffer, format='PNG')
    qr_data = 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')
    return {'secret':secret, 'otpauth_uri':uri, 'qr_data_uri':qr_data, 'account_label':account_label, 'issuer':'X-Wallet'}

@app.post('/api/security/2fa/confirm')
def confirm_2fa(data: TwoFactorCodeIn, db: Session = Depends(get_db), user: User = Depends(admin)):
    if not user.totp_secret:
        raise HTTPException(400, 'Start 2FA setup first and scan the QR code.')
    if not pyotp.TOTP(decrypt_session(user.totp_secret)).verify(data.code.strip(), valid_window=1):
        raise HTTPException(400, 'The 2FA code is incorrect. Please scan the QR code and try again.')
    user.totp_enabled = True
    db.commit()
    return {'ok':True, 'totp_enabled':True}

@app.post('/api/security/2fa/disable')
def disable_2fa(data: TwoFactorCodeIn, db: Session = Depends(get_db), user: User = Depends(admin)):
    if not user.totp_enabled or not user.totp_secret:
        raise HTTPException(400, '2FA is not enabled.')
    if not pyotp.TOTP(decrypt_session(user.totp_secret)).verify(data.code.strip(), valid_window=1):
        raise HTTPException(400, 'Invalid 2FA code.')
    user.totp_enabled = False
    user.totp_secret = None
    db.commit()
    return {'ok':True, 'totp_enabled':False}

@app.post('/api/admin/users/{uid}/2fa/setup')
def admin_setup_user_2fa(uid: int, db: Session = Depends(get_db), actor: User = Depends(admin)):
    require_permission(actor, 'users.manage_2fa')
    target = db.get(User, uid)
    if not target or not target.active:
        raise HTTPException(404, 'Active user not found')
    if target.role in ('admin', 'superadmin') and actor.role != 'superadmin':
        raise HTTPException(403, 'Only the super administrator can manage administrator 2FA')
    secret = pyotp.random_base32()
    try:
        target.totp_secret = encrypt_session(secret)
        target.totp_enabled = False
        db.commit()
    except Exception as exc:
        db.rollback()
        print('Employee 2FA setup storage error:', repr(exc))
        raise HTTPException(503, '2FA secret could not be stored. Check PostgreSQL users.totp_secret column is VARCHAR(255) and SESSION_ENCRYPTION_KEY is a valid unchanged Fernet key.')
    issuer = 'X-Wallet'
    account_name = f'{issuer} ({target.username})'
    uri = pyotp.TOTP(secret).provisioning_uri(name=target.username, issuer_name=issuer)
    qr = qrcode.QRCode(box_size=4, border=2)
    qr.add_data(uri); qr.make(fit=True)
    image = qr.make_image(fill_color='black', back_color='white')
    buffer = BytesIO(); image.save(buffer, format='PNG')
    qr_data = 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')
    audit(db, actor, 'user.2fa.setup_started', 'user', uid)
    return {'qr_data_uri': qr_data, 'secret': secret, 'account_label': account_name}

@app.post('/api/admin/users/{uid}/2fa/confirm')
def admin_confirm_user_2fa(uid: int, data: TwoFactorCodeIn, db: Session = Depends(get_db), actor: User = Depends(admin)):
    require_permission(actor, 'users.manage_2fa')
    target = db.get(User, uid)
    if not target or not target.active or not target.totp_secret:
        raise HTTPException(404, '2FA setup was not found for this active account')
    if target.role in ('admin', 'superadmin') and actor.role != 'superadmin':
        raise HTTPException(403, 'Only the super administrator can manage administrator 2FA')
    if not pyotp.TOTP(decrypt_session(target.totp_secret)).verify(data.code.strip(), valid_window=1):
        raise HTTPException(400, 'The 2FA code is incorrect. Scan the QR code and try again.')
    target.totp_enabled = True
    db.commit()
    audit(db, actor, 'user.2fa.enabled', 'user', uid)
    return {'ok': True, 'totp_enabled': True}

@app.post('/api/admin/users/{uid}/2fa/reset')
def admin_reset_user_2fa(uid: int, db: Session = Depends(get_db), actor: User = Depends(superadmin)):
    target = db.get(User, uid)
    if not target:
        raise HTTPException(404, 'User not found')
    if target.id == actor.id:
        raise HTTPException(400, 'Use your own 2FA settings to change your super administrator authenticator')
    target.totp_secret = None
    target.totp_enabled = False
    db.commit()
    audit(db, actor, 'user.2fa.reset', 'user', uid)
    return {'ok': True, 'totp_enabled': False, 'username': target.username}

@app.get('/api/admin/users')
def list_users(db: Session = Depends(get_db), actor: User = Depends(admin)):
    require_any_permission(actor, 'users.create_employee','users.create_admin','users.manage_status','users.delete','users.reset_password','users.manage_team','users.manage_2fa','customers.assign')
    query = db.query(User)
    if actor.role != 'superadmin':
        query = query.filter(User.role != 'superadmin')
    return [{'id':u.id,'public_id':u.public_id,'username':(u.username[len(f'deleted_{u.id}_'):] if u.username.startswith(f'deleted_{u.id}_') else u.username),'deleted':u.username.startswith(f'deleted_{u.id}_'),'role':u.role,'team_id':u.team_id,'active':u.active,'totp_enabled':u.totp_enabled,'created_at':u.created_at.isoformat(),'last_seen':u.last_seen.isoformat() if u.last_seen else None,'created_by_user_id':u.created_by_user_id,'permissions':user_permissions(u)} for u in query.order_by(User.id).all()]

@app.post('/api/admin/users')
def create_user(data: UserIn, request: Request, db: Session = Depends(get_db), actor: User = Depends(admin)):
    username = data.username.strip()
    if data.role == 'admin':
        require_permission(actor, 'users.create_admin')
    else:
        require_permission(actor, 'users.create_employee')
    if not username or len(username) > 80:
        raise HTTPException(400, 'Username must contain 1 to 80 characters')
    invalid_permissions=sorted(set(data.permissions)-ADMIN_PERMISSION_KEYS)
    if invalid_permissions:
        raise HTTPException(400, 'Unknown permissions: '+', '.join(invalid_permissions))
    if actor.role != 'superadmin' and data.role == 'admin':
        not_delegable = sorted(set(data.permissions) - set(user_permissions(actor)))
        if not_delegable:
            raise HTTPException(403, 'You cannot grant permissions you do not have: ' + ', '.join(not_delegable))
    if data.role not in ('admin', 'employee', 'chatting', 'finding'):
        raise HTTPException(400, 'Role must be Administrator, Chatting, or Finding')
    if len(data.password) < 10:
        raise HTTPException(400, 'Password must be at least 10 characters')
    if len(data.password.encode('utf-8')) > 72:
        raise HTTPException(400, 'Password must be no more than 72 UTF-8 bytes for the current password-hash algorithm')
    if db.query(User).filter(func.lower(User.username) == username.lower()).first():
        raise HTTPException(409, 'Username already exists (usernames are case-insensitive)')
    resolved_team_id = data.team_id
    if resolved_team_id is None and getattr(data, 'auto_team', True):
        team_name = {'chatting': 'Chatting', 'finding': 'Finding', 'admin': 'Admin'}.get(data.role)
        if team_name:
            matched_team = db.query(Team).filter(func.lower(Team.name) == team_name.lower()).first()
            resolved_team_id = matched_team.id if matched_team else None
    if resolved_team_id is not None and not db.get(Team, resolved_team_id):
        raise HTTPException(404, 'Team not found')
    try:
        u = User(username=username, public_id=make_public_user_id(db),
                 password_hash=hash_password(data.password), role=data.role,
                 team_id=resolved_team_id, created_by_user_id=actor.id, active=True,
                 permissions=json.dumps(sorted({p for p in data.permissions if p in ADMIN_PERMISSION_KEYS}) if data.role == 'admin' else []))
        db.add(u)
        db.flush()
        db.commit()
        db.refresh(u)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, 'Username or public user ID already exists. Please retry.') from exc
    except Exception as exc:
        db.rollback()
        print('employee creation failed:', repr(exc))
        raise HTTPException(500, 'Employee account could not be created. Check Railway application logs and database connectivity.') from exc
    audit(db, actor, 'user.create', 'user', u.id)
    from urllib.parse import quote
    base_url = (settings.app_base_url or 'https://xwalletchatserver-production.up.railway.app').rstrip('/')
    login_prefix = '/admin-login/' if u.role == 'admin' else ('/finding/' if u.role == 'finding' else '/login/')
    login_path = login_prefix + quote(u.username, safe='')
    login_url = base_url + login_path
    return {'id': u.id, 'public_id': u.public_id, 'username': u.username,
            'role': u.role, 'permissions': user_permissions(u), 'login_url': login_url, 'needs_custom_domain': False}

@app.patch('/api/admin/users/{uid}')
def update_user(uid:int, active:bool, db:Session=Depends(get_db), actor:User=Depends(admin)):
    require_permission(actor, 'users.manage_status')
    u=db.get(User,uid)
    if not u: raise HTTPException(404,'User not found')
    if u.role == 'superadmin' or (actor.role != 'superadmin' and u.role == 'admin'):
        raise HTTPException(403, 'You cannot change the status of this administrator account')
    if u.id == actor.id and not active:
        raise HTTPException(400, 'You cannot disable your own account')
    if active and u.username.startswith(f'deleted_{u.id}_'):
        restored=u.username[len(f'deleted_{u.id}_'):]
        if db.query(User).filter(func.lower(User.username)==restored.lower(), User.id!=u.id).first():
            raise HTTPException(409, 'Cannot restore original username because it is already in use')
        u.username=restored
    u.active=active
    if not active:
        for session in db.query(PresenceSession).filter_by(user_id=u.id, is_active=True).all():
            session.is_active=False; session.logout_at=datetime.now(timezone.utc)
    db.commit(); audit(db,actor,'user.active','user',uid,str(active)); return {'ok':True,'active':u.active}

@app.put('/api/admin/users/{uid}/permissions')
def update_user_permissions(uid:int, data:UserPermissionsIn, db:Session=Depends(get_db), actor:User=Depends(superadmin)):
    target=db.get(User,uid)
    if not target: raise HTTPException(404,'User not found')
    if target.role != 'admin': raise HTTPException(400,'Permissions can only be assigned to administrator accounts')
    invalid=sorted(set(data.permissions)-ADMIN_PERMISSION_KEYS)
    if invalid: raise HTTPException(400,'Unknown permissions: '+', '.join(invalid))
    target.permissions=json.dumps(sorted(set(data.permissions)))
    db.commit(); audit(db,actor,'user.permissions_update','user',uid,','.join(sorted(set(data.permissions))))
    return {'ok':True,'permissions':user_permissions(target)}

@app.post('/api/admin/users/{uid}/password')
def reset_user_password(uid:int, data:dict, db:Session=Depends(get_db), actor:User=Depends(admin)):
    require_permission(actor, 'users.reset_password')
    u=db.get(User,uid)
    password=data.get('password','')
    if not u: raise HTTPException(404,'User not found')
    if actor.role != 'superadmin' and u.role in ('admin','superadmin'):
        raise HTTPException(403, 'You cannot reset this administrator password')
    if len(password)<10: raise HTTPException(400,'Password must be at least 10 characters')
    u.password_hash=hash_password(password); db.commit(); audit(db,actor,'user.password_reset','user',uid)
    return {'ok':True}

@app.delete('/api/admin/users/{uid}')
def delete_user(uid:int, db:Session=Depends(get_db), actor:User=Depends(admin)):
    require_permission(actor, 'users.delete')
    u=db.get(User,uid)
    if not u: raise HTTPException(404,'User not found')
    if u.id==actor.id: raise HTTPException(400,'Cannot delete yourself')
    if u.role == 'superadmin' or (actor.role != 'superadmin' and u.role == 'admin'):
        raise HTTPException(403, 'You cannot delete this administrator account')
    if not u.username.startswith(f'deleted_{u.id}_'):
        u.username=f'deleted_{u.id}_{u.username}'
    u.active=False
    for session in db.query(PresenceSession).filter_by(user_id=u.id, is_active=True).all():
        session.is_active=False; session.logout_at=datetime.now(timezone.utc)
    db.commit(); audit(db,actor,'user.soft_delete','user',uid); return {'ok':True,'note':'Soft deleted; historical records retained'}

@app.post('/api/admin/teams')
def create_team(data:TeamIn, db:Session=Depends(get_db), actor:User=Depends(admin)):
    require_permission(actor, 'teams.manage')
    if db.query(Team).filter_by(name=data.name).first(): raise HTTPException(409,'Team exists')
    t=Team(name=data.name); db.add(t); db.commit(); db.refresh(t); audit(db,actor,'team.create','team',t.id); return {'id':t.id,'name':t.name}

@app.get('/api/admin/teams')
def list_teams(db:Session=Depends(get_db), actor:User=Depends(admin)):
    require_any_permission(actor, 'teams.manage','users.manage_team','users.create_employee','users.create_admin')
    return [{'id':t.id,'name':t.name} for t in db.query(Team).all()]

@app.patch('/api/admin/users/{uid}/team')
def set_user_team(uid: int, data: dict, db: Session = Depends(get_db), actor: User = Depends(admin)):
    require_permission(actor, 'users.manage_team')
    user = db.get(User, uid)
    if not user:
        raise HTTPException(404, 'User not found')
    if actor.role != 'superadmin' and user.role in ('admin','superadmin'):
        raise HTTPException(403, 'You cannot change this administrator team')
    team_id = data.get('team_id')
    if team_id is not None and not db.get(Team, int(team_id)):
        raise HTTPException(404, 'Team not found')
    user.team_id = int(team_id) if team_id is not None else None
    db.commit()
    audit(db, actor, 'user.team_update', 'user', uid, f'team_id={user.team_id}')
    return {'ok': True, 'team_id': user.team_id}

@app.post('/api/admin/telegram/begin')
async def telegram_begin(data:PhoneIn, actor:User=Depends(admin)):
    require_permission(actor, 'telegram.connect')
    try: return {'phone_code_hash':await tg.begin_login(data.phone)}
    except Exception as e: raise HTTPException(400,str(e))

@app.post('/api/admin/telegram/complete')
async def telegram_complete(data:CodeIn, db:Session=Depends(get_db), actor:User=Depends(admin)):
    require_permission(actor, 'telegram.connect')
    try:
        result=await tg.finish_login(data.phone_code_hash,data.code,data.password)
        if result.get('account_id'): audit(db,actor,'telegram.account.connect','telegram_account',result['account_id'])
        return result
    except Exception as e: raise HTTPException(400,str(e))

@app.get('/api/admin/app-url')
def admin_app_url(actor: User = Depends(admin)):
    require_permission(actor, 'users.create_employee')
    return {'base_url': 'https://xwalletchatserver-production.up.railway.app'}

@app.get('/api/admin/telegram/accounts')
def telegram_accounts(db:Session=Depends(get_db), actor:User=Depends(admin)):
    if actor.role != 'superadmin' and not ({'telegram.connect','telegram.sync','customers.assign'} & set(user_permissions(actor))): raise HTTPException(403, 'Missing Telegram account permission')
    rows = db.query(TgAccount).order_by(TgAccount.id).all()
    owner_ids = {a.assigned_user_id for a in rows if a.assigned_user_id}
    owners = {u.id: u for u in db.query(User).filter(User.id.in_(owner_ids)).all()} if owner_ids else {}
    counts = dict(db.query(Customer.account_id, func.count(Customer.id)).group_by(Customer.account_id).all())
    return [{'id':a.id,'label':a.label,'phone_masked':a.phone_masked,'status':a.status,
             'created_at':utc_iso(a.created_at),'customer_count':counts.get(a.id,0),
             'assigned_user_id':a.assigned_user_id,
             'assigned_username':owners[a.assigned_user_id].username if a.assigned_user_id in owners else None}
            for a in rows]

@app.put('/api/admin/telegram/accounts/{account_id}/owner')
def set_telegram_account_owner(account_id: int, data: dict, db: Session = Depends(get_db), actor: User = Depends(admin)):
    require_permission(actor, 'telegram.connect')
    account = db.get(TgAccount, account_id)
    if not account:
        raise HTTPException(404, 'Telegram account not found')
    raw_owner = data.get('user_id')
    owner = db.get(User, int(raw_owner)) if raw_owner not in (None, '', 0, '0') else None
    if raw_owner not in (None, '', 0, '0'):
        if not owner or not owner.active or owner.role not in ('employee','chatting','finding'):
            raise HTTPException(400, 'Choose an active employee account (Chatting or Finding)')
        if actor.role != 'superadmin' and owner.role == 'admin':
            raise HTTPException(403, 'Administrators cannot assign Telegram accounts to administrator accounts')
    account.assigned_user_id = owner.id if owner else None
    db.commit()
    audit(db, actor, 'telegram.account.owner_update', 'telegram_account', account_id,
          f'assigned_user_id={account.assigned_user_id}')
    return {'ok': True, 'assigned_user_id': account.assigned_user_id,
            'assigned_username': owner.username if owner else None}

@app.post('/api/admin/telegram/accounts/{account_id}/sync')
async def admin_sync_telegram_account(account_id: int, db: Session = Depends(get_db), actor: User = Depends(admin)):
    require_permission(actor, 'telegram.sync')
    account = db.get(TgAccount, account_id)
    if not account:
        raise HTTPException(404, 'Telegram account not found')
    client = tg._clients.get(account_id)
    if not client or not client.is_connected():
        raise HTTPException(409, 'Telegram account is not connected. Reconnect this account first.')
    # Queue Telegram network work and return immediately; the UI can keep responding
    # while Telegram enumerates dialogs and downloads history.
    async def run_account_sync(aid, telegram_client):
        try:
            result = await tg.sync_dialogs(aid, telegram_client, sync_messages=False)
            with SessionLocal() as sync_db:
                count = sync_db.query(Customer).filter_by(account_id=aid).count()
                audit(sync_db, actor, 'telegram.account.sync', 'telegram_account', aid,
                      f'customers={count}; dialogs={result.get("dialog_count", 0)}')
            await tg.sync_dialog_messages(aid, telegram_client)
        except Exception as exc:
            print('background Telegram account sync failed:', aid, repr(exc))
    asyncio.create_task(run_account_sync(account_id, client))
    count = db.query(Customer).filter_by(account_id=account_id).count()
    return {'ok': True, 'queued': True, 'customer_count': count,
            'message': 'Telegram sync was queued. Existing customers remain available while new chats and history are imported in the background.'}

@app.get('/api/admin/telegram/accounts/{account_id}/profile')
async def admin_telegram_account_profile(account_id: int, db: Session = Depends(get_db), actor: User = Depends(admin)):
    if actor.role != 'superadmin' and not ({'telegram.connect','telegram.sync'} & set(user_permissions(actor))): raise HTTPException(403, 'Missing Telegram account permission')
    account = db.get(TgAccount, account_id)
    if not account:
        raise HTTPException(404, 'Telegram account not found.')
    client = tg._clients.get(account_id)
    if not client or not client.is_connected():
        raise HTTPException(409, 'Telegram account is not connected.')
    me = await client.get_me()
    name = ' '.join(filter(None, [getattr(me, 'first_name', ''), getattr(me, 'last_name', '')])).strip()
    if name and account.label != name[:120]:
        account.label = name[:120]
        db.commit()

    # Avoid filesystem writes: Railway logs showed OSError(28), no space left on device.
    avatar_available = account_id in _TELEGRAM_ACCOUNT_AVATAR_CACHE
    try:
        photo_bytes = await client.download_profile_photo(me, file=bytes)
        if photo_bytes:
            _TELEGRAM_ACCOUNT_AVATAR_CACHE[account_id] = photo_bytes
            avatar_available = True
    except Exception as exc:
        print('telegram account avatar sync (memory cache):', repr(exc))
        avatar_available = avatar_available or os.path.isfile(os.path.join(UPLOAD_ROOT, f'tg_account_{account_id}_avatar.jpg'))

    return {
        'id': getattr(me, 'id', None),
        'name': name or account.label or str(account_id),
        'username': getattr(me, 'username', '') or '',
        'phone': getattr(me, 'phone', '') or '',
        'avatar_available': avatar_available,
        'avatar_url': f'/api/admin/telegram/accounts/{account_id}/avatar?v={int(datetime.now().timestamp())}' if avatar_available else None
    }

@app.get('/api/admin/telegram/accounts/{account_id}/avatar')
def admin_telegram_account_avatar(account_id: int, actor: User = Depends(admin)):
    if actor.role != 'superadmin' and not ({'telegram.connect','telegram.sync'} & set(user_permissions(actor))): raise HTTPException(403, 'Missing Telegram account permission')
    photo = _TELEGRAM_ACCOUNT_AVATAR_CACHE.get(account_id)
    if photo:
        return Response(content=photo, media_type='image/jpeg',
                        headers={'Cache-Control': 'no-store, max-age=0'})
    path = os.path.join(UPLOAD_ROOT, f'tg_account_{account_id}_avatar.jpg')
    if os.path.isfile(path):
        return FileResponse(path, media_type='image/jpeg',
                            headers={'Cache-Control': 'no-store, max-age=0'})
    raise HTTPException(404, 'Telegram avatar is not cached. Refresh the connected account profile.')

@app.get('/api/admin/customers')
def admin_customers(account_id:int|None=None, db:Session=Depends(get_db), actor:User=Depends(admin)):
    require_any_permission(actor, 'customers.assign','telegram.sync')
    q=db.query(Customer)
    if account_id is not None:
        q=q.filter(Customer.account_id==account_id)
    rows=q.order_by(Customer.account_id, Customer.real_name, Customer.real_username).all()
    result=[]
    for c in rows:
        assigned=[]
        for a in db.query(Assignment).filter_by(customer_id=c.id,active=True).all():
            u=db.get(User,a.user_id)
            if u: assigned.append({'user_id':u.public_id,'username':u.username})
        result.append({'id':c.id,'alias':c.alias or c.real_name or ('@'+c.real_username if c.real_username else 'Telegram User'),
            'telegram_username':c.real_username,'telegram_user_id':c.telegram_peer_id,'name':c.real_name,
            'phone':c.phone,'account_id':c.account_id,'avatar_available':bool(c.avatar_path),
            'dialog_folder':c.dialog_folder,'is_archived':c.dialog_folder == 1,
            'is_saved_messages':bool(c.is_saved_messages),'assigned_to':assigned})
    return result

@app.get('/api/admin/customers/{cid}/avatar')
def admin_customer_avatar(cid:int, db:Session=Depends(get_db), actor:User=Depends(admin)):
    require_any_permission(actor, 'customers.assign','telegram.sync')
    c=db.get(Customer,cid)
    if not c or not c.avatar_path: raise HTTPException(404,'Telegram profile photo is not available')
    path=safe_upload_path(c.avatar_path)
    return FileResponse(path, media_type=mimetypes.guess_type(path)[0] or 'image/jpeg')

@app.post('/api/admin/assign')
def assign(data:AssignIn, db:Session=Depends(get_db), actor:User=Depends(admin)):
    require_permission(actor, 'customers.assign')
    c=db.get(Customer,data.customer_id); u=db.get(User,data.user_id)
    if c and c.is_saved_messages: raise HTTPException(403, 'Saved Messages is private to administrators and cannot be assigned to employees.')
    if not c or not u or not u.active: raise HTTPException(404,'Customer or active user not found')
    if u.role not in ('employee', 'chatting', 'finding'): raise HTTPException(400, 'Customers can only be assigned to Chatting or Finding employees')
    # Reassignment revokes old active assignments but never deletes history/messages/tags.
    old=db.query(Assignment).filter_by(customer_id=c.id,active=True).all()
    for a in old: a.active=False; a.revoked_at=datetime.now(timezone.utc)
    assignment=Assignment(customer_id=c.id,user_id=u.id,assigned_by=actor.id,active=True); db.add(assignment); db.flush()
    db.add(InboxNotification(user_id=u.id, kind='customer_assigned', title='New customer assigned', body=f'A customer conversation has been assigned to you: {c.alias or ("Customer #"+str(c.id))}. Please open Chat Workspace.', customer_id=c.id))
    db.commit(); audit(db,actor,'customer.assign','customer',c.id,f'to_user={u.id}'); return {'ok':True,'history_preserved':True,'customer_id':c.id,'user_id':u.id,'public_user_id':u.public_id,'username':u.username,'assignment_id':assignment.id}

@app.post('/api/admin/assign/bulk')
def bulk_assign(data:BulkAssignIn, db:Session=Depends(get_db), actor:User=Depends(admin)):
    require_permission(actor, 'customers.assign')
    u=db.get(User,data.user_id)
    if not u or not u.active: raise HTTPException(404,'Active user not found')
    if u.role not in ('employee', 'chatting', 'finding'): raise HTTPException(400, 'Customers can only be assigned to Chatting or Finding employees')
    if not data.customer_ids: raise HTTPException(400, 'Select at least one customer')
    if len(data.customer_ids) > 500: raise HTTPException(400, 'Bulk assignment is limited to 500 customers at a time')
    selected = db.query(Customer).filter(Customer.id.in_(data.customer_ids)).all()
    if any(c.is_saved_messages for c in selected): raise HTTPException(403, 'Saved Messages cannot be assigned to employees.')
    if len(selected) != len(set(data.customer_ids)) or any(c.account_id != data.account_id for c in selected):
        raise HTTPException(400, 'Every selected customer must belong to the currently selected Telegram account')
    for cid in data.customer_ids:
        c=db.get(Customer,cid)
        if not c: continue
        for a in db.query(Assignment).filter_by(customer_id=cid,active=True).all(): a.active=False; a.revoked_at=datetime.now(timezone.utc)
        db.add(Assignment(customer_id=cid,user_id=u.id,assigned_by=actor.id,active=True))
        db.add(InboxNotification(user_id=u.id, kind='customer_assigned', title='New customer assigned', body=f'A customer conversation has been assigned to you: {c.alias or ("Customer #"+str(c.id))}. Please open Chat Workspace.', customer_id=cid))
    db.commit(); audit(db,actor,'customer.bulk_assign','user',u.id,f'count={len(data.customer_ids)}'); return {'ok':True,'count':len(data.customer_ids)}

@app.post('/api/admin/revoke/{customer_id}')
def revoke(customer_id:int, db:Session=Depends(get_db), actor:User=Depends(admin)):
    require_permission(actor, 'customers.assign')
    rows=db.query(Assignment).filter_by(customer_id=customer_id,active=True).all()
    for a in rows: a.active=False; a.revoked_at=datetime.now(timezone.utc)
    db.commit(); audit(db,actor,'customer.revoke','customer',customer_id); return {'ok':True,'revoked':len(rows),'history_preserved':True}

@app.get('/api/notifications')
def get_notifications(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.query(InboxNotification).filter_by(user_id=user.id, is_read=False).order_by(desc(InboxNotification.id)).limit(30).all()
    return [{'id':n.id,'kind':n.kind,'title':n.title,'body':n.body,'customer_id':n.customer_id,'created_at':utc_iso(n.created_at)} for n in rows]

@app.post('/api/notifications/read')
def mark_notifications_read(data: dict, db: Session = Depends(get_db), user: User = Depends(current_user)):
    ids = [int(v) for v in (data.get('ids') or []) if str(v).isdigit()][:100]
    if ids:
        db.query(InboxNotification).filter(InboxNotification.user_id==user.id, InboxNotification.id.in_(ids)).update({InboxNotification.is_read: True}, synchronize_session=False)
        db.commit()
    return {'ok': True, 'count': len(ids)}

@app.get('/api/inbox')
def inbox(filter:str='all', db:Session=Depends(get_db), user:User=Depends(current_user)):
    q=db.query(Customer)
    if user.role not in ('superadmin','admin'):
        q=q.filter(Customer.is_saved_messages.is_(False))
        ids=[a.customer_id for a in db.query(Assignment).filter_by(user_id=user.id,active=True).all()]
        q=q.filter(Customer.id.in_(ids) if ids else False)
    result=[]
    for c in q.all():
        last=db.query(Message).filter_by(customer_id=c.id).order_by(desc(Message.sent_at)).first()
        unread=0
        if user.role not in ('superadmin','admin'):
            state=db.query(ReadState).filter_by(customer_id=c.id,user_id=user.id).first()
            last_read=state.last_read_message_id if state else 0
            unread=db.query(Message).filter(Message.customer_id==c.id,Message.direction=='in',Message.id>last_read).count()
        if filter=='unread' and unread==0: continue
        if filter=='unreplied' and (not last or last.direction!='in'): continue
        result.append({**safe_customer(c,user),'last_message':redact(last.body)[:180] if last else '', 'last_message_at':last.sent_at.isoformat() if last else None,'unread':unread,'account_id':c.account_id})
    return sorted(result,key=lambda x:x['last_message_at'] or '',reverse=True)

@app.get('/api/customers/{cid}/messages')
def messages(cid:int, limit:int=200, before:int|None=None, db:Session=Depends(get_db), user:User=Depends(current_user)):
    c=db.get(Customer,cid)
    if not c or not can_access(db,user,cid): raise HTTPException(404,'Customer not found')
    q=db.query(Message).filter_by(customer_id=cid)
    if before: q=q.filter(Message.id<before)
    rows=q.order_by(desc(Message.id)).limit(min(max(limit,1),500)).all()[::-1]
    # Never serialize Telegram IDs, peer IDs, usernames, phone numbers or access hashes to employees.
    return [{
        'id':m.id,
        'direction':m.direction,
        'text':redact(m.body) if user.role not in ('admin','superadmin') else m.body,
        'sent_at':utc_iso(m.sent_at),
        'sender':m.sender_label,
        'media_available':bool((m.media_path and os.path.isfile(os.path.join(UPLOAD_ROOT,m.media_path))) or m.media_pending),
        'media_kind':m.media_type or (media_kind(m.media_path) if m.media_path else None),
        'media_url':f'/api/messages/{m.id}/media' if ((m.media_path and os.path.isfile(os.path.join(UPLOAD_ROOT,m.media_path))) or m.media_pending) else None,
        'transcript':m.transcript or ''
    } for m in rows]

@app.get('/api/messages/{mid}/media')
async def message_media(mid:int, db:Session=Depends(get_db), user:User=Depends(current_user)):
    msg=db.get(Message,mid)
    if not msg or not can_access(db,user,msg.customer_id):
        raise HTTPException(404,'Media not found')
    path = ''
    if msg.media_path:
        try:
            path = safe_upload_path(msg.media_path)
        except HTTPException:
            path = ''
    # Lazy recovery: if the file was missed during an earlier sync or the volume
    # was full, fetch the original Telegram message when the user opens it.
    if not path:
        customer = db.get(Customer, msg.customer_id)
        account_id = customer.account_id if customer else None
        client = tg._clients.get(account_id) if account_id else None
        if client and client.is_connected() and msg.telegram_message_id:
            try:
                peer = await client.get_input_entity(int(customer.telegram_peer_id))
                original = await client.get_messages(peer, ids=int(msg.telegram_message_id))
                if original and getattr(original, 'media', None):
                    msg.media_type = tg.message_media_kind(original)
                    msg.media_pending = True
                    recovered = await tg.save_message_media(account_id, original, customer.id)
                    if recovered and os.path.isfile(recovered) and os.path.getsize(recovered) > 0:
                        msg.media_path = os.path.relpath(recovered, UPLOAD_ROOT)
                        msg.media_pending = False
                        db.commit()
                        path = safe_upload_path(msg.media_path)
            except Exception as exc:
                db.rollback()
                print('telegram lazy media recovery:', repr(exc))
    if not path:
        raise HTTPException(404,'Media is not available locally and could not be recovered from Telegram. Check the Telegram connection and Railway volume.')
    guessed = mimetypes.guess_type(path)[0] or 'application/octet-stream'
    ext = os.path.splitext(path)[1].lower()
    if ext in ('.ogg','.oga','.opus','.mp3','.m4a','.wav','.aac','.flac','.3gp') and not guessed.startswith('audio/'):
        guessed = 'audio/ogg' if ext in ('.ogg','.oga','.opus') else 'audio/mpeg'
    if ext in ('.mp4','.mov','.m4v','.webm','.mkv','.avi','.3gp') and not guessed.startswith('video/'):
        guessed = 'video/mp4' if ext != '.webm' else 'video/webm'
    return FileResponse(path, media_type=guessed, headers={'Content-Disposition': 'inline; filename=\"' + os.path.basename(path).replace('\"','') + '\"'})

@app.post('/api/messages/{mid}/transcribe')
async def transcribe_message(mid:int, db:Session=Depends(get_db), user:User=Depends(current_user)):
    msg=db.get(Message,mid)
    if not msg or not can_access(db,user,msg.customer_id) or not msg.media_path:
        raise HTTPException(404,'Voice message not found')
    if msg.transcript:
        return {'ok':True,'transcript':msg.transcript,'cached':True}
    path=safe_upload_path(msg.media_path)
    kind=media_kind(msg.media_path)
    if kind != 'audio':
        raise HTTPException(415,'Only voice/audio messages can be transcribed.')
    if not settings.openai_api_key:
        raise HTTPException(503,'Voice transcription is not configured. Add OPENAI_API_KEY to Railway Variables, then redeploy.')
    size=os.path.getsize(path)
    if size > 25*1024*1024:
        raise HTTPException(413,'This audio file is larger than the 25 MB transcription limit.')
    try:
        import httpx
        async with httpx.AsyncClient(timeout=180) as client:
            with open(path,'rb') as audio_file:
                response=await client.post(
                    'https://api.openai.com/v1/audio/transcriptions',
                    headers={'Authorization':f'Bearer {settings.openai_api_key}'},
                    data={'model':settings.transcription_model,'response_format':'json'},
                    files={'file':(os.path.basename(path),audio_file,mimetypes.guess_type(path)[0] or 'audio/ogg')}
                )
        if response.status_code >= 400:
            detail='Voice transcription provider rejected the request.'
            try: detail=response.json().get('error',{}).get('message') or detail
            except Exception: pass
            raise HTTPException(response.status_code if response.status_code < 500 else 502,detail)
        transcript=(response.json().get('text') or '').strip()
        if not transcript:
            raise HTTPException(422,'No speech was detected in this audio.')
        msg.transcript=transcript
        db.commit()
        return {'ok':True,'transcript':transcript,'cached':False}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502,'Voice transcription failed: '+str(exc)[:240])

@app.post('/api/customers/{cid}/send')
async def send(cid:int,data:SendIn, db:Session=Depends(get_db), user:User=Depends(current_user)):
    c=db.get(Customer,cid)
    if not c or not can_access(db,user,cid): raise HTTPException(404,'Customer not found')
    try: sent=await tg.send_message(c.account_id,c,data.text)
    except Exception as e: raise HTTPException(400,str(e))
    m=db.query(Message).filter_by(customer_id=cid,telegram_message_id=str(sent.id)).first()
    if not m:
        m=Message(customer_id=cid,telegram_message_id=str(sent.id),direction='out',body=data.text,sent_at=sent.date or datetime.now(timezone.utc),sender_label=user.username)
        db.add(m); db.commit(); db.refresh(m)
    return {'ok':True,'message':{'id':m.id,'direction':'out','text':m.body,'sent_at':utc_iso(m.sent_at),'sender':m.sender_label}}

@app.post('/api/customers/{cid}/upload')
async def upload_customer_media(cid:int, file:UploadFile=File(...), caption:str=Form(''),
                                db:Session=Depends(get_db), user:User=Depends(current_user)):
    c=db.get(Customer,cid)
    if not c or not can_access(db,user,cid): raise HTTPException(404,'Customer not found')
    ext=os.path.splitext(file.filename or '')[1].lower()
    if ext not in ALLOWED_MEDIA_EXTENSIONS:
        raise HTTPException(415,'Only photos, videos, and audio/voice files are supported.')
    content=await file.read(MAX_UPLOAD_BYTES+1)
    if not content: raise HTTPException(400,'The selected file is empty.')
    if len(content)>MAX_UPLOAD_BYTES: raise HTTPException(413,'File is larger than the 50 MB limit.')
    filename=f"upload_{uuid.uuid4().hex}{ext}"
    full=os.path.join(UPLOAD_ROOT,filename)
    with open(full,'wb') as target: target.write(content)
    try:
        sent=await tg.send_media(c.account_id,c,full,caption.strip())
        rel=os.path.relpath(full,UPLOAD_ROOT)
        msg=Message(customer_id=cid,telegram_message_id=str(sent.id),direction='out',body=caption.strip(),
                    media_path=rel,sent_at=sent.date or datetime.now(timezone.utc),sender_label=user.username)
        db.add(msg); db.commit(); db.refresh(msg)
        return {'ok':True,'message_id':msg.id}
    except Exception as exc:
        try: os.remove(full)
        except OSError: pass
        raise HTTPException(400,str(exc))

@app.post('/api/messages/{mid}/forward')
async def forward_chat_message(mid:int, data:ForwardIn, db:Session=Depends(get_db), user:User=Depends(current_user)):
    source_msg=db.get(Message,mid)
    if not source_msg or not can_access(db,user,source_msg.customer_id):
        raise HTTPException(404,'Source message not found')
    source=db.get(Customer,source_msg.customer_id)
    target=db.get(Customer,data.target_customer_id)
    if not target or not can_access(db,user,target.id):
        raise HTTPException(404,'Target customer not found')
    if source.account_id != target.account_id:
        raise HTTPException(400,'Messages can only be forwarded between customers connected to the same Telegram account.')
    try:
        sent=await tg.forward_message(source.account_id,source,target,source_msg.telegram_message_id)
        row=Message(customer_id=target.id,telegram_message_id=str(sent.id),direction='out',
                    body=source_msg.body,media_path=source_msg.media_path,
                    sent_at=sent.date or datetime.now(timezone.utc),sender_label=user.username)
        if not db.query(Message).filter_by(customer_id=target.id,telegram_message_id=str(sent.id)).first():
            db.add(row);db.commit()
        return {'ok':True,'target_customer_id':target.id}
    except Exception as exc:
        raise HTTPException(400,str(exc))

@app.post('/api/customers/{cid}/read')
def mark_read(cid:int, db:Session=Depends(get_db), user:User=Depends(current_user)):
    if not can_access(db,user,cid): raise HTTPException(404,'Customer not found')
    latest=db.query(Message).filter_by(customer_id=cid).order_by(desc(Message.id)).first()
    state=db.query(ReadState).filter_by(customer_id=cid,user_id=user.id).first()
    if not state: state=ReadState(customer_id=cid,user_id=user.id); db.add(state)
    state.last_read_message_id=latest.id if latest else 0; db.commit(); return {'ok':True}

@app.get('/api/customers/{cid}/tags')
def get_tags(cid:int, db:Session=Depends(get_db), user:User=Depends(current_user)):
    c=db.get(Customer,cid)
    if not c or not can_access(db,user,cid): raise HTTPException(404,'Customer not found')
    q=db.query(Tag).filter(Tag.customer_id==cid)
    if user.role not in ('admin','superadmin'):
        q=q.filter(((Tag.scope=='private') & (Tag.owner_user_id==user.id)) | ((Tag.scope=='team') & (Tag.team_id==user.team_id)) | (Tag.scope=='shared'))
    return [{'id':t.id,'key':t.key,'value':t.value,'scope':t.scope,'updated_at':t.updated_at.isoformat()} for t in q.all()]

@app.post('/api/customers/{cid}/tags')
def upsert_tag(cid:int,data:TagIn, db:Session=Depends(get_db), user:User=Depends(current_user)):
    c=db.get(Customer,cid)
    if not c or not can_access(db,user,cid): raise HTTPException(404,'Customer not found')
    if data.scope not in ('private','team','shared'): raise HTTPException(400,'scope must be private, team, or shared')
    if data.scope=='team' and not user.team_id and user.role not in ('admin','superadmin'): raise HTTPException(400,'Join a team before creating team tags')
    if data.scope=='shared' and user.role not in ('admin','superadmin'): owner=None
    elif user.role in ('admin','superadmin') and data.scope=='private': owner=None
    else: owner=user.id if data.scope=='private' else None
    team_id=user.team_id if data.scope=='team' else None
    tag=db.query(Tag).filter_by(customer_id=cid,scope=data.scope,owner_user_id=owner,team_id=team_id,key=data.key).first()
    if tag: tag.value=data.value
    else: tag=Tag(customer_id=cid,scope=data.scope,owner_user_id=owner,team_id=team_id,key=data.key,value=data.value); db.add(tag)
    db.commit(); db.refresh(tag); audit(db,user,'tag.upsert','tag',tag.id,data.scope); return {'id':tag.id,'key':tag.key,'value':tag.value,'scope':tag.scope}

@app.delete('/api/tags/{tag_id}')
def delete_tag(tag_id:int, db:Session=Depends(get_db), user:User=Depends(current_user)):
    tag=db.get(Tag,tag_id)
    if not tag: raise HTTPException(404,'Tag not found')
    allowed=user.role in ('admin','superadmin') or (tag.scope=='private' and tag.owner_user_id==user.id) or (tag.scope=='team' and tag.team_id==user.team_id)
    if not allowed: raise HTTPException(403,'Cannot delete another employee private tag')
    db.delete(tag); db.commit(); audit(db,user,'tag.delete','tag',tag_id); return {'ok':True}

@app.get('/api/admin/alerts/offline-config')
def get_employee_offline_alert_config(db: Session = Depends(get_db), actor: User = Depends(superadmin)):
    row = db.get(EmployeeAlertConfig, 1)
    chat_ids = []
    if row:
        try: chat_ids = json.loads(row.chat_ids or '[]')
        except Exception: chat_ids = []
    return {'enabled': bool(row.enabled) if row else False, 'chat_ids': chat_ids,
            'bot_token_configured': bool(row and row.bot_token_ciphertext)}

@app.put('/api/admin/alerts/offline-config')
def save_employee_offline_alert_config(data: EmployeeAlertConfigIn, db: Session = Depends(get_db), actor: User = Depends(superadmin)):
    chat_ids = list(dict.fromkeys(str(v).strip() for v in data.chat_ids if str(v).strip()))
    if any(not re.fullmatch(r'-?\d+', value) for value in chat_ids):
        raise HTTPException(400, 'Chat IDs must be numeric Telegram chat IDs.')
    if len(chat_ids) > 30:
        raise HTTPException(400, 'You can configure up to 30 Telegram chat IDs.')
    row = db.get(EmployeeAlertConfig, 1)
    if row is None:
        row = EmployeeAlertConfig(id=1, bot_token_ciphertext='', chat_ids='[]', enabled=False)
        db.add(row)
    token = (data.bot_token or '').strip()
    if token:
        if not re.fullmatch(r'\d{5,15}:[A-Za-z0-9_-]{20,}', token):
            raise HTTPException(400, 'Telegram Bot Token format is invalid.')
        try: row.bot_token_ciphertext = encrypt_session(token)
        except Exception as exc: raise HTTPException(500, f'Cannot encrypt bot token: {exc}')
    row.chat_ids = json.dumps(chat_ids)
    row.enabled = bool(data.enabled)
    db.commit()
    audit(db, actor, 'employee.offline_alert_config.update', 'settings', 'employee_offline_alert', f'enabled={row.enabled}; recipients={len(chat_ids)}')
    return {'ok': True, 'enabled': row.enabled, 'chat_ids': chat_ids, 'bot_token_configured': bool(row.bot_token_ciphertext)}

@app.get('/api/admin/attendance/config')
def get_attendance_config(db: Session = Depends(get_db), actor: User = Depends(superadmin)):
    cfg = db.get(AttendanceBotConfig, 1)
    identities = db.query(EmployeeTelegramIdentity, User).join(User, User.id==EmployeeTelegramIdentity.user_id).order_by(User.username).all()
    return {'enabled': bool(cfg.enabled) if cfg else False, 'bot_id': cfg.bot_id if cfg else '', 'group_chat_id': cfg.group_chat_id if cfg else '', 'bot_token_configured': bool(cfg and cfg.bot_token_ciphertext), 'employees': [{'user_id':u.id,'username':u.username,'role':u.role,'telegram_user_id':ident.telegram_user_id} for ident,u in identities]}

@app.put('/api/admin/attendance/config')
def save_attendance_config(data: AttendanceBotConfigIn, db: Session = Depends(get_db), actor: User = Depends(superadmin)):
    bot_id = (data.bot_id or '').strip()
    group_id = (data.group_chat_id or '').strip()
    token = (data.bot_token or '').strip()
    if bot_id and not re.fullmatch(r'\d{5,15}', bot_id):
        raise HTTPException(400, 'Bot ID must be numeric.')
    if group_id and not re.fullmatch(r'-?\d{5,20}', group_id):
        raise HTTPException(400, 'Group Chat ID must be a numeric Telegram chat ID.')
    if token and not re.fullmatch(r'\d{5,15}:[A-Za-z0-9_-]{20,}', token):
        raise HTTPException(400, 'Attendance Bot Token format is invalid.')
    token_bot_id = token.split(':',1)[0] if token else ''
    if token and bot_id and bot_id != token_bot_id:
        raise HTTPException(400, 'Bot ID does not match the numeric ID at the beginning of the Bot Token.')
    if token and not bot_id: bot_id = token_bot_id
    cfg = db.get(AttendanceBotConfig, 1)
    if cfg is None:
        cfg = AttendanceBotConfig(id=1, bot_token_ciphertext='', bot_id='', group_chat_id='', enabled=False, last_update_id=0); db.add(cfg)
    if token:
        cfg.bot_token_ciphertext = encrypt_session(token)
        if not bot_id: bot_id = token.split(':',1)[0]
    if not bot_id and cfg.bot_id: bot_id = cfg.bot_id
    cfg.bot_id, cfg.group_chat_id, cfg.enabled = bot_id, group_id, bool(data.enabled)
    if not cfg.enabled:
        db.query(EmployeeBreakState).update({EmployeeBreakState.break_started_at: None, EmployeeBreakState.last_return_at: datetime.now(timezone.utc)}, synchronize_session=False)
    seen_ids, seen_users = set(), set()
    for item in data.employees:
        telegram_id = str(item.telegram_user_id).strip()
        target = db.get(User, item.user_id)
        if not target or target.role not in ('employee','chatting','finding','admin') or not target.active:
            raise HTTPException(400, f'Invalid active employee account ID: {item.user_id}')
        if not re.fullmatch(r'\d{3,20}', telegram_id):
            raise HTTPException(400, f'Telegram user ID for {target.username} must be numeric.')
        if telegram_id in seen_ids or item.user_id in seen_users:
            raise HTTPException(400, 'Each employee and Telegram user ID can only appear once.')
        seen_ids.add(telegram_id); seen_users.add(item.user_id)
    db.query(EmployeeTelegramIdentity).delete(synchronize_session=False)
    for item in data.employees:
        db.add(EmployeeTelegramIdentity(user_id=item.user_id, telegram_user_id=str(item.telegram_user_id).strip()))
    db.commit()
    audit(db, actor, 'employee.attendance_bot_config.update', 'settings', 'attendance_bot', f'enabled={cfg.enabled}; employees={len(data.employees)}')
    return {'ok': True, 'enabled': cfg.enabled, 'bot_id': cfg.bot_id, 'group_chat_id': cfg.group_chat_id, 'bot_token_configured': bool(cfg.bot_token_ciphertext), 'employees': len(data.employees)}

async def attendance_bot_loop():
    """Dedicated attendance bot: employees send /break and /back in the configured group."""
    import httpx
    while True:
        try:
            with SessionLocal() as db:
                cfg = db.get(AttendanceBotConfig, 1)
                if not cfg or not cfg.enabled or not cfg.bot_token_ciphertext or not cfg.group_chat_id:
                    await asyncio.sleep(5); continue
                try: bot_token = decrypt_session(cfg.bot_token_ciphertext)
                except Exception as exc:
                    print('attendance bot token decrypt failed:', repr(exc)); await asyncio.sleep(10); continue
                offset = int(cfg.last_update_id or 0) + 1
                group_id = str(cfg.group_chat_id)
                identities = {str(row.telegram_user_id): row.user_id for row in db.query(EmployeeTelegramIdentity).all()}
            async with httpx.AsyncClient(timeout=6.0) as client:
                response = await client.get(f'https://api.telegram.org/bot{bot_token}/getUpdates', params={'offset':offset,'timeout':0,'allowed_updates':json.dumps(['message'])})
                payload = response.json()
                if payload.get('ok'):
                    for update in payload.get('result', []):
                        update_id = int(update.get('update_id', 0))
                        message = update.get('message') or {}
                        chat = message.get('chat') or {}
                        sender = message.get('from') or {}
                        text_value = str(message.get('text') or '').strip().lower()
                        if str(chat.get('id','')) == group_id and str(sender.get('id','')) in identities and text_value.startswith('/'):
                            command = text_value.split()[0].split('@')[0]
                            user_id = identities[str(sender.get('id'))]
                            now_utc = datetime.now(timezone.utc)
                            with SessionLocal() as db:
                                employee = db.get(User, user_id)
                                state = db.get(EmployeeBreakState, user_id)
                                if employee and employee.active and command in ('/break','/breakout','/out'):
                                    if state is None: state = EmployeeBreakState(user_id=user_id); db.add(state)
                                    if state.break_started_at is None:
                                        state.break_started_at = now_utc; state.last_action = 'break'
                                    db.commit()
                                    local = now_utc.astimezone(ZoneInfo('Asia/Yangon'))
                                    reply = f'{employee.username} started a break at {local.strftime("%Y-%m-%d %H:%M:%S")} MMT.'
                                elif employee and employee.active and command in ('/back','/return','/in'):
                                    if state is None: state = EmployeeBreakState(user_id=user_id); db.add(state)
                                    state.break_started_at = None; state.last_return_at = now_utc; state.last_action = 'back'; db.commit()
                                    local = now_utc.astimezone(ZoneInfo('Asia/Yangon'))
                                    reply = f'{employee.username} returned at {local.strftime("%Y-%m-%d %H:%M:%S")} MMT. Offline timer has been reset.'
                                else:
                                    reply = ''
                            if reply:
                                try: await client.post(f'https://api.telegram.org/bot{bot_token}/sendMessage', json={'chat_id':group_id,'text':reply})
                                except Exception as exc: print('attendance bot reply failed:', repr(exc))
                        with SessionLocal() as db:
                            latest = db.get(AttendanceBotConfig, 1)
                            if latest:
                                latest.last_update_id = max(int(latest.last_update_id or 0), update_id); db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print('attendance bot polling error:', repr(exc))
        await asyncio.sleep(5)

async def employee_offline_alert_loop():
    """Poll presence and send one Telegram alert per offline episode during Myanmar night hours."""
    import httpx
    tz = ZoneInfo('Asia/Yangon')
    while True:
        try:
            now_local = datetime.now(tz)
            in_window = (now_local.hour > 20 or (now_local.hour == 20 and now_local.minute >= 30) or now_local.hour < 9 or (now_local.hour == 9 and now_local.minute < 30))
            if in_window:
                with SessionLocal() as db:
                    cfg = db.get(EmployeeAlertConfig, 1)
                    if cfg and cfg.enabled and cfg.bot_token_ciphertext:
                        try:
                            bot_token = decrypt_session(cfg.bot_token_ciphertext)
                            recipients = json.loads(cfg.chat_ids or '[]')
                        except Exception:
                            bot_token, recipients = '', []
                        if bot_token and recipients:
                            users = db.query(User).filter(User.active.is_(True), User.role.in_(('employee','chatting','finding','admin'))).all()
                            for user in users:
                                sessions = db.query(PresenceSession).filter(PresenceSession.user_id == user.id).order_by(desc(PresenceSession.last_seen)).all()
                                if not sessions:
                                    continue
                                latest = sessions[0]
                                last_seen = latest.last_seen
                                if last_seen.tzinfo is None: last_seen = last_seen.replace(tzinfo=timezone.utc)
                                online = latest.is_active and (datetime.now(timezone.utc) - last_seen).total_seconds() <= 90
                                break_state = db.get(EmployeeBreakState, user.id)
                                if break_state and break_state.break_started_at:
                                    continue
                                state = db.query(EmployeeAlertState).filter_by(user_id=user.id).first()
                                if online:
                                    if state and state.offline_key:
                                        state.offline_key = ''
                                        state.notified_at = None
                                    continue
                                offline_since = latest.logout_at or last_seen
                                if offline_since.tzinfo is None: offline_since = offline_since.replace(tzinfo=timezone.utc)
                                if break_state and break_state.last_return_at:
                                    returned = break_state.last_return_at
                                    if returned.tzinfo is None: returned = returned.replace(tzinfo=timezone.utc)
                                    offline_since = max(offline_since, returned)
                                elapsed = (datetime.now(timezone.utc) - offline_since).total_seconds()
                                if elapsed < 900:
                                    continue
                                event_key = offline_since.astimezone(timezone.utc).isoformat(timespec='seconds')
                                if state and state.offline_key == event_key and state.notified_at:
                                    continue
                                since_local = offline_since.astimezone(tz)
                                duration_minutes = int(elapsed // 60)
                                duration_text = f'{duration_minutes // 60}h {duration_minutes % 60}m' if duration_minutes >= 60 else f'{duration_minutes} minutes'
                                message = (f'Employee: {user.username}\nStatus: Offline\nOffline since: {since_local.strftime("%Y-%m-%d %H:%M:%S")} (MMT)\n'
                                           f'Offline duration: {duration_text}\nAlert time: {now_local.strftime("%Y-%m-%d %H:%M:%S")} (MMT)')
                                delivered = bool(recipients)
                                async with httpx.AsyncClient(timeout=4.0) as client:
                                    for chat_id in recipients:
                                        try:
                                            resp = await client.post(f'https://api.telegram.org/bot{bot_token}/sendMessage', json={'chat_id': chat_id, 'text': message})
                                            resp.raise_for_status()
                                            if not resp.json().get('ok'): delivered = False
                                        except Exception as exc:
                                            delivered = False
                                            print('employee offline alert delivery failed:', repr(exc))
                                if delivered:
                                    if state is None:
                                        state = EmployeeAlertState(user_id=user.id, offline_key=event_key, notified_at=datetime.now(timezone.utc))
                                        db.add(state)
                                    else:
                                        state.offline_key = event_key
                                        state.notified_at = datetime.now(timezone.utc)
                            db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print('employee offline alert monitor error:', repr(exc))
        await asyncio.sleep(60)

@app.get('/api/admin/stats')
def stats(db:Session=Depends(get_db), actor:User=Depends(admin)):
    require_permission(actor, 'stats.view')
    now_utc = datetime.now(timezone.utc)
    local_now = datetime.now(ZoneInfo('Asia/Yangon'))
    today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    employee_roles = ('employee', 'chatting', 'finding')
    visible_roles = employee_roles + (('admin',) if actor.role == 'superadmin' else ())
    users = db.query(User).filter(User.role.in_(visible_roles)).order_by(User.role, User.username).all()
    user_ids = [u.id for u in users]
    usernames = [u.username for u in users]

    # Batch-load presence, assignments, message counts and audit activity to avoid
    # the previous per-user query pattern that became slow as staff grew.
    presence_rows = (db.query(PresenceSession)
                     .filter(PresenceSession.user_id.in_(user_ids)).all()) if user_ids else []
    latest_by_user = {}
    for row in presence_rows:
        last = row.last_seen if row.last_seen.tzinfo else row.last_seen.replace(tzinfo=timezone.utc)
        if row.user_id not in latest_by_user or last > latest_by_user[row.user_id][0]:
            latest_by_user[row.user_id] = (last, row)

    assignment_rows = (db.query(Assignment.user_id, Assignment.customer_id, Assignment.assigned_at)
                       .filter(Assignment.user_id.in_(user_ids), Assignment.active.is_(True)).all()) if user_ids else []
    assigned_by_user = {}
    today_received_by_user = {}
    customer_to_users = {}
    for uid, cid, assigned_at in assignment_rows:
        assigned_by_user.setdefault(uid, set()).add(cid)
        customer_to_users.setdefault(cid, set()).add(uid)
        stamp = assigned_at if assigned_at and assigned_at.tzinfo else (assigned_at.replace(tzinfo=timezone.utc) if assigned_at else None)
        if stamp and stamp >= today_start:
            today_received_by_user[uid] = today_received_by_user.get(uid, 0) + 1

    sent_by_user = dict(db.query(Message.sender_label, func.count(Message.id))
                        .filter(Message.sender_label.in_(usernames), Message.direction == 'out',
                                 Message.sent_at >= today_start)
                        .group_by(Message.sender_label).all()) if usernames else {}
    inbound_by_customer = dict(db.query(Message.customer_id, func.count(Message.id))
                               .filter(Message.customer_id.in_(list(customer_to_users) or [-1]),
                                       Message.direction == 'in', Message.sent_at >= today_start)
                               .group_by(Message.customer_id).all()) if customer_to_users else {}

    creator_ids = {u.created_by_user_id for u in users if u.created_by_user_id}
    audit_rows = (db.query(AuditLog).filter(AuditLog.target_type == 'user',
                                            AuditLog.target_id.in_([str(uid) for uid in user_ids]))
                  .order_by(desc(AuditLog.created_at)).all()) if user_ids else []
    last_op_by_user = {}
    for row in audit_rows:
        try:
            target_id = int(row.target_id)
        except (TypeError, ValueError):
            continue
        if target_id not in last_op_by_user and row.actor_user_id != target_id:
            last_op_by_user[target_id] = row
    related_user_ids = creator_ids | {r.actor_user_id for r in last_op_by_user.values() if r.actor_user_id}
    related_users = {u.id: u for u in db.query(User).filter(User.id.in_(related_user_ids)).all()} if related_user_ids else {}

    staff = []
    for u in users:
        presence = latest_by_user.get(u.id)
        last_seen, session = presence if presence else (None, None)
        online = bool(session and session.is_active and last_seen and (now_utc - last_seen).total_seconds() <= 90 and u.active)
        assigned_customer_ids = assigned_by_user.get(u.id, set())
        today_inbound = sum(inbound_by_customer.get(cid, 0) for cid in assigned_customer_ids)
        login_at = session.login_at if session else None
        logout_at = session.logout_at if session else None
        if login_at and login_at.tzinfo is None: login_at = login_at.replace(tzinfo=timezone.utc)
        if logout_at and logout_at.tzinfo is None: logout_at = logout_at.replace(tzinfo=timezone.utc)
        online_seconds = int(max(0, ((now_utc if online else (logout_at or last_seen)) - login_at).total_seconds())) if login_at and (online or logout_at or last_seen) else 0
        offline_base = (session.logout_at if session and session.logout_at else last_seen)
        offline_base = offline_base if not offline_base or offline_base.tzinfo else offline_base.replace(tzinfo=timezone.utc)
        offline_seconds = int(max(0, (now_utc - offline_base).total_seconds())) if (not online and offline_base) else None
        creator = related_users.get(u.created_by_user_id) if u.created_by_user_id else None
        last_op = last_op_by_user.get(u.id)
        operator = related_users.get(last_op.actor_user_id) if last_op and last_op.actor_user_id else None
        staff.append({
            'id': u.id, 'public_id': u.public_id, 'username': u.username, 'role': u.role,
            'active': u.active, 'online': online, 'channel': session.channel if session else 'Telegram',
            'login_at': utc_iso(login_at), 'logout_at': utc_iso(logout_at), 'last_seen': utc_iso(last_seen),
            'online_seconds': online_seconds, 'offline_seconds': offline_seconds,
            'today_received_members': today_received_by_user.get(u.id, 0), 'today_member_messages': today_inbound,
            'today_sent_messages': sent_by_user.get(u.username, 0), 'assigned_members': len(assigned_customer_ids),
            'created_at': utc_iso(u.created_at), 'created_by': creator.username if creator else 'System',
            'last_operator': operator.username if operator else (creator.username if creator else 'System'),
            'last_operation': last_op.action if last_op else 'user.create'
        })
    active_assignments = db.query(func.count(func.distinct(Assignment.customer_id))).filter(Assignment.active.is_(True)).scalar() or 0
    online_count = sum(1 for item in staff if item['online'])
    today_new = db.query(func.count(Assignment.id)).filter(Assignment.assigned_at >= today_start).scalar() or 0
    today_messages = db.query(func.count(Message.id)).filter(Message.sent_at >= today_start).scalar() or 0
    return {
        'employees': len(users), 'online_last_5m': online_count, 'online_members': online_count,
        'active_conversations': active_assignments, 'today_received': today_new,
        'today_messages': today_messages, 'telegram_accounts': db.query(func.count(TgAccount.id)).scalar() or 0,
        'staff': staff
    }

@app.get('/api/admin/audit')
def audit_logs(limit:int=100,db:Session=Depends(get_db),actor:User=Depends(admin)):
    require_permission(actor, 'stats.view')
    rows=db.query(AuditLog).order_by(desc(AuditLog.created_at)).limit(min(limit,500)).all()
    return [{'actor_user_id':r.actor_user_id,'actor_username':(db.get(User, r.actor_user_id).username if r.actor_user_id and db.get(User, r.actor_user_id) else 'System'),'action':r.action,'target_type':r.target_type,'target_id':r.target_id,'detail':r.detail,'created_at':r.created_at.isoformat()} for r in rows]

@app.get('/api/health')
def health(): return {'ok':True,'service':'telegram-crm'}

@app.get('/api/health/ready')
def readiness(db: Session = Depends(get_db)):
    try:
        db.execute(text('SELECT 1'))
    except Exception as exc:
        raise HTTPException(503, f'Database is not ready: {type(exc).__name__}')
    return {'ok': True, 'database': 'connected', 'database_dialect': engine.dialect.name}

@app.get('/api/admin/diagnostics')
def diagnostics(db: Session = Depends(get_db), actor: User = Depends(admin)):
    require_permission(actor, 'stats.view')
    try:
        db.execute(text('SELECT 1'))
        database_status = 'connected'
    except Exception as exc:
        database_status = f'error: {type(exc).__name__}'
    accounts = db.query(TgAccount).all() if database_status == 'connected' else []
    return {
        'database': database_status,
        'database_dialect': engine.dialect.name,
        'database_url_configured': bool(os.getenv('DATABASE_URL')),
        'telegram_api_configured': bool(settings.telegram_api_id and settings.telegram_api_hash),
        'session_encryption_configured': bool(settings.session_encryption_key),
        'telegram_accounts_total': len(accounts),
        'telegram_accounts_connected': len([a for a in accounts if a.status == 'connected']),
    }
