"""Admin DZD wallet oversight — read a teacher's wallet ledger, and the
only entry point allowed to manually correct a wallet balance (business
audit, 2026-09-08). Mirrors app/routers/admin/kp.py's EP equivalent.
Every adjustment is journaled twice: as a teacher_wallet_transactions row
(transaction_type='adjustment', actor_id=admin) and as an audit_logs row —
see app/services/wallet.py's adjust_wallet_balance()."""
from __future__ import annotations

import math
from typing import Any, Dict
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.dependencies import get_admin_user, get_db
from app.models.profile import TeacherProfile
from app.models.wallet import TeacherWalletTransaction
from app.services.wallet import adjust_wallet_balance

router = APIRouter(tags=["Admin — Wallet"])


class AdminWalletAdjustRequest(BaseModel):
    delta: int = Field(..., description="Signed DZD amount to apply — positive credits, negative debits.")
    reason: str = Field(..., min_length=3, max_length=500)


@router.get("/{teacher_id}/balance")
def admin_get_wallet_balance(
    teacher_id: UUID,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    tp = db.get(TeacherProfile, teacher_id)
    if tp is None:
        raise HTTPException(status_code=404, detail="Teacher profile not found")
    return {"teacher_id": str(tp.user_id), "wallet_balance_dzd": tp.wallet_balance_dzd}


@router.get("/{teacher_id}/transactions")
def admin_list_wallet_transactions(
    teacher_id: UUID,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    stmt = select(TeacherWalletTransaction).where(TeacherWalletTransaction.teacher_id == teacher_id)
    total = len(db.exec(stmt).all())
    rows = db.exec(
        stmt.order_by(TeacherWalletTransaction.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
    ).all()
    return {
        "items": [
            {
                "id": str(t.id),
                "amount": t.amount,
                "transaction_type": t.transaction_type,
                "source": t.source,
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


@router.post("/{teacher_id}/adjust")
def admin_adjust_wallet_balance(
    teacher_id: UUID,
    payload: AdminWalletAdjustRequest,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Manually credit or debit a teacher's DZD wallet, with a mandatory
    reason. Never lets the balance go negative — use a smaller delta if the
    balance can't cover a debit."""
    try:
        tp = adjust_wallet_balance(
            teacher_id=teacher_id, delta=payload.delta, reason=payload.reason,
            actor_id=UUID(current_user["id"]), db=db,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "teacher_id": str(tp.user_id),
        "new_balance": tp.wallet_balance_dzd,
        "delta": payload.delta,
        "reason": payload.reason,
    }
