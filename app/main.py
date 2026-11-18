import asyncio, re, secrets, os, uuid, mimetypes, base64, hmac
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Depends, HTTPException, Header, UploadFile, File, Form, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import desc, text, func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from .db import Base, engine, get_db, SessionLocal
from .models import User, Team, TgAccount, Customer, Assignment, Message, Tag, AuditLog, ReadState, PresenceSession, Notification
from .security import hash_password, verify_password, create_token, decode_token, create_2fa_challenge, encrypt_session, decrypt_session
from .config import settings
from . import telegram_service as tg
import pyotp, qrcode
from io import BytesIO

Base.metadata.create_all(bind=engine)

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
    conn.exec_driver_sql("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_public_id ON users (public_id)")
    customer_columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(customers)").fetchall()} if engine.dialect.name == "sqlite" else {row[0] for row in conn.exec_driver_sql("SELECT column_name FROM information_schema.columns WHERE table_name='customers'").fetchall()}
    if "avatar_path" not in customer_columns:
        conn.exec_driver_sql("ALTER TABLE customers ADD COLUMN avatar_path VARCHAR(500) NOT NULL DEFAULT ''")
    message_columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(messages)").fetchall()} if engine.dialect.name == "sqlite" else {row[0] for row in conn.exec_driver_sql("SELECT column_name FROM information_schema.columns WHERE table_name='messages'").fetchall()}
    if "sender_user_id" not in message_columns:
        conn.exec_driver_sql("ALTER TABLE messages ADD COLUMN sender_user_id INTEGER REFERENCES users(id)")

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

app = FastAPI(title='X-Wallet Chat Management', version='7.6.0')
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins.split(','), allow_credentials=False, allow_methods=['*'], allow_headers=['*'])
app.mount('/static', StaticFiles(directory='static'), name='static')

class LoginIn(BaseModel): username: str; password: str
class TwoFactorVerifyIn(BaseModel): challenge_token: str; code: str
class TwoFactorCodeIn(BaseModel): code: str
class ForwardIn(BaseModel): target_customer_id: int
class UserIn(BaseModel): username: str; password: str = Field(min_length=10); role: str = 'chatting'; team_id: int | None = None
class TeamIn(BaseModel): name: str
class PhoneIn(BaseModel): phone: str
class CodeIn(BaseModel): phone_code_hash: str; code: str; password: str | None = None
class AssignIn(BaseModel): customer_id: int; user_id: int
class BulkAssignIn(BaseModel): customer_ids: list[int]; user_id: int; account_id: int
class SendIn(BaseModel): text: str = Field(min_length=1, max_length=4096)
class TagIn(BaseModel): key: str; value: str; scope: str = 'private'
class PresenceIn(BaseModel): session_id: str = Field(min_length=8, max_length=100)
class ChannelEventIn(BaseModel): channel: str

UPLOAD_ROOT = os.path.abspath(settings.upload_dir)
os.makedirs(UPLOAD_ROOT, exist_ok=True)
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
ALLOWED_MEDIA_EXTENSIONS = {'.jpg','.jpeg','.png','.gif','.webp','.bmp','.heic','.tif','.tiff','.mp4','.mov','.m4v','.webm','.mkv','.avi','.mpg','.mpeg','.mp3','.m4a','.ogg','.oga','.opus','.wav','.aac','.flac','.aiff','.3gp'}

def utc_iso(value):
    if value is None: return None
    if value.tzinfo is None: value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()

def media_kind(path):
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
    if settings.telegram_api_id and settings.telegram_api_hash and settings.session_encryption_key:
        asyncio.create_task(tg.restore_clients())

@app.get('/')
def home(): return FileResponse('static/index.html')

@app.get('/superadmin-login')
def superadmin_login_page(): return FileResponse('static/index.html')

@app.get('/login/{username}')
def employee_login_link(username: str):
    # Chatting employee link; username is prefilled but never grants access by itself.
    return FileResponse('static/index.html')

@app.get('/finding/{username}')
def finding_login_link(username: str):
    # Finding employee link uses a distinct route and brand, but still requires credentials.
    return FileResponse('static/index.html')

@app.post('/api/auth/login')
def login(data: LoginIn, db: Session = Depends(get_db)):
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
    user.last_seen = datetime.now(timezone.utc); db.commit()
    return {'token': create_token(user.id,user.role), 'user': {'id':user.id,'public_id':user.public_id,'username':user.username,'role':user.role,'team_id':user.team_id,'totp_enabled':user.totp_enabled}}

@app.post('/api/auth/2fa/verify')
def verify_login_2fa(data: TwoFactorVerifyIn, db: Session = Depends(get_db)):
    payload = decode_token(data.challenge_token)
    if not payload or payload.get('purpose') != '2fa_login':
        raise HTTPException(401, 'Two-factor login challenge expired. Please sign in again.')
    user = db.get(User, int(payload['sub']))
    if not user or not user.active or not user.totp_enabled or not user.totp_secret:
        raise HTTPException(401, 'Two-factor account is unavailable.')
    if not pyotp.TOTP(decrypt_session(user.totp_secret)).verify(data.code.strip(), valid_window=1):
        raise HTTPException(401, 'Invalid 2FA code. Check the code and try again.')
    user.last_seen = datetime.now(timezone.utc); db.commit()
    return {'token': create_token(user.id,user.role), 'user': {'id':user.id,'public_id':user.public_id,'username':user.username,'role':user.role,'team_id':user.team_id,'totp_enabled':True}}

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

@app.post('/api/presence/heartbeat')
def presence_heartbeat(data: PresenceIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    now_utc = datetime.now(timezone.utc)
    row = db.query(PresenceSession).filter_by(user_id=user.id, session_key=data.session_id).first()
    if row is None:
        row = PresenceSession(user_id=user.id, session_key=data.session_id, login_at=now_utc, last_seen=now_utc, is_active=True)
        db.add(row)
        audit(db, user, 'session.login', 'user', user.id, 'browser_session_started')
    else:
        row.last_seen = now_utc
        row.is_active = True
        row.logout_at = None
    user.last_seen = now_utc
    db.commit()
    return {'ok': True, 'server_time': now_utc.isoformat()}

@app.post('/api/presence/logout')
def presence_logout(data: PresenceIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    now_utc = datetime.now(timezone.utc)
    row = db.query(PresenceSession).filter_by(user_id=user.id, session_key=data.session_id).first()
    if row:
        row.is_active = False
        row.logout_at = now_utc
        row.last_seen = now_utc
    audit(db, user, 'session.logout', 'user', user.id, 'browser_session_ended')
    db.commit()
    return {'ok': True}

@app.get('/api/notifications')
def employee_notifications(db: Session = Depends(get_db), user: User = Depends(current_user)):
    if user.role in ('admin', 'superadmin'):
        return {'count': 0, 'items': []}
    rows = db.query(Notification).filter_by(user_id=user.id).order_by(desc(Notification.created_at)).limit(40).all()
    unread_count = db.query(Notification).filter_by(user_id=user.id, read_at=None).count()
    return {'count': unread_count, 'items': [{'id':n.id,'customer_id':n.customer_id,'title':n.title,'body':n.body,'kind':n.kind,'created_at':utc_iso(n.created_at),'read':n.read_at is not None} for n in rows]}

@app.post('/api/notifications/{notification_id}/read')
def mark_notification_read(notification_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    row = db.query(Notification).filter_by(id=notification_id, user_id=user.id).first()
    if row and row.read_at is None:
        row.read_at = datetime.now(timezone.utc)
        db.commit()
    return {'ok': True}

@app.post('/api/notifications/read-all')
def mark_all_notifications_read(db: Session = Depends(get_db), user: User = Depends(current_user)):
    db.query(Notification).filter_by(user_id=user.id, read_at=None).update({Notification.read_at: datetime.now(timezone.utc)})
    db.commit()
    return {'ok': True}

@app.post('/api/activity/channel')
def record_channel_activity(data: ChannelEventIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    allowed = {'telegram','whatsapp','instagram','tiktok','line'}
    channel = data.channel.lower().strip()
    if channel not in allowed:
        raise HTTPException(400, 'Unsupported social channel')
    audit(db, user, 'channel.open', 'channel', channel, 'opened_inside_crm')
    return {'ok': True}

@app.get('/api/me')
def me(user: User = Depends(current_user)):
    return {'id':user.id,'public_id':user.public_id,'username':user.username,'role':user.role,'team_id':user.team_id,'totp_enabled':user.totp_enabled}

@app.post('/api/security/2fa/setup')
def setup_2fa(db: Session = Depends(get_db), user: User = Depends(admin)):
    if user.totp_enabled:
        raise HTTPException(409, '2FA is already enabled on this account.')
    secret = pyotp.random_base32()
    user.totp_secret = encrypt_session(secret)
    db.commit()
    account_label = f"X-Wallet + {user.username}"
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.username, issuer_name='X-Wallet')
    qr = qrcode.QRCode(box_size=3, border=1, error_correction=qrcode.constants.ERROR_CORRECT_L)
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
    target = db.get(User, uid)
    if not target or not target.active:
        raise HTTPException(404, 'Active user not found')
    if target.role in ('admin', 'superadmin') and actor.role != 'superadmin':
        raise HTTPException(403, 'Only the super administrator can manage administrator 2FA')
    secret = pyotp.random_base32()
    target.totp_secret = encrypt_session(secret)
    target.totp_enabled = False
    db.commit()
    issuer = 'X-Wallet'
    account_name = f'X-Wallet + {target.username}'
    uri = pyotp.TOTP(secret).provisioning_uri(name=target.username, issuer_name='X-Wallet')
    qr = qrcode.QRCode(box_size=3, border=1, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(uri); qr.make(fit=True)
    image = qr.make_image(fill_color='black', back_color='white')
    buffer = BytesIO(); image.save(buffer, format='PNG')
    qr_data = 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')
    audit(db, actor, 'user.2fa.setup_started', 'user', uid)
    return {'qr_data_uri': qr_data, 'secret': secret, 'account_label': account_name}

@app.post('/api/admin/users/{uid}/2fa/confirm')
def admin_confirm_user_2fa(uid: int, data: TwoFactorCodeIn, db: Session = Depends(get_db), actor: User = Depends(admin)):
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
    return [{'id':u.id,'public_id':u.public_id,'username':u.username,'role':u.role,'team_id':u.team_id,'active':u.active,'totp_enabled':u.totp_enabled,'created_at':u.created_at.isoformat(),'last_seen':u.last_seen.isoformat() if u.last_seen else None} for u in db.query(User).order_by(User.id).all()]

@app.post('/api/admin/users')
def create_user(data: UserIn, request: Request, db: Session = Depends(get_db), actor: User = Depends(admin)):
    username = data.username.strip()
    if not username or len(username) > 80:
        raise HTTPException(400, 'Username must contain 1 to 80 characters')
    if data.role not in ('admin', 'employee', 'chatting', 'finding'):
        raise HTTPException(400, 'Role must be admin, Chatting, or Finding')
    if len(data.password) < 10:
        raise HTTPException(400, 'Password must be at least 10 characters')
    if len(data.password.encode('utf-8')) > 72:
        raise HTTPException(400, 'Password must be no more than 72 UTF-8 bytes for the current password-hash algorithm')
    if db.query(User).filter(func.lower(User.username) == username.lower()).first():
        raise HTTPException(409, 'Username already exists (usernames are case-insensitive)')
    if data.team_id is not None and not db.get(Team, data.team_id):
        raise HTTPException(404, 'Team not found')
    try:
        u = User(username=username, public_id=make_public_user_id(db),
                 password_hash=hash_password(data.password), role=data.role,
                 team_id=data.team_id, active=True)
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
    login_path = ('/finding/' if u.role == 'finding' else '/login/') + quote(u.username, safe='')
    login_url = base_url + login_path
    return {'id': u.id, 'public_id': u.public_id, 'username': u.username,
            'role': u.role, 'login_url': login_url, 'needs_custom_domain': False}

@app.patch('/api/admin/users/{uid}')
def update_user(uid:int, active:bool, db:Session=Depends(get_db), actor:User=Depends(admin)):
    u=db.get(User,uid)
    if not u: raise HTTPException(404,'User not found')
    u.active=active; db.commit(); audit(db,actor,'user.active','user',uid,str(active)); return {'ok':True}

@app.post('/api/admin/users/{uid}/password')
def reset_user_password(uid:int, data:dict, db:Session=Depends(get_db), actor:User=Depends(admin)):
    u=db.get(User,uid)
    password=data.get('password','')
    if not u: raise HTTPException(404,'User not found')
    if len(password)<10: raise HTTPException(400,'Password must be at least 10 characters')
    u.password_hash=hash_password(password); db.commit(); audit(db,actor,'user.password_reset','user',uid)
    return {'ok':True}

@app.delete('/api/admin/users/{uid}')
def delete_user(uid:int, db:Session=Depends(get_db), actor:User=Depends(admin)):
    u=db.get(User,uid)
    if not u: raise HTTPException(404,'User not found')
    if u.id==actor.id: raise HTTPException(400,'Cannot delete yourself')
    u.active=False; u.username=f'deleted_{u.id}_{u.username}'; db.commit(); audit(db,actor,'user.soft_delete','user',uid); return {'ok':True,'note':'Soft deleted; historical records retained'}

@app.post('/api/admin/teams')
def create_team(data:TeamIn, db:Session=Depends(get_db), actor:User=Depends(admin)):
    if db.query(Team).filter_by(name=data.name).first(): raise HTTPException(409,'Team exists')
    t=Team(name=data.name); db.add(t); db.commit(); db.refresh(t); audit(db,actor,'team.create','team',t.id); return {'id':t.id,'name':t.name}

@app.get('/api/admin/teams')
def list_teams(db:Session=Depends(get_db), actor:User=Depends(admin)):
    return [{'id':t.id,'name':t.name} for t in db.query(Team).all()]

@app.patch('/api/admin/users/{uid}/team')
def set_user_team(uid: int, data: dict, db: Session = Depends(get_db), actor: User = Depends(admin)):
    user = db.get(User, uid)
    if not user:
        raise HTTPException(404, 'User not found')
    team_id = data.get('team_id')
    if team_id is not None and not db.get(Team, int(team_id)):
        raise HTTPException(404, 'Team not found')
    user.team_id = int(team_id) if team_id is not None else None
    db.commit()
    audit(db, actor, 'user.team_update', 'user', uid, f'team_id={user.team_id}')
    return {'ok': True, 'team_id': user.team_id}

@app.post('/api/admin/telegram/begin')
async def telegram_begin(data:PhoneIn, actor:User=Depends(admin)):
    try: return {'phone_code_hash':await tg.begin_login(data.phone)}
    except Exception as e: raise HTTPException(400,str(e))

@app.post('/api/admin/telegram/complete')
async def telegram_complete(data:CodeIn, db:Session=Depends(get_db), actor:User=Depends(admin)):
    try:
        result=await tg.finish_login(data.phone_code_hash,data.code,data.password)
        if result.get('account_id'): audit(db,actor,'telegram.account.connect','telegram_account',result['account_id'])
        return result
    except Exception as e: raise HTTPException(400,str(e))

@app.get('/api/admin/app-url')
def admin_app_url(actor: User = Depends(admin)):
    return {'base_url': 'https://xwalletchatserver-production.up.railway.app'}

@app.get('/api/admin/telegram/accounts')
def telegram_accounts(db:Session=Depends(get_db), actor:User=Depends(admin)):
    return [{'id':a.id,'label':a.label,'phone_masked':a.phone_masked,'status':a.status,'created_at':utc_iso(a.created_at),'customer_count':db.query(Customer).filter_by(account_id=a.id).count()} for a in db.query(TgAccount).order_by(TgAccount.id).all()]

@app.post('/api/admin/telegram/accounts/{account_id}/sync')
async def admin_sync_telegram_account(account_id: int, db: Session = Depends(get_db), actor: User = Depends(admin)):
    account = db.get(TgAccount, account_id)
    if not account:
        raise HTTPException(404, 'Telegram account not found')
    client = tg._clients.get(account_id)
    if not client or not client.is_connected():
        raise HTTPException(409, 'Telegram account is not connected. Reconnect this account first.')
    try:
        # Index every chat first so the admin customer list is populated quickly.
        result = await tg.sync_dialogs(account_id, client, sync_messages=False)
    except Exception as exc:
        db.rollback()
        raise HTTPException(502, f'Telegram dialog sync failed: {exc}')
    count = db.query(Customer).filter_by(account_id=account_id).count()
    audit(db, actor, 'telegram.account.sync', 'telegram_account', account_id,
          f'customers={count}; dialogs={result.get("dialog_count", 0)}')
    # Import message history after returning the complete customer index.
    asyncio.create_task(tg.sync_dialog_messages(account_id, client))
    return {'ok': True, **result, 'customer_count': count,
            'message': 'All available chat dialogs have been indexed. Message history is importing in the background.'}

@app.get('/api/admin/telegram/accounts/{account_id}/profile')
async def admin_telegram_account_profile(account_id: int, actor: User = Depends(admin)):
    client = tg._clients.get(account_id)
    if not client or not client.is_connected():
        raise HTTPException(409, 'Telegram account is not connected.')
    me = await client.get_me()
    name = ' '.join(filter(None, [getattr(me, 'first_name', ''), getattr(me, 'last_name', '')])).strip()
    avatar_file = os.path.join(UPLOAD_ROOT, f'tg_account_{account_id}_avatar.jpg')
    avatar_available = False
    try:
        saved = await client.download_profile_photo(me, file=avatar_file)
        avatar_available = bool(saved)
    except Exception:
        pass
    return {
        'id': getattr(me, 'id', None),
        'name': name or account_id,
        'username': getattr(me, 'username', '') or '',
        'phone': getattr(me, 'phone', '') or '',
        'avatar_available': avatar_available
    }

@app.get('/api/admin/telegram/accounts/{account_id}/avatar')
def admin_telegram_account_avatar(account_id: int, actor: User = Depends(admin)):
    path = os.path.join(UPLOAD_ROOT, f'tg_account_{account_id}_avatar.jpg')
    if not os.path.isfile(path):
        raise HTTPException(404, 'Telegram account avatar is not available yet. Refresh account profile first.')
    return FileResponse(path, media_type='image/jpeg')

@app.get('/api/admin/customers')
def admin_customers(account_id:int|None=None, db:Session=Depends(get_db), actor:User=Depends(admin)):
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
            'assigned_to':assigned})
    return result

@app.get('/api/admin/customers/{cid}/avatar')
def admin_customer_avatar(cid:int, db:Session=Depends(get_db), actor:User=Depends(admin)):
    c=db.get(Customer,cid)
    if not c or not c.avatar_path: raise HTTPException(404,'Telegram profile photo is not available')
    path=safe_upload_path(c.avatar_path)
    return FileResponse(path, media_type=mimetypes.guess_type(path)[0] or 'image/jpeg')

@app.post('/api/admin/assign')
def assign(data:AssignIn, db:Session=Depends(get_db), actor:User=Depends(admin)):
    c=db.get(Customer,data.customer_id); u=db.get(User,data.user_id)
    if not c or not u or not u.active: raise HTTPException(404,'Customer or active user not found')
    if u.role not in ('employee', 'chatting', 'finding'): raise HTTPException(400, 'Customers can only be assigned to Chatting or Finding employees')
    # Reassignment revokes old active assignments but never deletes history/messages/tags.
    old=db.query(Assignment).filter_by(customer_id=c.id,active=True).all()
    for a in old: a.active=False; a.revoked_at=datetime.now(timezone.utc)
    assignment=Assignment(customer_id=c.id,user_id=u.id,assigned_by=actor.id,active=True); db.add(assignment); db.add(Notification(user_id=u.id,customer_id=c.id,title='New customer assigned',body=f'A new Telegram customer has been assigned to you: {c.alias or 'Telegram customer'}',kind='assignment')); db.commit(); audit(db,actor,'customer.assign','customer',c.id,f'to_user={u.id}'); return {'ok':True,'history_preserved':True,'customer_id':c.id,'user_id':u.id,'public_user_id':u.public_id,'username':u.username,'assignment_id':assignment.id}

@app.post('/api/admin/assign/bulk')
def bulk_assign(data:BulkAssignIn, db:Session=Depends(get_db), actor:User=Depends(admin)):
    u=db.get(User,data.user_id)
    if not u or not u.active: raise HTTPException(404,'Active user not found')
    if u.role not in ('employee', 'chatting', 'finding'): raise HTTPException(400, 'Customers can only be assigned to Chatting or Finding employees')
    if not data.customer_ids: raise HTTPException(400, 'Select at least one customer')
    if len(data.customer_ids) > 500: raise HTTPException(400, 'Bulk assignment is limited to 500 customers at a time')
    selected = db.query(Customer).filter(Customer.id.in_(data.customer_ids)).all()
    if len(selected) != len(set(data.customer_ids)) or any(c.account_id != data.account_id for c in selected):
        raise HTTPException(400, 'Every selected customer must belong to the currently selected Telegram account')
    for cid in data.customer_ids:
        c=db.get(Customer,cid)
        if not c: continue
        for a in db.query(Assignment).filter_by(customer_id=cid,active=True).all(): a.active=False; a.revoked_at=datetime.now(timezone.utc)
        db.add(Assignment(customer_id=cid,user_id=u.id,assigned_by=actor.id,active=True)); db.add(Notification(user_id=u.id,customer_id=cid,title='New customer assigned',body=f'A new Telegram customer has been assigned to you: {c.alias or 'Telegram customer'}',kind='assignment'))
    db.commit(); audit(db,actor,'customer.bulk_assign','user',u.id,f'count={len(data.customer_ids)}'); return {'ok':True,'count':len(data.customer_ids)}

@app.post('/api/admin/revoke/{customer_id}')
def revoke(customer_id:int, db:Session=Depends(get_db), actor:User=Depends(admin)):
    rows=db.query(Assignment).filter_by(customer_id=customer_id,active=True).all()
    for a in rows: a.active=False; a.revoked_at=datetime.now(timezone.utc)
    db.commit(); audit(db,actor,'customer.revoke','customer',customer_id); return {'ok':True,'revoked':len(rows),'history_preserved':True}

@app.get('/api/inbox')
def inbox(filter:str='all', db:Session=Depends(get_db), user:User=Depends(current_user)):
    q=db.query(Customer)
    if user.role not in ('superadmin','admin'):
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
    return [{'id':m.id,'direction':m.direction,'text':redact(m.body) if user.role not in ('admin','superadmin') else m.body,'sent_at':utc_iso(m.sent_at),'sender':m.sender_label,'media_available':bool(m.media_path),'media_kind':media_kind(m.media_path) if m.media_path else None} for m in rows]

@app.get('/api/messages/{mid}/media')
def message_media(mid:int, db:Session=Depends(get_db), user:User=Depends(current_user)):
    msg=db.get(Message,mid)
    if not msg or not can_access(db,user,msg.customer_id) or not msg.media_path:
        raise HTTPException(404,'Media not found')
    path=safe_upload_path(msg.media_path)
    return FileResponse(path, media_type=mimetypes.guess_type(path)[0] or 'application/octet-stream')

@app.post('/api/customers/{cid}/send')
async def send(cid:int,data:SendIn, db:Session=Depends(get_db), user:User=Depends(current_user)):
    c=db.get(Customer,cid)
    if not c or not can_access(db,user,cid): raise HTTPException(404,'Customer not found')
    try: sent=await tg.send_message(c.account_id,c,data.text)
    except Exception as e: raise HTTPException(400,str(e))
    m=db.query(Message).filter_by(customer_id=cid,telegram_message_id=str(sent.id)).first()
    if not m:
        m=Message(customer_id=cid,telegram_message_id=str(sent.id),direction='out',body=data.text,sent_at=sent.date or datetime.now(timezone.utc),sender_label=user.username,sender_user_id=user.id)
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
                    media_path=rel,sent_at=sent.date or datetime.now(timezone.utc),sender_label=user.username,sender_user_id=user.id)
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
                    sent_at=sent.date or datetime.now(timezone.utc),sender_label=user.username,sender_user_id=user.id)
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
    state.last_read_message_id=latest.id if latest else 0; db.query(Notification).filter_by(user_id=user.id,customer_id=cid,read_at=None).update({Notification.read_at: datetime.now(timezone.utc)}); db.commit(); return {'ok':True}

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

@app.get('/api/admin/employee-monitor')
def employee_monitor(db: Session = Depends(get_db), actor: User = Depends(admin)):
    now_utc = datetime.now(timezone.utc)
    local_now = now_utc.astimezone(ZoneInfo('Asia/Yangon'))
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    employees = db.query(User).filter(User.role.in_(['employee','chatting','finding'])).order_by(User.created_at.desc()).all()
    out = []
    for u in employees:
        sessions = db.query(PresenceSession).filter_by(user_id=u.id).order_by(desc(PresenceSession.last_seen)).all()
        latest = sessions[0] if sessions else None
        is_online = bool(latest and latest.is_active and (now_utc - (latest.last_seen if latest.last_seen.tzinfo else latest.last_seen.replace(tzinfo=timezone.utc))).total_seconds() <= 90)
        active_count = db.query(Assignment).filter_by(user_id=u.id, active=True).count()
        message_count = db.query(Message).filter(Message.sender_user_id==u.id, Message.direction=='out', Message.sent_at>=local_start).count()
        total_messages = db.query(Message).filter(Message.sender_user_id==u.id, Message.direction=='out').count()
        today_assignments = db.query(Assignment).filter(Assignment.user_id==u.id, Assignment.assigned_at>=local_start).count()
        intervals = []
        for sess in sessions:
            start = sess.login_at if sess.login_at.tzinfo else sess.login_at.replace(tzinfo=timezone.utc)
            last_seen = sess.last_seen if sess.last_seen.tzinfo else sess.last_seen.replace(tzinfo=timezone.utc)
            end = now_utc if sess.is_active and (now_utc-last_seen).total_seconds() <= 90 else (sess.logout_at or sess.last_seen)
            end = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
            if end >= local_start and start <= now_utc:
                intervals.append((max(start, local_start), min(end, now_utc)))
        intervals.sort(key=lambda item: item[0])
        merged = []
        for start, end in intervals:
            if not merged or start > merged[-1][1]: merged.append([start, end])
            elif end > merged[-1][1]: merged[-1][1] = end
        online_seconds = sum(max(0, int((end-start).total_seconds())) for start, end in merged)
        channel_row = db.query(AuditLog).filter(AuditLog.actor_user_id==u.id, AuditLog.action=='channel.open').order_by(desc(AuditLog.created_at)).first()
        creator = db.query(AuditLog).filter(AuditLog.action=='user.create', AuditLog.target_type=='user', AuditLog.target_id==str(u.id)).order_by(AuditLog.created_at.asc()).first()
        creator_user = db.get(User, creator.actor_user_id) if creator and creator.actor_user_id else None
        login_row = next((x for x in sessions if x.login_at), None)
        logout_row = next((x for x in sessions if x.logout_at), None)
        offline_since = None
        if not is_online and latest:
            last = latest.last_seen if latest.last_seen.tzinfo else latest.last_seen.replace(tzinfo=timezone.utc)
            offline_since = (latest.logout_at or (last + timedelta(seconds=90))).isoformat()
        out.append({'id':u.id,'username':u.username,'role':u.role,'active':u.active,'online':is_online,'login_at':utc_iso(latest.login_at if latest else None),'logout_at':utc_iso(logout_row.logout_at if logout_row else None),'online_since':utc_iso(latest.login_at if is_online and latest else None),'offline_since':offline_since,'online_seconds_today':online_seconds,'active_customers':active_count,'today_assigned':today_assignments,'today_messages':message_count,'total_messages':total_messages,'created_at':utc_iso(u.created_at),'created_by':creator_user.username if creator_user else '--','last_channel':channel_row.target_id if channel_row else '--','last_channel_at':utc_iso(channel_row.created_at if channel_row else None)})
    return {'employees':out,'online_count':sum(1 for x in out if x['online']),'active_conversations':sum(x['active_customers'] for x in out),'today_messages':sum(x['today_messages'] for x in out),'today_assignments':sum(x['today_assigned'] for x in out),'timezone':'Asia/Yangon'}

@app.get('/api/admin/stats')
def stats(db:Session=Depends(get_db), actor:User=Depends(admin)):
    local_now=datetime.now(ZoneInfo('Asia/Yangon'))
    local_start=local_now.replace(hour=0,minute=0,second=0,microsecond=0).astimezone(timezone.utc)
    users=db.query(User).all(); active_assignments=db.query(Assignment).filter_by(active=True).count()
    online=db.query(PresenceSession.user_id).filter(PresenceSession.is_active==True, PresenceSession.last_seen>=datetime.now(timezone.utc)-timedelta(seconds=90)).distinct().count()
    employee_roles=('employee','chatting','finding')
    return {'employees':len([u for u in users if u.role in employee_roles]),'online_count':online,'online_last_5m':online,'active_conversations':active_assignments,'today_received':db.query(Assignment).filter(Assignment.assigned_at>=local_start).count(),'today_messages':db.query(Message).filter(Message.direction=='out',Message.sent_at>=local_start).count(),'telegram_accounts':db.query(TgAccount).count(),'timezone':'Asia/Yangon'}

@app.get('/api/admin/audit')
def audit_logs(limit:int=100,db:Session=Depends(get_db),actor:User=Depends(admin)):
    rows=db.query(AuditLog).order_by(desc(AuditLog.created_at)).limit(min(limit,500)).all()
    return [{'actor_user_id':r.actor_user_id,'action':r.action,'target_type':r.target_type,'target_id':r.target_id,'detail':r.detail,'created_at':r.created_at.isoformat()} for r in rows]

@app.get('/api/health')
def health(): return {'ok':True,'service':'telegram-crm'}

@app.get('/api/health/ready')
def readiness(db: Session = Depends(get_db)):
    try:
        db.execute(text('SELECT 1'))
    except Exception as exc:
        raise HTTPException(503, f'Database is not ready: {type(exc).__name__}')
    return {'ok': True, 'database': 'connected'}

@app.get('/api/admin/diagnostics')
def diagnostics(db: Session = Depends(get_db), actor: User = Depends(admin)):
    try:
        db.execute(text('SELECT 1'))
        database_status = 'connected'
    except Exception as exc:
        database_status = f'error: {type(exc).__name__}'
    accounts = db.query(TgAccount).all() if database_status == 'connected' else []
    return {
        'database': database_status,
        'telegram_api_configured': bool(settings.telegram_api_id and settings.telegram_api_hash),
        'session_encryption_configured': bool(settings.session_encryption_key),
        'telegram_accounts_total': len(accounts),
        'telegram_accounts_connected': len([a for a in accounts if a.status == 'connected']),
    }
