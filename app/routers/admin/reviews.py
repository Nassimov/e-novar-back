from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlmodel import Session, select

from app.dependencies import get_admin_user, get_db
from app.models.admin import Report
from app.models.profile import Profile, TeacherProfile
from app.models.review import Review

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Admin — Reviews"])

# ---------------------------------------------------------------------------
# Data model notes (read before touching this file):
#
# public.review_status enum: 'visible' | 'hidden' | 'flagged'. The 'flagged'
# value exists in the DB type but nothing in the app ever sets it, and
# Review has NO is_flagged / flag_reason / booking_id columns (the previous
# version of this file referenced all three — they don't exist on the real
# app/models/review.py::Review model and would have raised AttributeError
# the moment these endpoints were actually called).
#
# "Flagged/reported reviews" are, instead, reviews that have at least one
# *open* row in public.reports with target_type='review' and
# target_id=review.id (app/models/admin.py::Report). That table is the real
# moderation-flag mechanism used across the app (reviews, users, teachers,
# messages, challenges all share it).
#
# public.report_status enum: 'open' | 'reviewed' | 'dismissed' | 'actioned'.
#
# TeacherProfile has no `rating` field — the real column is `rating_avg`
# (app/models/profile.py::TeacherProfile), alongside `reviews_count`. Both
# are recomputed here after any action that changes which reviews count as
# 'visible', mirroring the DB trigger recompute_teacher_rating() in
# docs/database-schema.sql (which only averages status='visible' reviews).
# ---------------------------------------------------------------------------


def _moderator_id(admin: Dict[str, Any]) -> Optional[UUID]:
    """Admin sessions are env-var based with no real profiles row — get_admin_user
    returns {"id": None, ...} in that case (see app/dependencies.py). Only
    stamp moderated_by when we actually have a real profile UUID, since the
    column is a FK to profiles.id."""
    raw = admin.get("id")
    if not raw:
        return None
    try:
        return UUID(str(raw))
    except (ValueError, TypeError):
        return None


def _severity(open_report_count: int) -> str:
    """Simple, honest severity proxy: how many distinct open reports a review
    currently has. Not a claim about content — just volume of flags."""
    if open_report_count >= 3:
        return "high"
    if open_report_count == 2:
        return "medium"
    if open_report_count == 1:
        return "low"
    return "none"


def _resolve_open_reports(db: Session, review_id: UUID, new_status: str) -> int:
    reports = db.exec(
        select(Report)
        .where(Report.target_type == "review")
        .where(Report.target_id == review_id)
        .where(Report.status == "open")
    ).all()
    for rep in reports:
        rep.status = new_status
        db.add(rep)
    return len(reports)


def _recompute_teacher_stats(db: Session, teacher_id: UUID) -> None:
    teacher_profile = db.get(TeacherProfile, teacher_id)
    if teacher_profile is None:
        return
    remaining = db.exec(
        select(Review)
        .where(Review.teacher_id == teacher_id)
        .where(Review.status == "visible")
    ).all()
    if remaining:
        teacher_profile.rating_avg = round(sum(r.rating for r in remaining) / len(remaining), 2)
        teacher_profile.reviews_count = len(remaining)
    else:
        teacher_profile.rating_avg = 0.0
        teacher_profile.reviews_count = 0
    db.add(teacher_profile)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ReportItem(BaseModel):
    id: str
    reporter_id: str
    reporter_name: str
    reason: Optional[str]
    status: str
    created_at: str


class ReviewItem(BaseModel):
    id: str
    student_id: str
    teacher_id: str
    session_id: Optional[str]
    rating: int
    comment: Optional[str]
    status: str
    student_name: str
    teacher_name: str
    created_at: str
    moderated_by: Optional[str]
    moderated_at: Optional[str]
    open_report_count: int
    latest_report_reason: Optional[str]
    severity: str


class ReviewDetail(ReviewItem):
    reports: List[ReportItem]


class ReviewListResponse(BaseModel):
    items: List[ReviewItem]
    total: int
    page: int
    size: int
    pages: int


class ModerationResult(BaseModel):
    review_id: str
    status: str
    reports_updated: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_items(db: Session, reviews: List[Review]) -> List[ReviewItem]:
    if not reviews:
        return []
    review_ids = [r.id for r in reviews]
    profile_ids = {r.student_id for r in reviews} | {r.teacher_id for r in reviews}
    profiles = db.exec(select(Profile).where(Profile.id.in_(profile_ids))).all()
    prof_map = {p.id: p for p in profiles}

    reports = db.exec(
        select(Report)
        .where(Report.target_type == "review")
        .where(Report.target_id.in_(review_ids))
    ).all()
    reports_by_review: Dict[UUID, List[Report]] = {}
    for rep in reports:
        reports_by_review.setdefault(rep.target_id, []).append(rep)

    items: List[ReviewItem] = []
    for r in reviews:
        student = prof_map.get(r.student_id)
        teacher = prof_map.get(r.teacher_id)
        review_reports = reports_by_review.get(r.id, [])
        open_reports = [rep for rep in review_reports if rep.status == "open"]
        latest_open = max(open_reports, key=lambda rep: rep.created_at) if open_reports else None
        items.append(ReviewItem(
            id=str(r.id),
            student_id=str(r.student_id),
            teacher_id=str(r.teacher_id),
            session_id=str(r.session_id) if r.session_id else None,
            rating=r.rating,
            comment=r.comment,
            status=r.status,
            student_name=student.full_name or "—" if student else "—",
            teacher_name=teacher.full_name or "—" if teacher else "—",
            created_at=r.created_at.isoformat(),
            moderated_by=str(r.moderated_by) if r.moderated_by else None,
            moderated_at=r.moderated_at.isoformat() if r.moderated_at else None,
            open_report_count=len(open_reports),
            latest_report_reason=latest_open.reason if latest_open else None,
            severity=_severity(len(open_reports)),
        ))
    return items


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/", response_model=ReviewListResponse)
async def list_reviews(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    flagged_only: bool = Query(False),
    status_filter: Optional[str] = Query(None, alias="status"),
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """
    List reviews for moderation, newest first.

    flagged_only=true restricts the list to reviews that currently have at
    least one *open* Report row against them (see module docstring — there
    is no is_flagged column on reviews).
    """
    query = select(Review)
    if status_filter:
        query = query.where(Review.status == status_filter)
    reviews = db.exec(query.order_by(Review.created_at.desc())).all()

    items = _build_items(db, reviews)
    if flagged_only:
        items = [it for it in items if it.open_report_count > 0]

    total = len(items)
    offset = (page - 1) * size
    paginated = items[offset: offset + size]

    return ReviewListResponse(
        items=paginated,
        total=total,
        page=page,
        size=size,
        pages=math.ceil(total / size) if total else 0,
    )


@router.get("/{review_id}", response_model=ReviewDetail)
async def get_review(
    review_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Single review detail including every report (open and resolved) ever
    filed against it."""
    review = db.get(Review, review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found")

    item = _build_items(db, [review])[0]

    reports = db.exec(
        select(Report)
        .where(Report.target_type == "review")
        .where(Report.target_id == review_id)
        .order_by(Report.created_at.desc())
    ).all()
    reporter_ids = {rep.reporter_id for rep in reports}
    reporters = db.exec(select(Profile).where(Profile.id.in_(reporter_ids))).all() if reporter_ids else []
    reporter_map = {p.id: p for p in reporters}

    report_items = [
        ReportItem(
            id=str(rep.id),
            reporter_id=str(rep.reporter_id),
            reporter_name=(reporter_map[rep.reporter_id].full_name or "—") if rep.reporter_id in reporter_map else "—",
            reason=rep.reason,
            status=rep.status,
            created_at=rep.created_at.isoformat(),
        )
        for rep in reports
    ]

    return ReviewDetail(**item.model_dump(), reports=report_items)


@router.post("/{review_id}/keep", response_model=ModerationResult)
async def keep_review(
    review_id: UUID,
    admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """
    Admin reviewed the flag(s) and judged the review legitimate: dismiss
    every open report against it and stamp who/when it was reviewed, but
    leave the review's own status untouched (it stays 'visible' / whatever
    it already was) — this is the "false flag, keep the review" action.
    """
    review = db.get(Review, review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found")

    updated = _resolve_open_reports(db, review_id, "dismissed")
    review.moderated_by = _moderator_id(admin)
    review.moderated_at = datetime.utcnow()
    db.add(review)
    db.commit()
    db.refresh(review)

    logger.info("Review kept: review_id=%s reports_dismissed=%d", review_id, updated)
    return ModerationResult(review_id=str(review_id), status=review.status, reports_updated=updated)


@router.post("/{review_id}/hide", response_model=ModerationResult)
async def hide_review(
    review_id: UUID,
    admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """
    Soft-moderation: hide the review from public view (status -> 'hidden')
    without deleting it, and close out any open reports against it as
    'actioned'. Recomputes the teacher's rating_avg/reviews_count since
    hidden reviews are excluded from that calculation.
    """
    review = db.get(Review, review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found")
    if review.status == "hidden":
        raise HTTPException(status_code=409, detail="Review is already hidden")

    updated = _resolve_open_reports(db, review_id, "actioned")
    review.status = "hidden"
    review.moderated_by = _moderator_id(admin)
    review.moderated_at = datetime.utcnow()
    db.add(review)
    db.commit()

    _recompute_teacher_stats(db, review.teacher_id)
    db.commit()
    db.refresh(review)

    logger.info("Review hidden: review_id=%s reports_actioned=%d", review_id, updated)
    return ModerationResult(review_id=str(review_id), status=review.status, reports_updated=updated)


@router.delete("/{review_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_review(
    review_id: UUID,
    _admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """
    Delete a review outright and recompute the teacher's rating_avg/
    reviews_count from the remaining visible reviews (same intent as the
    previous, broken implementation — this one only touches real columns).
    Any open reports against the review are closed as 'actioned' first,
    since their target is about to disappear.
    """
    review = db.get(Review, review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found")

    teacher_id = review.teacher_id
    reports_updated = _resolve_open_reports(db, review_id, "actioned")
    db.delete(review)
    db.commit()

    _recompute_teacher_stats(db, teacher_id)
    db.commit()

    logger.info("Review deleted: review_id=%s teacher_id=%s reports_actioned=%d", review_id, teacher_id, reports_updated)
    return None


@router.post("/reports/{report_id}/dismiss", response_model=ModerationResult)
async def dismiss_report(
    report_id: UUID,
    admin: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """
    Dismiss a single report as a false flag without touching the review it
    targets — for when a review has several reports and only some of them
    are bogus (the review stays exactly as it was).
    """
    report = db.get(Report, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    if report.target_type != "review":
        raise HTTPException(status_code=400, detail="This report does not target a review")

    report.status = "dismissed"
    db.add(report)

    review = db.get(Review, report.target_id)
    if review is not None:
        review.moderated_by = _moderator_id(admin)
        review.moderated_at = datetime.utcnow()
        db.add(review)

    db.commit()

    return ModerationResult(
        review_id=str(report.target_id),
        status=review.status if review is not None else "unknown",
        reports_updated=1,
    )
