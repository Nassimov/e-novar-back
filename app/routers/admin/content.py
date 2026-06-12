from __future__ import annotations

import math
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlmodel import Session, select

from app.dependencies import get_admin_user, get_db
from app.models.teacher import TeacherPayout, TeacherWithdrawal
from app.models.user import User
from app.schemas.admin import WithdrawalProcessRequest

router = APIRouter(tags=["admin-content"])



# In-memory content store (replace with DB tables in production)
_subjects: List[Dict] = []
_levels: List[Dict] = []
_store_rewards: List[Dict] = []


class SubjectCreate(BaseModel):
    name: str
    icon: Optional[str] = None
    is_active: bool = True


class LevelCreate(BaseModel):
    name: str
    group: str
    order: int = 0


class StoreRewardCreate(BaseModel):
    name: str
    description: str
    kp_cost: int
    type: str
    icon: Optional[str] = None


# Subjects CRUD
@router.get("/subjects")
async def list_subjects(current_user: Dict[str, Any] = Depends(get_admin_user)):
    """List all subjects."""
    from app.routers.catalogs import SUBJECTS
    return {"items": [{"id": str(i), "name": s} for i, s in enumerate(SUBJECTS)], "total": len(SUBJECTS)}


@router.post("/subjects", status_code=status.HTTP_201_CREATED)
async def create_subject(
    payload: SubjectCreate,
    current_user: Dict[str, Any] = Depends(get_admin_user),
):
    """Create a new subject."""
    import uuid
    new_subject = {"id": str(uuid.uuid4()), "name": payload.name, "icon": payload.icon, "is_active": payload.is_active}
    _subjects.append(new_subject)
    return new_subject


@router.put("/subjects/{subject_id}")
async def update_subject(
    subject_id: str,
    payload: SubjectCreate,
    current_user: Dict[str, Any] = Depends(get_admin_user),
):
    """Update a subject."""
    for s in _subjects:
        if s["id"] == subject_id:
            s.update({"name": payload.name, "icon": payload.icon, "is_active": payload.is_active})
            return {"message": "Subject updated", **s}
    raise HTTPException(status_code=404, detail="Subject not found")


@router.delete("/subjects/{subject_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_subject(
    subject_id: str,
    current_user: Dict[str, Any] = Depends(get_admin_user),
):
    """Delete a subject."""
    global _subjects
    _subjects = [s for s in _subjects if s["id"] != subject_id]
    return None


# Levels CRUD
@router.get("/levels")
async def list_levels_admin(current_user: Dict[str, Any] = Depends(get_admin_user)):
    """List all levels."""
    from app.routers.catalogs import LEVELS
    return {"items": [{"id": str(i), "name": l} for i, l in enumerate(LEVELS)], "total": len(LEVELS)}


@router.post("/levels", status_code=status.HTTP_201_CREATED)
async def create_level(
    payload: LevelCreate,
    current_user: Dict[str, Any] = Depends(get_admin_user),
):
    """Create a new level."""
    import uuid
    new_level = {"id": str(uuid.uuid4()), "name": payload.name, "group": payload.group, "order": payload.order}
    _levels.append(new_level)
    return new_level


# Store rewards CRUD
@router.get("/store-rewards")
async def list_store_rewards_admin(current_user: Dict[str, Any] = Depends(get_admin_user)):
    """List all store rewards."""
    from app.routers.catalogs import STORE_REWARDS
    return {"items": STORE_REWARDS, "total": len(STORE_REWARDS)}


@router.post("/store-rewards", status_code=status.HTTP_201_CREATED)
async def create_store_reward(
    payload: StoreRewardCreate,
    current_user: Dict[str, Any] = Depends(get_admin_user),
):
    """Create a new store reward."""
    import uuid
    reward = {
        "id": str(uuid.uuid4()),
        "name": payload.name,
        "description": payload.description,
        "kp_cost": payload.kp_cost,
        "type": payload.type,
        "icon": payload.icon,
    }
    _store_rewards.append(reward)
    return reward


# Payout management (EP → DZD)
@router.get("/withdrawals")
async def list_withdrawals(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status_filter: Optional[str] = Query(None, alias="status"),
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """List all EP→DZD payout requests."""
    query = select(TeacherPayout)
    if status_filter:
        query = query.where(TeacherPayout.status == status_filter)

    payouts = db.exec(query.order_by(TeacherPayout.requested_at.desc())).all()
    total = len(payouts)
    offset = (page - 1) * size
    paginated = payouts[offset: offset + size]

    return {
        "items": [
            {
                "id": str(p.id),
                "teacher_id": str(p.teacher_id),
                "ep_amount": p.ep_amount,
                "dzd_amount": p.dzd_amount,
                "status": p.status,
                "iban": p.iban,
                "bank_holder": p.bank_holder,
                "admin_note": p.admin_note,
                "requested_at": p.requested_at.isoformat(),
                "processed_at": p.processed_at.isoformat() if p.processed_at else None,
            }
            for p in paginated
        ],
        "total": total,
        "page": page,
        "size": size,
        "pages": math.ceil(total / size) if total else 0,
    }


@router.put("/withdrawals/{withdrawal_id}")
async def process_withdrawal(
    withdrawal_id: UUID,
    payload: WithdrawalProcessRequest,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Approve or reject an EP→DZD payout request.
    On approve: deducts EP via kp_transactions (trigger updates kp_balances).
    """
    from datetime import datetime

    payout = db.get(TeacherPayout, withdrawal_id)
    if payout is None:
        raise HTTPException(status_code=404, detail="Payout request not found")
    if payout.status != "pending":
        raise HTTPException(status_code=400, detail="Only pending payouts can be processed")

    if payload.action == "approve":
        if payload.dzd_amount is None:
            raise HTTPException(status_code=400, detail="dzd_amount requis pour approuver un retrait")
        payout.status = "approved"
        payout.dzd_amount = payload.dzd_amount
        payout.processed_at = datetime.utcnow()

        # Deduct EP from teacher's balance via kp_transactions
        from app.models.kp import KpTransaction
        db.add(KpTransaction(
            user_id=payout.teacher_id,
            amount=-payout.ep_amount,
            source="reward",
            label=f"Retrait EP → DZD ({payload.dzd_amount} DZD)",
            ref_type="payout",
            ref_id=payout.id,
        ))

    elif payload.action == "reject":
        payout.status = "rejected"
        payout.processed_at = datetime.utcnow()
    else:
        raise HTTPException(status_code=400, detail="action invalide. Utilise 'approve' ou 'reject'")

    if payload.admin_note:
        payout.admin_note = payload.admin_note

    db.add(payout)
    db.commit()
    return {"message": f"Payout {payload.action}d", "payout_id": str(withdrawal_id)}
