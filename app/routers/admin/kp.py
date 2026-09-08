"""Admin EP (KP) balance oversight — read history, and the only entry point
allowed to manually correct a user's balance (business/EP audit, 2026-09-08).
Every adjustment is journaled twice: as a kp_transactions row
(transaction_type='adjustment', actor_id=admin) and as an audit_logs row —
see app/services/kp.py's adjust_kp_balance()."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlmodel import Session, select

from app.dependencies import get_admin_user, get_db
from app.models.enums import KpTransactionType
from app.models.kp import KpAccount, KpTransaction
from app.models.profile import Profile
from app.services.kp import adjust_kp_balance, get_or_create_kp_account

router = APIRouter(tags=["Admin — EP"])


class AdminKpAdjustRequest(BaseModel):
    delta: int = Field(..., description="Signed amount to apply — positive credits, negative debits.")
    reason: str = Field(..., min_length=3, max_length=500)


@router.get("/{user_id}/balance")
def admin_get_kp_balance(
    user_id: UUID,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    account = get_or_create_kp_account(user_id, db)
    return {
        "user_id": str(account.user_id),
        "balance": account.balance,
        "total_earned": account.total_earned,
        "week_earned": account.week_earned,
        "level": account.level,
        "xp": account.xp,
    }


@router.get("/{user_id}/transactions")
def admin_list_kp_transactions(
    user_id: UUID,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    stmt = select(KpTransaction).where(KpTransaction.user_id == user_id)
    total = len(db.exec(stmt).all())
    rows = db.exec(
        stmt.order_by(KpTransaction.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
    ).all()
    return {
        "items": [
            {
                "id": str(t.id),
                "amount": t.amount,
                "source": t.source,
                "transaction_type": t.transaction_type,
                "label": t.label,
                "ref_type": t.ref_type,
                "ref_id": str(t.ref_id) if t.ref_id else None,
                "actor_id": str(t.actor_id) if t.actor_id else None,
                "created_at": t.created_at.isoformat(),
            }
            for t in rows
        ],
        "total": total,
        "page": page,
        "size": size,
        "pages": math.ceil(total / size) if total else 0,
    }


@router.get("/suspicious")
def admin_list_suspicious_kp_velocity(
    hours: int = Query(24, ge=1, le=168),
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Monitoring aid for multi-account/farming review (Point 5.3) — lists
    users whose EP earned in the last `hours` crosses
    PlatformSettings.kp_suspicious_daily_threshold. Purely informational:
    nothing here blocks, rate-limits, or flags a user automatically — an
    admin decides what (if anything) to do, e.g. via POST .../adjust."""
    from app.services.pricing import get_platform_settings

    threshold = get_platform_settings(db).kp_suspicious_daily_threshold
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    rows = db.exec(
        select(
            KpTransaction.user_id,
            func.sum(KpTransaction.amount).label("earned"),
        )
        .where(
            KpTransaction.transaction_type == KpTransactionType.earn.value,
            KpTransaction.created_at >= since,
        )
        .group_by(KpTransaction.user_id)
        .having(func.sum(KpTransaction.amount) >= threshold)
        .order_by(func.sum(KpTransaction.amount).desc())
    ).all()

    user_ids = [r[0] for r in rows]
    profiles = {
        p.id: p for p in db.exec(select(Profile).where(Profile.id.in_(user_ids))).all()
    } if user_ids else {}

    return {
        "threshold": threshold,
        "window_hours": hours,
        "items": [
            {
                "user_id": str(uid),
                "full_name": (profiles.get(uid).full_name if profiles.get(uid) else None) or "—",
                "earned_in_window": int(earned),
            }
            for uid, earned in rows
        ],
    }


@router.post("/{user_id}/adjust")
def admin_adjust_kp_balance(
    user_id: UUID,
    payload: AdminKpAdjustRequest,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Manually credit or debit a user's EP balance, with a mandatory
    reason. Never lets the balance go negative (same floor as a normal
    spend) — use a smaller delta if the user's balance can't cover a debit."""
    try:
        account = adjust_kp_balance(
            user_id=user_id,
            delta=payload.delta,
            reason=payload.reason,
            actor_id=UUID(current_user["id"]),
            db=db,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "user_id": str(account.user_id),
        "new_balance": account.balance,
        "delta": payload.delta,
        "reason": payload.reason,
    }
