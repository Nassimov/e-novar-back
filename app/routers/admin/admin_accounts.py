"""Super-admin invite system — manage OTHER admins.

The original env-var admin (app/routers/admin/auth.py) is the permanent
super admin and has no row in admin_accounts — it's represented here as a
synthetic, undeletable entry. Only a super_admin can invite/suspend/delete
other admins (see app.dependencies.require_super_admin).
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlmodel import Session, select

from app.config import get_settings
from app.core.redis import get_redis_client
from app.dependencies import get_db, require_super_admin
from app.models.admin_account import AdminAccount, AdminAccountAuditLog
from app.services import admin_accounts as svc

router = APIRouter(tags=["Admin — Accounts"])
settings = get_settings()

_ENROLLMENT_TTL = 900  # 15 min to scan the QR and confirm a code


class InviteRequest(BaseModel):
    email: EmailStr
    role: str = "admin"  # 'admin' | 'super_admin'


class AcceptInviteRequest(BaseModel):
    token: str
    password: str


class ConfirmEnrollmentRequest(BaseModel):
    enrollment_token: str
    code: str


def _actor_label(current_user: Dict[str, Any]) -> str:
    return current_user.get("email") or "super admin"


def _actor_id(current_user: Dict[str, Any]) -> Optional[UUID]:
    """admin_accounts.id of the acting admin — None for the env-var bootstrap
    super admin. Note: current_user["id"] is always None (see
    app.dependencies.get_admin_user) — that key must never be treated as an
    admin_accounts id, only "admin_account_id" is."""
    raw = current_user.get("admin_account_id")
    return UUID(raw) if raw else None


@router.get("/")
def list_admin_accounts(
    current_user: Dict[str, Any] = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    accounts = db.exec(select(AdminAccount).order_by(AdminAccount.created_at.desc())).all()
    items = [
        {
            "id": "bootstrap",
            "email": settings.admin_email,
            "role": "super_admin",
            "status": "active",
            "is_bootstrap": True,
            "invited_by": None,
            "last_login_at": None,
            "created_at": None,
        }
    ]
    for a in accounts:
        items.append({
            "id": str(a.id),
            "email": a.email,
            "role": a.role,
            "status": a.status,
            "is_bootstrap": False,
            "invited_by": str(a.invited_by) if a.invited_by else None,
            "last_login_at": a.last_login_at.isoformat() if a.last_login_at else None,
            "created_at": a.created_at.isoformat(),
        })
    return items


@router.post("/invite", status_code=201)
async def invite_admin(
    payload: InviteRequest,
    current_user: Dict[str, Any] = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    if payload.role not in ("admin", "super_admin"):
        raise HTTPException(status_code=400, detail="Invalid role")

    try:
        plaintext_token = svc.create_invite(
            db, email=payload.email, role=payload.role,
            invited_by_id=_actor_id(current_user), invited_by_label=_actor_label(current_user),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    db.commit()

    invite_url = f"{settings.frontend_url}/admin/accept-invite?token={plaintext_token}"
    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#111;">
      <h1 style="color:#4F46E5;">Invitation administrateur E-NOVAR</h1>
      <p>Vous avez été invité(e) à rejoindre la console d'administration E-NOVAR.</p>
      <p><a href="{invite_url}" style="background:#4F46E5;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:bold;">Configurer mon compte</a></p>
      <p style="color:#6B7280;font-size:13px;">Ce lien expire dans {svc.INVITE_TTL_HOURS} heures et ne peut être utilisé qu'une seule fois.</p>
    </body></html>
    """
    from app.services.onesignal import send_email
    result = send_email(to=payload.email, subject="Invitation administrateur E-NOVAR", html=html)

    response: Dict[str, Any] = {"message": f"Invitation envoyée à {payload.email}"}
    if result.get("status") in ("skipped", "error"):
        # Either the email provider isn't configured, or OneSignal genuinely
        # couldn't deliver (e.g. a brand-new address it had never seen before
        # and the registration retry in onesignal.send_email still failed) —
        # either way, never leave the admin with just a false "sent"
        # confirmation and no way to actually get the invite to the person.
        response["invite_url"] = invite_url
        response["message"] = (
            "Email non configuré sur cet environnement — lien d'invitation généré ci-dessous."
            if result.get("status") == "skipped"
            else "L'email n'a pas pu être délivré — partage ce lien d'invitation directement."
        )
    return response


@router.post("/{account_id}/resend-invite")
async def resend_invite(
    account_id: UUID,
    current_user: Dict[str, Any] = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    account = db.get(AdminAccount, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return await invite_admin(
        InviteRequest(email=account.email, role=account.role), current_user, db  # type: ignore[arg-type]
    )


@router.post("/{account_id}/suspend")
def suspend_admin(
    account_id: UUID,
    current_user: Dict[str, Any] = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    account = db.get(AdminAccount, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    account.status = "suspended"
    account.updated_at = datetime.utcnow()
    db.add(account)
    svc.log_action(db, actor_admin_id=_actor_id(current_user), actor_label=_actor_label(current_user),
                   target_admin_id=account.id, action="suspended")
    db.commit()
    return {"status": "suspended"}


@router.post("/{account_id}/reinstate")
def reinstate_admin(
    account_id: UUID,
    current_user: Dict[str, Any] = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    account = db.get(AdminAccount, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    if account.status != "suspended":
        raise HTTPException(status_code=409, detail="Only a suspended account can be reinstated")
    account.status = "active"
    account.updated_at = datetime.utcnow()
    db.add(account)
    svc.log_action(db, actor_admin_id=_actor_id(current_user), actor_label=_actor_label(current_user),
                   target_admin_id=account.id, action="reinstated")
    db.commit()
    return {"status": "active"}


@router.delete("/{account_id}", status_code=204)
def delete_admin(
    account_id: UUID,
    current_user: Dict[str, Any] = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    account = db.get(AdminAccount, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    if str(account.id) == current_user.get("admin_account_id"):
        raise HTTPException(status_code=400, detail="Vous ne pouvez pas supprimer votre propre compte")

    svc.log_action(db, actor_admin_id=_actor_id(current_user), actor_label=_actor_label(current_user),
                   target_admin_id=account.id, action="deleted", metadata={"email": account.email})
    db.delete(account)
    db.commit()
    return None


# ─── Public invite-acceptance flow (no admin session exists yet) ──────────────

@router.post("/accept-invite")
def accept_invite(payload: AcceptInviteRequest, db: Session = Depends(get_db)):
    account = svc.find_by_invite_token(db, payload.token)
    if account is None:
        raise HTTPException(status_code=400, detail="Lien d'invitation invalide ou expiré")
    if len(payload.password) < 10:
        raise HTTPException(status_code=400, detail="Le mot de passe doit contenir au moins 10 caractères")

    provisioning_uri = svc.accept_invite(db, account, password=payload.password)
    db.commit()

    enrollment_token = secrets.token_urlsafe(32)
    get_redis_client().setex(
        f"admin:enrollment:{enrollment_token}", _ENROLLMENT_TTL, json.dumps({"account_id": str(account.id)})
    )

    return {
        "enrollment_token": enrollment_token,
        "provisioning_uri": provisioning_uri,
        "email": account.email,
    }


@router.post("/confirm-enrollment")
def confirm_enrollment(payload: ConfirmEnrollmentRequest, db: Session = Depends(get_db)):
    raw = get_redis_client().get(f"admin:enrollment:{payload.enrollment_token}")
    if not raw:
        raise HTTPException(status_code=400, detail="Session d'inscription expirée — recommencez l'invitation")
    data = json.loads(raw)
    account = db.get(AdminAccount, UUID(data["account_id"]))
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    if not svc.confirm_totp_enrollment(db, account, payload.code):
        raise HTTPException(status_code=400, detail="Code invalide")

    db.commit()
    get_redis_client().delete(f"admin:enrollment:{payload.enrollment_token}")
    return {"status": "active"}
