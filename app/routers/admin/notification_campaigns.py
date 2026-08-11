from __future__ import annotations

"""
Admin — notification/marketing campaigns.

Distinct from app/routers/admin/promos.py (discount CODE management) — this
is broadcast messaging (push/email/in-app) with flexible audience targeting,
one-off or scheduled/recurring. See app/models/notification.py's
NotificationCampaign/NotificationCampaignTarget and
app/workers/campaign_tasks.py for the send pipeline.

Targeting reuses the same Notification/NotificationQueue tables and the
existing async delivery worker (task_process_notification_queue) — a
campaign send is just N calls to the same persist+queue logic
notification_engine.emit() uses, without needing a notification_templates
row per campaign.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.dependencies import get_admin_user, get_db
from app.models.notification import (
    NotificationCampaign,
    NotificationCampaignTarget,
    NotificationDeliveryLog,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Admin — Notification Campaigns"])

_ALLOWED_TARGET_TYPES = {"all", "students", "teachers", "parents", "admins", "custom"}
_ALLOWED_CHANNELS = {"in_app", "push", "email"}
_RATE_WINDOW = 3600
_RATE_MAX = 20  # campaigns sent/scheduled per admin per hour — spam guard


# ─── I/O schemas ───────────────────────────────────────────────────────────────

class CampaignIn(BaseModel):
    name: str
    description: Optional[str] = None
    category: str = "marketing"
    priority: str = "marketing"
    channels: List[str] = Field(default_factory=lambda: ["push", "email"])
    title: str
    body: str
    deep_link: Optional[str] = None
    target_type: str = "all"
    target_filters: Dict[str, Any] = Field(default_factory=dict)
    scheduled_at: Optional[datetime] = None
    recurring_rule: Optional[Dict[str, Any]] = None

    def validate_shape(self) -> None:
        if self.target_type not in _ALLOWED_TARGET_TYPES:
            raise HTTPException(status_code=422, detail=f"target_type must be one of {sorted(_ALLOWED_TARGET_TYPES)}")
        bad_channels = set(self.channels) - _ALLOWED_CHANNELS
        if bad_channels:
            raise HTTPException(status_code=422, detail=f"Unknown channels: {sorted(bad_channels)}")
        if not self.title.strip() or not self.body.strip():
            raise HTTPException(status_code=422, detail="title and body are required")


class CampaignUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    title: Optional[str] = None
    body: Optional[str] = None
    deep_link: Optional[str] = None
    channels: Optional[List[str]] = None
    target_type: Optional[str] = None
    target_filters: Optional[Dict[str, Any]] = None
    scheduled_at: Optional[datetime] = None
    recurring_rule: Optional[Dict[str, Any]] = None


def _rate_limit_campaign_send(admin_email: str) -> None:
    from app.core.rate_limiter import check_rate_limit
    check_rate_limit(
        key=f"campaign:rate:{admin_email}", limit=_RATE_MAX, window_seconds=_RATE_WINDOW,
        message="Trop de campagnes envoyées cette heure. Réessayez plus tard.",
    )


# ─── Targeting resolution ──────────────────────────────────────────────────────

def resolve_campaign_targets(db: Session, target_type: str, filters: Dict[str, Any]) -> List[UUID]:
    """
    Resolves a campaign's audience to a concrete list of profile UUIDs.
    `filters` (all optional):
      role: [str]                        — only for target_type == "custom"
      wilaya: [str]
      language: [str]
      level_main: [str]                  — student_profiles.level_main
      subjects: [str]                    — subject slugs (students: subjects_interested
                                            overlap; teachers: via teacher_subject_prices)
      min_ep / max_ep: int                — kp_balances.balance range
      registered_after / registered_before: ISO date — profiles.created_at
      inactive_days: int                 — profiles.last_seen_at older than N days
      leaderboard_top_n: int              — top N by kp_balances.balance (applied last)
      user_ids: [str]                     — explicit hand-picked list (bypasses all other filters)
    """
    from sqlalchemy import text

    if filters.get("user_ids"):
        return [UUID(u) for u in filters["user_ids"]]

    roles: List[str]
    if target_type == "all":
        roles = ["student", "teacher", "parent"]
    elif target_type == "custom":
        roles = filters.get("role") or ["student", "teacher", "parent", "admin"]
    else:
        roles = [target_type[:-1]] if target_type.endswith("s") else [target_type]

    where = ["ur.role = ANY(:roles)"]
    params: Dict[str, Any] = {"roles": roles}

    if filters.get("wilaya"):
        where.append("p.wilaya = ANY(:wilaya)")
        params["wilaya"] = filters["wilaya"]
    if filters.get("language"):
        where.append("p.language = ANY(:language)")
        params["language"] = filters["language"]
    if filters.get("registered_after"):
        where.append("p.created_at >= :registered_after")
        params["registered_after"] = filters["registered_after"]
    if filters.get("registered_before"):
        where.append("p.created_at <= :registered_before")
        params["registered_before"] = filters["registered_before"]
    if filters.get("inactive_days"):
        where.append("(p.last_seen_at IS NULL OR p.last_seen_at < now() - (:inactive_days * interval '1 day'))")
        params["inactive_days"] = filters["inactive_days"]
    if filters.get("level_main"):
        where.append("p.id IN (SELECT user_id FROM student_profiles WHERE level_main = ANY(:level_main))")
        params["level_main"] = filters["level_main"]
    if filters.get("subjects"):
        where.append(
            "(p.id IN (SELECT user_id FROM student_profiles WHERE subjects_interested && :subjects) "
            "OR p.id IN (SELECT tsp.teacher_id FROM teacher_subject_prices tsp "
            "JOIN subjects s ON s.id = tsp.subject_id WHERE s.slug = ANY(:subjects)))"
        )
        params["subjects"] = filters["subjects"]
    if filters.get("min_ep") is not None:
        where.append("p.id IN (SELECT user_id FROM kp_balances WHERE balance >= :min_ep)")
        params["min_ep"] = filters["min_ep"]
    if filters.get("max_ep") is not None:
        where.append("p.id IN (SELECT user_id FROM kp_balances WHERE balance <= :max_ep)")
        params["max_ep"] = filters["max_ep"]

    sql = f"""
        SELECT DISTINCT p.id FROM profiles p
        JOIN user_roles ur ON ur.user_id = p.id
        WHERE {' AND '.join(where)}
    """
    if filters.get("leaderboard_top_n"):
        sql = f"""
            SELECT p.id FROM ({sql}) p
            JOIN kp_balances kb ON kb.user_id = p.id
            ORDER BY kb.balance DESC
            LIMIT :top_n
        """
        params["top_n"] = filters["leaderboard_top_n"]

    rows = db.execute(text(sql), params).all()
    return [r[0] for r in rows]


# ─── CRUD ───────────────────────────────────────────────────────────────────────

@router.get("/")
async def list_campaigns(
    status_filter: Optional[str] = Query(None, alias="status"),
    admin=Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    q = select(NotificationCampaign).order_by(NotificationCampaign.created_at.desc())
    if status_filter:
        q = q.where(NotificationCampaign.status == status_filter)
    campaigns = db.exec(q).all()
    return {"items": campaigns}


@router.post("/", status_code=201)
async def create_campaign(
    payload: CampaignIn,
    admin=Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    payload.validate_shape()
    campaign = NotificationCampaign(
        name=payload.name, description=payload.description,
        category=payload.category, priority=payload.priority, channels=payload.channels,
        title=payload.title, body=payload.body, deep_link=payload.deep_link,
        target_type=payload.target_type, target_filters=payload.target_filters,
        status="scheduled" if payload.scheduled_at else "draft",
        scheduled_at=payload.scheduled_at, recurring_rule=payload.recurring_rule,
        created_by_label=admin.get("email"),
    )
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    return campaign


@router.get("/{campaign_id}")
async def get_campaign(campaign_id: UUID, admin=Depends(get_admin_user), db: Session = Depends(get_db)):
    campaign = db.get(NotificationCampaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


@router.patch("/{campaign_id}")
async def update_campaign(
    campaign_id: UUID,
    payload: CampaignUpdate,
    admin=Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    campaign = db.get(NotificationCampaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.status not in ("draft", "scheduled"):
        raise HTTPException(status_code=409, detail=f"Cannot edit a campaign with status '{campaign.status}'")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(campaign, field, value)
    if payload.scheduled_at is not None:
        campaign.status = "scheduled"
    campaign.updated_at = datetime.now(timezone.utc)
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    return campaign


@router.delete("/{campaign_id}", status_code=204)
async def cancel_campaign(campaign_id: UUID, admin=Depends(get_admin_user), db: Session = Depends(get_db)):
    campaign = db.get(NotificationCampaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.status in ("sending", "sent"):
        raise HTTPException(status_code=409, detail="Cannot cancel a campaign that already started sending")
    campaign.status = "cancelled"
    db.add(campaign)
    db.commit()


@router.post("/{campaign_id}/preview")
async def preview_campaign(campaign_id: UUID, admin=Depends(get_admin_user), db: Session = Depends(get_db)):
    """Resolves the audience without sending anything — recipient count for the admin UI."""
    campaign = db.get(NotificationCampaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    user_ids = resolve_campaign_targets(db, campaign.target_type, campaign.target_filters or {})
    return {"recipients_count": len(user_ids)}


@router.post("/{campaign_id}/send-now", status_code=202)
async def send_campaign_now(campaign_id: UUID, admin=Depends(get_admin_user), db: Session = Depends(get_db)):
    campaign = db.get(NotificationCampaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.status not in ("draft", "scheduled"):
        raise HTTPException(status_code=409, detail=f"Cannot send a campaign with status '{campaign.status}'")

    _rate_limit_campaign_send(admin.get("email") or "unknown")

    campaign.status = "sending"
    campaign.scheduled_at = campaign.scheduled_at or datetime.now(timezone.utc)
    db.add(campaign)
    db.commit()

    from app.workers.campaign_tasks import task_send_campaign
    task_send_campaign.delay(str(campaign.id))
    return {"status": "sending", "campaign_id": str(campaign.id)}


@router.get("/{campaign_id}/logs")
async def get_campaign_logs(
    campaign_id: UUID,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    admin=Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    campaign = db.get(NotificationCampaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")

    targets = db.exec(
        select(NotificationCampaignTarget)
        .where(NotificationCampaignTarget.campaign_id == campaign_id)
        .order_by(NotificationCampaignTarget.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
    ).all()
    delivery = db.exec(
        select(NotificationDeliveryLog).where(NotificationDeliveryLog.campaign_id == campaign_id)
    ).all()
    channel_stats: Dict[str, Dict[str, int]] = {}
    for log in delivery:
        entry = channel_stats.setdefault(log.channel, {"success": 0, "failed": 0, "skipped": 0})
        entry[log.status] = entry.get(log.status, 0) + 1

    return {
        "campaign": campaign,
        "targets": targets,
        "channel_stats": channel_stats,
    }
