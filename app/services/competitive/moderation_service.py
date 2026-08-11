from __future__ import annotations

"""
Moderation Service — Competitive Arena Phase 16 (Reporting, Sanctions,
Appeals, Audit Logs).

Reuses three pre-existing mechanisms instead of building parallel ones:
  - public.reports (widened, migration 096) — the ALREADY-existing
    platform moderation queue (previously wired only for review reports).
  - public.audit_logs — the already-existing general admin-action audit
    trail (used elsewhere by admin_accounts/session_validation/club).
  - Real per-mechanism enforcement: competitive_statistics.suspended_until
    (Phase 2), competitive_chat_mutes (Phase 8), club_members.status=
    'banned' (Phase 11) — issue_sanction() writes to whichever of these
    already implements the sanction's actual effect, PLUS an arena_
    sanctions row as the unified record an admin/appeal flow reads.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from fastapi import HTTPException, status
from sqlmodel import Session, func, select

from app.models.admin import AuditLog, PlatformSettings, Report
from app.models.moderation import SANCTION_TYPES, ArenaAppeal, ArenaSanction
from app.services.notification_engine import emit

logger = logging.getLogger(__name__)

REPORT_CATEGORIES = [
    "cheating", "abusive_behavior", "harassment", "offensive_username",
    "offensive_club", "inappropriate_message", "inappropriate_profile", "spam", "other",
]


def _settings(db: Session) -> PlatformSettings:
    return db.get(PlatformSettings, True) or PlatformSettings()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _audit(db: Session, *, actor_id: Optional[UUID], action: str, target_type: str, target_id: Optional[UUID], meta: Optional[Dict[str, Any]] = None) -> None:
    try:
        db.add(AuditLog(actor_id=actor_id, action=action, target_type=target_type, target_id=target_id, meta=meta or {}))
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("moderation_service._audit failed action=%s target=%s/%s", action, target_type, target_id)


# ─── Reports ────────────────────────────────────────────────────────────────

def submit_report(
    db: Session, *, reporter_id: UUID, target_type: str, target_id: UUID,
    category: str, reason: Optional[str], evidence: Optional[List[Dict[str, Any]]] = None,
) -> Report:
    if category not in REPORT_CATEGORIES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Catégorie de signalement invalide.")
    report = Report(
        reporter_id=reporter_id, target_type=target_type, target_id=target_id,
        reason=reason, category=category, evidence=evidence or [], status="open",
    )
    db.add(report)
    db.commit()
    db.refresh(report)

    emit(
        db, event_type="arena_report_received", user_id=reporter_id,
        dedup_key=f"arena_report_received:{report.id}",
    )
    _audit(db, actor_id=reporter_id, action="report_submitted", target_type=target_type, target_id=target_id, meta={"report_id": str(report.id), "category": category})

    try:
        from app.core import domain_events
        domain_events.publish(domain_events.REPORT_SUBMITTED, report_id=report.id, target_type=target_type, target_id=target_id, category=category)
    except Exception:
        logger.debug("moderation_service.submit_report: domain event publish failed", exc_info=True)
    return report


def list_reports(
    db: Session, *, report_status: Optional[str] = None, category: Optional[str] = None,
    target_type: Optional[str] = None, page: int = 1, size: int = 50,
) -> Tuple[List[Report], int]:
    query = select(Report).where(Report.target_type != "review")  # review moderation has its own dedicated queue/UI
    if report_status:
        query = query.where(Report.status == report_status)
    if category:
        query = query.where(Report.category == category)
    if target_type:
        query = query.where(Report.target_type == target_type)
    total = db.exec(select(func.count()).select_from(query.subquery())).one()
    items = list(db.exec(query.order_by(Report.created_at.desc()).offset((page - 1) * size).limit(size)).all())
    return items, total


def get_report_or_404(db: Session, report_id: UUID) -> Report:
    report = db.get(Report, report_id)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Signalement introuvable.")
    return report


def review_report(db: Session, report_id: UUID, *, admin_id: UUID, new_status: str, resolution_note: Optional[str] = None) -> Report:
    if new_status not in ("reviewed", "dismissed", "actioned"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Statut invalide.")
    report = get_report_or_404(db, report_id)
    report.status = new_status
    report.reviewed_by = admin_id
    report.reviewed_at = _now()
    report.resolution_note = resolution_note
    db.add(report)
    db.commit()
    db.refresh(report)
    _audit(db, actor_id=admin_id, action=f"report_{new_status}", target_type=report.target_type, target_id=report.target_id, meta={"report_id": str(report.id)})
    return report


def merge_reports(db: Session, report_id: UUID, *, into_report_id: UUID, admin_id: UUID) -> Report:
    if report_id == into_report_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Impossible de fusionner un signalement avec lui-même.")
    report = get_report_or_404(db, report_id)
    get_report_or_404(db, into_report_id)  # 404s if target doesn't exist
    report.status = "merged"
    report.merged_into_id = into_report_id
    report.reviewed_by = admin_id
    report.reviewed_at = _now()
    db.add(report)
    db.commit()
    db.refresh(report)
    _audit(db, actor_id=admin_id, action="report_merged", target_type=report.target_type, target_id=report.target_id, meta={"report_id": str(report.id), "into": str(into_report_id)})
    return report


# ─── Sanctions ──────────────────────────────────────────────────────────────

def _sanction_default_duration(db: Session, sanction_type: str) -> Optional[timedelta]:
    settings_row = _settings(db)
    if sanction_type == "mute_temporary":
        return timedelta(hours=settings_row.moderation_default_mute_hours)
    if sanction_type == "suspension_temporary":
        return timedelta(days=settings_row.moderation_default_suspension_days)
    return None  # permanent types, or types with no time dimension (warning, tournament_ban, club_restriction)


def issue_sanction(
    db: Session, *, user_id: UUID, sanction_type: str, reason: Optional[str], issued_by: UUID,
    evidence: Optional[List[Dict[str, Any]]] = None, report_id: Optional[UUID] = None,
    club_id: Optional[UUID] = None, duration_days: Optional[int] = None,
) -> ArenaSanction:
    if sanction_type not in SANCTION_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Type de sanction invalide.")
    if sanction_type == "club_restriction" and club_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="club_id requis pour une restriction de club.")

    duration = timedelta(days=duration_days) if duration_days else _sanction_default_duration(db, sanction_type)
    ends_at = (_now() + duration) if duration else None
    permanent_ends_at = _now() + timedelta(days=36500)  # ~100 years — keeps suspended_until's existing "future=suspended" contract intact for permanent sanctions, never a magic None

    sanction = ArenaSanction(
        user_id=user_id, sanction_type=sanction_type, reason=reason, evidence=evidence or [],
        issued_by=issued_by, report_id=report_id, club_id=club_id, ends_at=ends_at, status="active",
    )
    db.add(sanction)
    db.commit()
    db.refresh(sanction)

    # Apply real enforcement.
    if sanction_type in ("mute_temporary", "mute_permanent"):
        from app.models.competitive import CompetitiveChatMute
        db.add(CompetitiveChatMute(
            user_id=user_id, muted_by=issued_by, reason=reason,
            muted_until=ends_at if sanction_type == "mute_temporary" else None,
        ))
        db.commit()
    elif sanction_type in ("suspension_temporary", "suspension_permanent", "competitive_ban"):
        from app.services.competitive import statistics_service
        stats = statistics_service.get_or_create_statistics(db, user_id)
        stats.suspended_until = ends_at if sanction_type == "suspension_temporary" else permanent_ends_at
        stats.suspended_reason = reason
        db.add(stats)
        db.commit()
    elif sanction_type == "club_restriction":
        from app.models.club import ClubMember
        member = db.exec(
            select(ClubMember).where(ClubMember.club_id == club_id).where(ClubMember.user_id == user_id)
        ).first()
        if member is not None:
            member.status = "banned"
            member.banned_at = _now()
            member.banned_reason = reason
            member.banned_by = issued_by
            db.add(member)
            db.commit()
    # 'warning' and 'tournament_ban' have no separate enforcement table —
    # this ArenaSanction row IS the enforcement record (tournament
    # registration checks has_active_sanction(db, user_id, 'tournament_ban')).

    if report_id is not None:
        review_report(db, report_id, admin_id=issued_by, new_status="actioned", resolution_note=f"Sanction appliquée : {sanction_type}")

    _audit(db, actor_id=issued_by, action=f"sanction_issued:{sanction_type}", target_type="user", target_id=user_id, meta={"sanction_id": str(sanction.id), "reason": reason})
    emit(
        db, event_type="arena_sanction_issued", user_id=user_id,
        context={"sanction_type": sanction_type}, data={"sanction_id": str(sanction.id)},
        dedup_key=f"arena_sanction_issued:{sanction.id}",
    )
    try:
        from app.core import domain_events
        domain_events.publish(domain_events.SANCTION_ISSUED, user_id=user_id, sanction_type=sanction_type, sanction_id=sanction.id)
    except Exception:
        logger.debug("moderation_service.issue_sanction: domain event publish failed", exc_info=True)
    return sanction


def revoke_sanction(db: Session, sanction_id: UUID, *, admin_id: UUID, note: Optional[str] = None) -> ArenaSanction:
    sanction = db.get(ArenaSanction, sanction_id)
    if sanction is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sanction introuvable.")
    if sanction.status != "active":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cette sanction n'est plus active.")

    sanction.status = "revoked"
    db.add(sanction)
    db.commit()

    if sanction.sanction_type in ("suspension_temporary", "suspension_permanent", "competitive_ban"):
        from app.models.competitive import CompetitiveStatistics
        stats = db.get(CompetitiveStatistics, sanction.user_id)
        if stats is not None:
            stats.suspended_until = None
            stats.suspended_reason = None
            db.add(stats)
            db.commit()
    elif sanction.sanction_type == "club_restriction" and sanction.club_id is not None:
        from app.models.club import ClubMember
        member = db.exec(
            select(ClubMember).where(ClubMember.club_id == sanction.club_id).where(ClubMember.user_id == sanction.user_id)
        ).first()
        if member is not None and member.status == "banned":
            member.status = "active"
            db.add(member)
            db.commit()
    # mute_temporary/mute_permanent: reversed via the existing dedicated
    # POST /admin/competitive/chat/unmute action (Phase 8) — not
    # duplicated here since that endpoint already owns competitive_chat_
    # mutes' own semantics correctly.

    db.refresh(sanction)
    _audit(db, actor_id=admin_id, action="sanction_revoked", target_type="user", target_id=sanction.user_id, meta={"sanction_id": str(sanction.id), "note": note})
    return sanction


def list_sanctions(
    db: Session, *, user_id: Optional[UUID] = None, sanction_status: Optional[str] = None,
    sanction_type: Optional[str] = None, page: int = 1, size: int = 50,
) -> Tuple[List[ArenaSanction], int]:
    query = select(ArenaSanction)
    if user_id:
        query = query.where(ArenaSanction.user_id == user_id)
    if sanction_status:
        query = query.where(ArenaSanction.status == sanction_status)
    if sanction_type:
        query = query.where(ArenaSanction.sanction_type == sanction_type)
    total = db.exec(select(func.count()).select_from(query.subquery())).one()
    items = list(db.exec(query.order_by(ArenaSanction.created_at.desc()).offset((page - 1) * size).limit(size)).all())
    return items, total


def has_active_sanction(db: Session, user_id: UUID, sanction_type: str) -> bool:
    now = _now()
    row = db.exec(
        select(ArenaSanction)
        .where(ArenaSanction.user_id == user_id)
        .where(ArenaSanction.sanction_type == sanction_type)
        .where(ArenaSanction.status == "active")
        .where((ArenaSanction.ends_at.is_(None)) | (ArenaSanction.ends_at > now))
    ).first()
    return row is not None


def get_my_sanctions(db: Session, user_id: UUID) -> List[ArenaSanction]:
    return list(db.exec(select(ArenaSanction).where(ArenaSanction.user_id == user_id).order_by(ArenaSanction.created_at.desc())).all())


# ─── Appeals ────────────────────────────────────────────────────────────────

def submit_appeal(db: Session, *, user_id: UUID, sanction_id: UUID, message: str) -> ArenaAppeal:
    sanction = db.get(ArenaSanction, sanction_id)
    if sanction is None or sanction.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sanction introuvable.")
    if sanction.status != "active":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cette sanction n'est plus active.")
    existing = db.exec(select(ArenaAppeal).where(ArenaAppeal.sanction_id == sanction_id)).first()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Un recours a déjà été déposé pour cette sanction.")

    appeal = ArenaAppeal(sanction_id=sanction_id, user_id=user_id, message=message)
    db.add(appeal)
    db.commit()
    db.refresh(appeal)

    emit(db, event_type="arena_appeal_submitted", user_id=user_id, dedup_key=f"arena_appeal_submitted:{appeal.id}")
    _audit(db, actor_id=user_id, action="appeal_submitted", target_type="sanction", target_id=sanction_id, meta={"appeal_id": str(appeal.id)})
    return appeal


def review_appeal(db: Session, appeal_id: UUID, *, admin_id: UUID, decision: str, resolution_message: Optional[str] = None) -> ArenaAppeal:
    if decision not in ("accepted", "rejected", "more_info_requested"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Décision invalide.")
    appeal = db.get(ArenaAppeal, appeal_id)
    if appeal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recours introuvable.")

    appeal.status = decision
    appeal.reviewed_by = admin_id
    appeal.reviewed_at = _now()
    appeal.resolution_message = resolution_message
    db.add(appeal)
    db.commit()
    db.refresh(appeal)

    if decision == "accepted":
        revoke_sanction(db, appeal.sanction_id, admin_id=admin_id, note="Recours accepté")

    emit(db, event_type="arena_appeal_resolved", user_id=appeal.user_id, context={"decision": decision}, dedup_key=f"arena_appeal_resolved:{appeal.id}")
    _audit(db, actor_id=admin_id, action=f"appeal_{decision}", target_type="sanction", target_id=appeal.sanction_id, meta={"appeal_id": str(appeal.id)})
    return appeal


def list_appeals(db: Session, *, appeal_status: Optional[str] = None, page: int = 1, size: int = 50) -> Tuple[List[ArenaAppeal], int]:
    query = select(ArenaAppeal)
    if appeal_status:
        query = query.where(ArenaAppeal.status == appeal_status)
    total = db.exec(select(func.count()).select_from(query.subquery())).one()
    items = list(db.exec(query.order_by(ArenaAppeal.created_at.desc()).offset((page - 1) * size).limit(size)).all())
    return items, total
