from datetime import datetime, timezone
from sqlalchemy import String, Integer, Boolean, DateTime, Text, ForeignKey, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .db import Base

def now(): return datetime.now(timezone.utc)

class User(Base):
    __tablename__ = 'users'
    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str | None] = mapped_column(String(6), unique=True, nullable=True, index=True)
    totp_secret: Mapped[str | None] = mapped_column(String(255), nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default='employee')
    team_id: Mapped[int | None] = mapped_column(ForeignKey('teams.id'), nullable=True)
    created_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    permissions: Mapped[str] = mapped_column(Text, default='[]')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class Team(Base):
    __tablename__ = 'teams'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class TgAccount(Base):
    __tablename__ = 'telegram_accounts'
    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(120))
    phone_masked: Mapped[str] = mapped_column(String(40), default='')
    session_ciphertext: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default='connected')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class Customer(Base):
    __tablename__ = 'customers'
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey('telegram_accounts.id'), index=True)
    telegram_peer_id: Mapped[str] = mapped_column(String(100), index=True)
    telegram_access_hash: Mapped[str] = mapped_column(String(100), default='')
    real_username: Mapped[str] = mapped_column(String(255), default='')
    real_name: Mapped[str] = mapped_column(String(255), default='')
    phone: Mapped[str] = mapped_column(String(80), default='')
    alias: Mapped[str] = mapped_column(String(120), default='')
    avatar_path: Mapped[str] = mapped_column(String(500), default='')
    # Telegram dialog metadata is admin-visible; Saved Messages is private to admins.
    dialog_folder: Mapped[int] = mapped_column(Integer, default=0)
    is_saved_messages: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint('account_id','telegram_peer_id', name='uq_customer_peer'),)

class Assignment(Base):
    __tablename__ = 'assignments'
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey('customers.id'), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    assigned_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class Message(Base):
    __tablename__ = 'messages'
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey('customers.id'), index=True)
    telegram_message_id: Mapped[str] = mapped_column(String(100))
    direction: Mapped[str] = mapped_column(String(10))
    body: Mapped[str] = mapped_column(Text, default='')
    media_path: Mapped[str] = mapped_column(Text, default='')
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    sender_label: Mapped[str] = mapped_column(String(120), default='Customer')
    __table_args__ = (UniqueConstraint('customer_id','telegram_message_id', name='uq_message_tg'),)

class ReadState(Base):
    __tablename__ = 'read_states'
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey('customers.id'), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    last_read_message_id: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (UniqueConstraint('customer_id','user_id', name='uq_read_state'),)

class Tag(Base):
    __tablename__ = 'tags'
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey('customers.id'), index=True)
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    team_id: Mapped[int | None] = mapped_column(ForeignKey('teams.id'), nullable=True)
    scope: Mapped[str] = mapped_column(String(20)) # private or team
    key: Mapped[str] = mapped_column(String(80))
    value: Mapped[str] = mapped_column(String(500))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)

class AuditLog(Base):
    __tablename__ = 'audit_logs'
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    action: Mapped[str] = mapped_column(String(100))
    target_type: Mapped[str] = mapped_column(String(50), default='')
    target_id: Mapped[str] = mapped_column(String(100), default='')
    detail: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PresenceSession(Base):
    __tablename__ = 'presence_sessions'
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    session_key: Mapped[str] = mapped_column(String(120))
    login_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    logout_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    channel: Mapped[str] = mapped_column(String(40), default='Telegram')
    __table_args__ = (UniqueConstraint('user_id', 'session_key', name='uq_presence_user_session'),)
