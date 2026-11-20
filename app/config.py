import os
import secrets
from dataclasses import dataclass

@dataclass
class Settings:
    # Prefer Railway PostgreSQL when DATABASE_URL is supplied. If a Railway
    # Volume is mounted at /data and no database URL is configured, persist
    # SQLite there instead of inside the disposable container filesystem.
    database_url: str = os.getenv("DATABASE_URL") or ("sqlite:////data/xwallet.db" if os.path.isdir("/data") else "sqlite:///./telegram_crm.db")
    # Production must provide a unique strong secret through Railway Variables.
    # Local development gets a per-process random key instead of a public fallback.
    jwt_secret: str = os.getenv("JWT_SECRET", "") or secrets.token_urlsafe(48)
    session_encryption_key: str = os.getenv("SESSION_ENCRYPTION_KEY", "")
    telegram_api_id: str = os.getenv("TELEGRAM_API_ID", "")
    telegram_api_hash: str = os.getenv("TELEGRAM_API_HASH", "")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    transcription_model: str = os.getenv("OPENAI_TRANSCRIPTION_MODEL", "gpt-4o-mini-transcribe")
    admin_username: str = os.getenv("ADMIN_USERNAME", "admin")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "")
    app_base_url: str = os.getenv("APP_BASE_URL", os.getenv("PUBLIC_APP_URL", "https://xwalletchatserver-production.up.railway.app"))
    # Comma-separated exact origins. Empty means same-origin APP_BASE_URL only.
    cors_origins: str = os.getenv("CORS_ORIGINS", "")
    upload_dir: str = os.getenv("UPLOAD_DIR") or ("/data/uploads" if os.path.isdir("/data") else "./uploads")
    attendance_ingest_secret: str = os.getenv("ATTENDANCE_INGEST_SECRET", "")

settings = Settings()
# Never silently run a Railway production deployment on an ephemeral SQLite file.
# Link the Postgres service's DATABASE_URL to this service in Railway Variables.
if (os.getenv('RAILWAY_ENVIRONMENT') or os.getenv('RAILWAY_PROJECT_ID')) and not os.getenv('DATABASE_URL'):
    raise RuntimeError('DATABASE_URL is missing. In Railway, add a variable reference from the Postgres service: DATABASE_URL -> DATABASE_URL.')


# Fail closed in Railway rather than silently accepting a public/default JWT key.
_is_railway = bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID"))
if len(settings.jwt_secret) < 32 or settings.jwt_secret == "CHANGE_ME_TO_A_LONG_RANDOM_SECRET":
    raise RuntimeError("JWT_SECRET must contain at least 32 characters. Set a unique random value in Railway Variables.")
if _is_railway and not os.getenv("JWT_SECRET"):
    raise RuntimeError("JWT_SECRET is required in Railway Variables; refusing to start with an ephemeral development key.")
