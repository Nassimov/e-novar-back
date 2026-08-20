"""Multi-admin accounts (migration 064) — super-admin invite system.

The original env-var admin (ADMIN_EMAIL/ADMIN_PASSWORD_HASH/ADMIN_2FA_SECRET,
see app/routers/admin/auth.py) keeps working exactly as before and acts as
the permanent super admin. Every OTHER admin is a real row in this table,
invited by an existing admin, with its own bcrypt password hash and its own
TOTP secret. See app/services/admin_accounts.py for the invite/enrollment/
login logic.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class AdminAccount(SQLModel, table=True):
    __tablename__ = "admin_accounts"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    email: str = Field(sa_column=sa.Column(sa.Text, unique=True, nullable=False))
    password_hash: Optional[str] = Field(default=None)
    totp_secret: Optional[str] = Field(default=None)
    role: str = Field(default="admin")           # 'super_admin' | 'admin'
    status: str = Field(default="invited")        # 'invited' | 'active' | 'suspended'
    invited_by: Optional[UUID] = Field(
        default=None,
        sa_column=sa.Column(sa.ForeignKey("admin_accounts.id", ondelete="SET NULL"), nullable=True),
    )
    invite_token_hash: Optional[str] = Field(default=None)
    invite_expires_at: Optional[datetime] = Field(default=None)
    last_login_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AdminAccountAuditLog(SQLModel, table=True):
    __tablename__ = "admin_account_audit_log"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    actor_admin_id: Optional[UUID] = Field(
        default=None,
        sa_column=sa.Column(sa.ForeignKey("admin_accounts.id", ondelete="SET NULL"), nullable=True),
    )
    actor_label: Optional[str] = Field(default=None)
    target_admin_id: Optional[UUID] = Field(
        default=None,
        sa_column=sa.Column(sa.ForeignKey("admin_accounts.id", ondelete="SET NULL"), nullable=True),
    )
    action: str = Field()
    metadata_: Optional[Any] = Field(default=None, sa_column=sa.Column("metadata", JSONB, nullable=True))
    created_at: datetime = Field(default_factory=datetime.utcnow)
