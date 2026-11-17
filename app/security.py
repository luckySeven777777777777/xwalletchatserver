from datetime import datetime, timedelta, timezone
from jose import jwt, JWTError
from passlib.context import CryptContext
from cryptography.fernet import Fernet, InvalidToken
from .config import settings

pwd = CryptContext(schemes=['bcrypt'], deprecated='auto')
ALGORITHM = 'HS256'

def hash_password(value: str) -> str: return pwd.hash(value)
def verify_password(value: str, hashed: str) -> bool: return pwd.verify(value, hashed)
def create_token(user_id: int, role: str) -> str:
    exp = datetime.now(timezone.utc) + timedelta(hours=12)
    return jwt.encode({'sub': str(user_id), 'role': role, 'exp': exp}, settings.jwt_secret, algorithm=ALGORITHM)
def create_2fa_challenge(user_id: int) -> str:
    exp = datetime.now(timezone.utc) + timedelta(minutes=5)
    return jwt.encode({'sub': str(user_id), 'purpose': '2fa_login', 'exp': exp}, settings.jwt_secret, algorithm=ALGORITHM)

def decode_token(token: str):
    try: return jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
    except JWTError: return None

def _fernet():
    if not settings.session_encryption_key:
        raise RuntimeError('SESSION_ENCRYPTION_KEY is required before connecting Telegram accounts')
    try: return Fernet(settings.session_encryption_key.encode())
    except Exception as e: raise RuntimeError('SESSION_ENCRYPTION_KEY must be a valid Fernet key') from e

def encrypt_session(value: str) -> str: return _fernet().encrypt(value.encode()).decode()
def decrypt_session(value: str) -> str: return _fernet().decrypt(value.encode()).decode()
