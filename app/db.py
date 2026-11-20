import os, shutil
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from .config import settings

url = settings.database_url
# One-time migration for deployments that previously used the container-local
# SQLite file before a Railway Volume was mounted at /data.
if url == "sqlite:////data/xwallet.db" and os.path.isdir("/data") and not os.path.exists("/data/xwallet.db"):
    legacy = os.path.abspath("./telegram_crm.db")
    if os.path.isfile(legacy):
        shutil.copy2(legacy, "/data/xwallet.db")
if url.startswith("postgres://"):
    url = url.replace("postgres://", "postgresql+psycopg://", 1)
elif url.startswith("postgresql://") and "+psycopg" not in url:
    url = url.replace("postgresql://", "postgresql+psycopg://", 1)
# Keep the SQLAlchemy pool bounded for Railway PostgreSQL, but do not let
# Telegram sync jobs monopolize connections (those jobs use short DB sessions).
# Values are configurable for Railway plans with different Postgres limits.
if url.startswith("sqlite"):
    engine_options = {
        "pool_pre_ping": True,
        "connect_args": {"check_same_thread": False},
    }
else:
    engine_options = {
        "pool_pre_ping": True,
        "pool_size": max(2, int(os.getenv("DB_POOL_SIZE", "8"))),
        "max_overflow": max(0, int(os.getenv("DB_MAX_OVERFLOW", "12"))),
        "pool_timeout": max(5, int(os.getenv("DB_POOL_TIMEOUT", "20"))),
        "pool_recycle": max(60, int(os.getenv("DB_POOL_RECYCLE", "1800"))),
        "pool_use_lifo": True,
        "connect_args": {"connect_timeout": 10, "application_name": "xwallet-chat-server"},
    }
engine = create_engine(url, **engine_options)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

class Base(DeclarativeBase):
    pass

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
