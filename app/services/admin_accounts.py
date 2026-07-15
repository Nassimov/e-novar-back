"""Super-admin invite system — service layer.

The original env-var admin (see app/routers/admin/auth.py) is the permanent
super admin (PDG) and needs no row here. Every admin they invite gets a real
`AdminAccount` row: invited (no password/TOTP yet) -> accepts the invite
(sets a password + scans a freshly generated TOTP QR + confirms one code) ->
active. Suspended admins can't log in but keep their row (audit trail,
re-activatable). Deleting a row is permanent.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from uuid import UUID

import bcrypt
import pyotp
from sqlmodel import Session, select

from app.models.admin_account import AdminAccount, AdminAccountAuditLog

INVITE_TTL_HOURS = 72


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def _hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def log_action(
    db: Session,
    *,
    actor_admin_id: Optional[UUID],
    actor_label: Optional[str],
    target_admin_id: Optional[UUID],
    action: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    db.add(AdminAccountAuditLog(
        actor_admin_id=actor_admin_id,
        actor_label=actor_label,
        target_admin_id=target_admin_id,
        action=action,
        metadata_=metadata or {},
    ))


def create_invite(
    db: Session,
    *,
    email: str,
    role: str,
    invited_by_id: Optional[UUID],
    invited_by_label: str,
) -> str:
    """Create (or re-invite) an admin account. Returns the plaintext invite
    token — shown/emailed exactly once, never stored."""
    email = email.strip().lower()
    existing = db.exec(select(AdminAccount).where(AdminAccount.email == email)).first()

    plaintext = secrets.token_urlsafe(32)
    now = datetime.utcnow()
    expires_at = now + timedelta(hours=INVITE_TTL_HOURS)

    if existing:
        if existing.status == "active":
            raise ValueError("Un compte actif existe déjà pour cet email")
        existing.role = role
        existing.status = "invited"
        existing.invite_token_hash = _hash_token(plaintext)
        existing.invite_expires_at = expires_at
        existing.updated_at = now
        db.add(existing)
        target_id = existing.id
    else:
        account = AdminAccount(
            email=email,
            role=role,
            status="invited",
            invited_by=invited_by_id,
            invite_token_hash=_hash_token(plaintext),
            invite_expires_at=expires_at,
        )
        db.add(account)
        db.flush()
        target_id = account.id

    log_action(
        db, actor_admin_id=invited_by_id, actor_label=invited_by_label,
        target_admin_id=target_id, action="invited", metadata={"email": email, "role": role},
    )
    return plaintext


def find_by_invite_token(db: Session, plaintext: str) -> Optional[AdminAccount]:
    if not plaintext:
        return None
    token_hash = _hash_token(plaintext)
    account = db.exec(
        select(AdminAccount).where(AdminAccount.invite_token_hash == token_hash)
    ).first()
    if account is None:
        return None
    if account.status == "active":
        return None
    if account.invite_expires_at and datetime.utcnow() > account.invite_expires_at:
        return None
    return account


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def totp_provisioning_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name="E-NOVAR Admin")


def verify_totp(secret: str, code: str) -> bool:
    return pyotp.TOTP(secret).verify((code or "").strip(), valid_window=1)


def accept_invite(
    db: Session,
    account: AdminAccount,
    *,
    password: str,
) -> str:
    """Step 1 of enrollment: set the password, generate a fresh TOTP secret,
    return its provisioning URI (frontend renders the QR). The secret is
    stored immediately but the account only flips to 'active' once
    `confirm_totp_enrollment` verifies the very first code — this prevents
    an interrupted enrollment from leaving a half-set-up account that looks
    active but whose owner never actually saved the QR."""
    account.password_hash = hash_password(password)
    secret = generate_totp_secret()
    account.totp_secret = secret
    account.invite_token_hash = None
    account.updated_at = datetime.utcnow()
    db.add(account)
    log_action(db, actor_admin_id=None, actor_label=account.email, target_admin_id=account.id,
               action="invite_password_set")
    return totp_provisioning_uri(secret, account.email)


def confirm_totp_enrollment(db: Session, account: AdminAccount, code: str) -> bool:
    if not account.totp_secret or not verify_totp(account.totp_secret, code):
        return False
    account.status = "active"
    account.updated_at = datetime.utcnow()
    db.add(account)
    log_action(db, actor_admin_id=None, actor_label=account.email, target_admin_id=account.id,
               action="enrollment_confirmed")
    return True
