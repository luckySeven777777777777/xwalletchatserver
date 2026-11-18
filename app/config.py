import os
from dataclasses import dataclass

@dataclass
class Settings:
    # Prefer Railway PostgreSQL when DATABASE_URL is supplied. If a Railway
    # Volume is mounted at /data and no database URL is configured, persist
    # SQLite there instead of inside the disposable container filesystem.
    database_url: str = os.getenv("DATABASE_URL") or ("sqlite:////data/xwallet.db" if os.path.isdir("/data") else "sqlite:///./telegram_crm.db")
    jwt_secret: str = os.getenv("JWT_SECRET", "CHANGE_ME_TO_A_LONG_RANDOM_SECRET")
    session_encryption_key: str = os.getenv("SESSION_ENCRYPTION_KEY", "")
    telegram_api_id: str = os.getenv("TELEGRAM_API_ID", "")
    telegram_api_hash: str = os.getenv("TELEGRAM_API_HASH", "")
    admin_username: str = os.getenv("ADMIN_USERNAME", "admin")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "")
    app_base_url: str = os.getenv("APP_BASE_URL", os.getenv("PUBLIC_APP_URL", "https://xwalletchatserver-production.up.railway.app"))
    cors_origins: str = os.getenv("CORS_ORIGINS", "*")
    upload_dir: str = os.getenv("UPLOAD_DIR") or ("/data/uploads" if os.path.isdir("/data") else "./uploads")

settings = Settings()
# Never silently run a Railway production deployment on an ephemeral SQLite file.
# Link the Postgres service's DATABASE_URL to this service in Railway Variables.
if (os.getenv('RAILWAY_ENVIRONMENT') or os.getenv('RAILWAY_PROJECT_ID')) and not os.getenv('DATABASE_URL'):
    raise RuntimeError('DATABASE_URL is missing. In Railway, add a variable reference from the Postgres service: DATABASE_URL -> DATABASE_URL.')
