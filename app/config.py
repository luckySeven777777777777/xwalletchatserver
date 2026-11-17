import os
from dataclasses import dataclass

@dataclass
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./telegram_crm.db")
    jwt_secret: str = os.getenv("JWT_SECRET", "CHANGE_ME_TO_A_LONG_RANDOM_SECRET")
    session_encryption_key: str = os.getenv("SESSION_ENCRYPTION_KEY", "")
    telegram_api_id: str = os.getenv("TELEGRAM_API_ID", "")
    telegram_api_hash: str = os.getenv("TELEGRAM_API_HASH", "")
    admin_username: str = os.getenv("ADMIN_USERNAME", "admin")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "")
    app_base_url: str = os.getenv("APP_BASE_URL", os.getenv("PUBLIC_APP_URL", ""))
    cors_origins: str = os.getenv("CORS_ORIGINS", "*")

settings = Settings()
