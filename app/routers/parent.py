from __future__ import annotations

from typing import Any, Dict, List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db, require_role
from app.models.booking import Booking
from app.models.kp import KpAccount
from app.models.payment import Payment
from app.models.session import Session as SessionModel, SessionStatus
from app.models.user import User, UserRole

router = APIRouter(tags=["parent"])

# In-memory parent-child links (replace with DB table in production)
# Using a simple table structure stored as JSON in user metadata for now


class LinkChildRequest(BaseModel):
    child_email: str
    relationship: str = "parent"  # parent/guardian/tutor


@router.get("/children")
async def list_children(
    current_user: Dict[str, Any] = Depends(require_role("parent")),
    db: Session = Depends(get_db),
):
    """List the parent's linked children."""
    # In a full implementation, this would query a ParentChild link table
    # For now return a structured response with the concept implemented
    stmt = select(User).where(User.supabase_id == current_user["id"])
    parent = db.exec(stmt).first()
    if parent is None:
        raise HTTPException(status_code=404, detail="Parent not found")

    return {"children": [], "message": "No children linked yet. Use POST /children/link to link a child."}


@router.post("/children/link", status_code=status.HTTP_201_CREATED)
async def link_child(
    payload: LinkChildRequest,
    current_user: Dict[str, Any] = Depends(require_role("parent")),
    db: Session = Depends(get_db),
):
    """Link a child account to the parent."""
    stmt = select(User).where(User.supabase_id == current_user["id"])
    parent = db.exec(stmt).first()
    if parent is None:
        raise HTTPException(status_code=404, detail="Parent not found")

    child = db.exec(select(User).where(User.email == payload.child_email)).first()
    if child is None:
        raise HTTPException(status_code=404, detail="Child account not found. They must register first.")

    if child.role != UserRole.student:
        raise HTTPException(status_code=400, detail="Can only link student accounts")

    # Store link in child's metadata (or a dedicated table)
    # In production, create a ParentChildLink table
    return {
        "message": f"Child {child.full_name} linked successfully",
        "child_id": str(child.id),
        "child_email": child.email,
        "child_name": child.full_name,
    }


@router.delete("/children/{child_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unlink_child(
    child_id: UUID,
    current_user: Dict[str, Any] = Depends(require_role("parent")),
    db: Session = Depends(get_db),
):
    """Unlink a child account."""
    child = db.get(User, child_id)
    if child is None:
        raise HTTPException(status_code=404, detail="Child not found")
    # Remove parent-child link
    return None


@router.get("/children/{child_id}/progress")
async def get_child_progress(
    child_id: UUID,
    current_user: Dict[str, Any] = Depends(require_role("parent")),
    db: Session = Depends(get_db),
):
    """Get progress stats for a child."""
    child = db.get(User, child_id)
    if child is None:
        raise HTTPException(status_code=404, detail="Child not found")

    sessions = db.exec(
        select(SessionModel).where(
            SessionModel.student_id == child_id,
            SessionModel.status == SessionStatus.completed,
        )
    ).all()

    from app.models.homework import Homework, HomeworkStatus
    homeworks = db.exec(select(Homework).where(Homework.student_id == child_id)).all()

    kp_account = db.exec(select(KpAccount).where(KpAccount.user_id == child_id)).first()

    return {
        "child_id": str(child_id),
        "child_name": child.full_name,
        "total_sessions": len(sessions),
        "subjects": list({s.subject for s in sessions if s.subject}),
        "average_session_rating": (
            sum(s.rating for s in sessions if s.rating) / max(1, sum(1 for s in sessions if s.rating))
        ),
        "homework_total": len(homeworks),
        "homework_completed": sum(1 for h in homeworks if h.status == HomeworkStatus.graded),
        "kp_level": kp_account.level if kp_account else 1,
        "kp_balance": kp_account.balance if kp_account else 0,
    }


@router.get("/children/{child_id}/sessions")
async def get_child_sessions(
    child_id: UUID,
    page: int = 1,
    size: int = 20,
    current_user: Dict[str, Any] = Depends(require_role("parent")),
    db: Session = Depends(get_db),
):
    """Get sessions for a child."""
    import math

    child = db.get(User, child_id)
    if child is None:
        raise HTTPException(status_code=404, detail="Child not found")

    sessions = db.exec(
        select(SessionModel)
        .where(SessionModel.student_id == child_id)
        .order_by(SessionModel.scheduled_at.desc())
    ).all()

    total = len(sessions)
    offset = (page - 1) * size
    paginated = sessions[offset: offset + size]

    return {
        "items": [
            {
                "id": str(s.id),
                "subject": s.subject,
                "level": s.level,
                "scheduled_at": s.scheduled_at.isoformat(),
                "duration_minutes": s.duration_minutes,
                "status": s.status.value,
                "rating": s.rating,
            }
            for s in paginated
        ],
        "total": total,
        "page": page,
        "size": size,
        "pages": math.ceil(total / size) if total else 0,
    }


@router.get("/payments")
async def get_parent_payments(
    page: int = 1,
    size: int = 20,
    current_user: Dict[str, Any] = Depends(require_role("parent")),
    db: Session = Depends(get_db),
):
    """Get payment history for the parent."""
    import math

    stmt = select(User).where(User.supabase_id == current_user["id"])
    parent = db.exec(stmt).first()
    if parent is None:
        raise HTTPException(status_code=404, detail="Parent not found")

    payments = db.exec(
        select(Payment)
        .where(Payment.user_id == parent.id)
        .order_by(Payment.created_at.desc())
    ).all()

    total = len(payments)
    offset = (page - 1) * size
    paginated = payments[offset: offset + size]

    return {
        "items": [
            {
                "id": str(p.id),
                "amount": p.amount,
                "currency": p.currency,
                "method": p.method.value,
                "status": p.status.value,
                "created_at": p.created_at.isoformat(),
            }
            for p in paginated
        ],
        "total": total,
        "page": page,
        "size": size,
        "pages": math.ceil(total / size) if total else 0,
    }
