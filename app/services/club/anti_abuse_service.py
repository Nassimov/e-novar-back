from __future__ import annotations

"""
Club Anti-Abuse Analyzer — Competitive Arena Phase 11, Part C.

Detection only, same posture as app/services/competitive/anti_abuse_
service.py (Phase 7) — "no automatic punishment, only detection", every
signal logged via club_service.log_event (AuditLog, target_type='club') so
the admin dashboard (app/routers/admin/club.py's suspicious-clubs endpoint)
has a real trail to act on. Deliberately NOT attempted: device/IP-based
multi-account correlation — no such infrastructure exists anywhere in this
codebase (same caveat Phase 7's own module carries).
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from uuid import UUID

from sqlmodel import Session, select

from app.models.club import Club, ClubMember, ClubRatingHistory
from app.services.club import club_service

logger = logging.getLogger(__name__)

_REPEATED_OPPONENT_WINDOW_MATCHES = 10
_REPEATED_OPPONENT_THRESHOLD = 6
_ABNORMAL_GAIN_WINDOW_HOURS = 24
_ABNORMAL_GAIN_THRESHOLD = 300
_FARMING_MIN_BATTLES = 10
_FARMING_MAX_DISTINCT_MEMBERS_RATIO = 0.3  # fewer than 30% of the roster ever actually plays


def analyze_after_battle(db: Session, *, club_id: UUID, opponent_club_id: Optional[UUID]) -> None:
    """Called once per club (both sides) from battle_service.finalize_club_
    battle, after the club rating engine has already run — a detection
    signal must never block or slow down the battle result itself."""
    try:
        _check_repeated_opponent(db, club_id=club_id, opponent_club_id=opponent_club_id)
        _check_abnormal_gain(db, club_id=club_id)
        _check_farming(db, club_id=club_id)
    except Exception:
        logger.exception("club anti_abuse_service.analyze_after_battle failed for club=%s — swallowed (detection-only)", club_id)


def _check_repeated_opponent(db: Session, *, club_id: UUID, opponent_club_id: Optional[UUID]) -> None:
    if opponent_club_id is None:
        return
    recent = list(db.exec(
        select(ClubRatingHistory)
        .where(ClubRatingHistory.club_id == club_id)
        .order_by(ClubRatingHistory.created_at.desc())
        .limit(_REPEATED_OPPONENT_WINDOW_MATCHES)
    ).all())
    same_opponent_rows = [r for r in recent if r.opponent_club_id == opponent_club_id]
    if len(same_opponent_rows) < _REPEATED_OPPONENT_THRESHOLD:
        return

    club_service.log_event(
        db, club_id=club_id, actor_id=None, event_type="anti_abuse_repeated_opponent_signal",
        meta={"opponent_club_id": str(opponent_club_id), "count": len(same_opponent_rows), "window": _REPEATED_OPPONENT_WINDOW_MATCHES},
    )

    # Win-trading heuristic — same "near-perfect alternation" pattern Phase
    # 7 already uses for players, applied to club vs. club results.
    results = [r.result for r in reversed(same_opponent_rows)]
    if len(results) >= 4:
        alternations = sum(1 for i in range(1, len(results)) if results[i] != results[i - 1])
        if alternations >= len(results) - 1:
            club_service.log_event(
                db, club_id=club_id, actor_id=None, event_type="anti_abuse_win_trading_signal",
                meta={"opponent_club_id": str(opponent_club_id), "pattern": results},
            )


def _check_abnormal_gain(db: Session, *, club_id: UUID) -> None:
    since = datetime.now(timezone.utc) - timedelta(hours=_ABNORMAL_GAIN_WINDOW_HOURS)
    rows = list(db.exec(
        select(ClubRatingHistory).where(ClubRatingHistory.club_id == club_id).where(ClubRatingHistory.created_at >= since)
    ).all())
    net_gain = sum(r.delta for r in rows)
    if net_gain >= _ABNORMAL_GAIN_THRESHOLD:
        club_service.log_event(
            db, club_id=club_id, actor_id=None, event_type="anti_abuse_abnormal_gain_signal",
            meta={"net_gain": net_gain, "window_hours": _ABNORMAL_GAIN_WINDOW_HOURS, "matches": len(rows)},
        )


def _check_farming(db: Session, *, club_id: UUID) -> None:
    """A 'fake club' farming rating with a skeleton crew: many battles played
    but only a tiny fraction of the roster ever actually participates
    (real member growth is a side-effect of a healthy club, not its
    substitute)."""
    club = db.get(Club, club_id)
    if club is None or club.matches_played < _FARMING_MIN_BATTLES or club.member_count < 3:
        return
    from app.models.competitive import CompetitiveMatchParticipant
    distinct_participants = db.exec(
        select(CompetitiveMatchParticipant.user_id).where(CompetitiveMatchParticipant.club_id == club_id).distinct()
    ).all()
    ratio = len(distinct_participants) / club.member_count if club.member_count else 0
    if ratio < _FARMING_MAX_DISTINCT_MEMBERS_RATIO:
        club_service.log_event(
            db, club_id=club_id, actor_id=None, event_type="anti_abuse_farming_signal",
            meta={"distinct_participants": len(distinct_participants), "member_count": club.member_count, "ratio": round(ratio, 2)},
        )


def list_suspicious_clubs(db: Session, *, limit: int = 100) -> List[Dict]:
    """Reads the AuditLog trail every check above writes to — mirrors
    app/routers/admin/competitive.py's suspicious-accounts endpoint shape."""
    from app.models.admin import AuditLog
    rows = db.exec(
        select(AuditLog)
        .where(AuditLog.target_type == "club")
        .where(AuditLog.action.like("anti_abuse_%"))
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    ).all()
    club_ids = list({r.target_id for r in rows if r.target_id})
    clubs = {c.id: c for c in db.exec(select(Club).where(Club.id.in_(club_ids))).all()} if club_ids else {}
    out = []
    for r in rows:
        club = clubs.get(r.target_id)
        out.append({
            "id": r.id, "club_id": r.target_id, "club_name": club.name if club else None,
            "signal_type": r.action, "meta": r.meta, "created_at": r.created_at,
        })
    return out
