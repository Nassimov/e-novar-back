from __future__ import annotations

"""
Player-facing Moderation API — Competitive Arena Phase 16 (Reporting +
Appeals). Mounted under /api/competitive (see app/main.py).
"""

from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.dependencies import get_current_user, get_db
from app.services.competitive import moderation_service

router = APIRouter(tags=["Competitive"])


class ReportCreateRequest(BaseModel):
    target_type: str = Field(..., max_length=50)
    target_id: UUID
    category: str = Field(..., max_length=50)
    reason: Optional[str] = Field(default=None, max_length=1000)
    evidence: List[Dict[str, Any]] = Field(default_factory=list, max_length=10)


@router.post("/reports")
def create_report(
    payload: ReportCreateRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    reporter_id = UUID(current_user["id"])

    from app.core.rate_limiter import check_rate_limit
    from app.models.admin import PlatformSettings
    settings_row = db.get(PlatformSettings, True) or PlatformSettings()
    check_rate_limit(
        key=f"arena_rl:report_submit:{reporter_id}", limit=settings_row.rate_limit_report_submit_per_10s,
        message="Trop de signalements envoyés. Merci de patienter.",
    )

    report = moderation_service.submit_report(
        db, reporter_id=reporter_id, target_type=payload.target_type, target_id=payload.target_id,
        category=payload.category, reason=payload.reason, evidence=payload.evidence,
    )
    return {"id": str(report.id), "status": report.status}


@router.get("/sanctions/me")
def get_my_sanctions(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    sanctions = moderation_service.get_my_sanctions(db, user_id)
    return {"items": [
        {
            "id": str(s.id), "sanction_type": s.sanction_type, "reason": s.reason,
            "starts_at": s.starts_at, "ends_at": s.ends_at, "status": s.status, "created_at": s.created_at,
        }
        for s in sanctions
    ]}


class AppealCreateRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)


@router.post("/sanctions/{sanction_id}/appeal")
def create_appeal(
    sanction_id: UUID,
    payload: AppealCreateRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    appeal = moderation_service.submit_appeal(db, user_id=user_id, sanction_id=sanction_id, message=payload.message)
    return {"id": str(appeal.id), "status": appeal.status}


@router.get("/appeals/me")
def get_my_appeals(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    sanctions = moderation_service.get_my_sanctions(db, user_id)
    from sqlmodel import select
    from app.models.moderation import ArenaAppeal
    sanction_ids = [s.id for s in sanctions]
    if not sanction_ids:
        return {"items": []}
    appeals = list(db.exec(select(ArenaAppeal).where(ArenaAppeal.sanction_id.in_(sanction_ids))).all())
    return {"items": [
        {
            "id": str(a.id), "sanction_id": str(a.sanction_id), "message": a.message, "status": a.status,
            "resolution_message": a.resolution_message, "created_at": a.created_at, "reviewed_at": a.reviewed_at,
        }
        for a in appeals
    ]}
