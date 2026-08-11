from __future__ import annotations

"""
Club Achievement Engine — Competitive Arena Phase 11, Part B.

Event-driven, not polled — check_and_grant() is called right after any event
that could move one of the counters an achievement watches (member join,
battle finalize, reputation award, season end), mirroring every prior
phase's "hook, don't poll" convention. Each achievement is granted at most
once per club (club_achievement_grants' UNIQUE(club_id, achievement_id)) —
re-checking an already-granted achievement is a cheap no-op, not an error.

Granting an achievement has three side effects, always together: a
ClubAchievementGrant row, a ClubTrophy ('special_event' by default — season/
tournament/club_battle achievements are trophied under their own
trophy_type by the caller that awards THOSE trophies directly, e.g.
season_service for season_champion), reputation (reputation_service.
award_achievement_reputation), a feed entry, and a notification.
"""

import logging
from typing import List, Optional
from uuid import UUID

from sqlmodel import Session, select

from app.models.club import Club, ClubAchievement, ClubAchievementGrant, ClubMember, ClubTrophy
from app.services.club import feed_service, reputation_service
from app.services.notification_engine import emit

logger = logging.getLogger(__name__)


def list_achievements(db: Session, *, active_only: bool = True) -> List[ClubAchievement]:
    query = select(ClubAchievement)
    if active_only:
        query = query.where(ClubAchievement.is_active == True)  # noqa: E712
    return list(db.exec(query.order_by(ClubAchievement.criteria_value.asc())).all())


def list_grants(db: Session, club_id: UUID) -> List[dict]:
    rows = list(db.exec(select(ClubAchievementGrant).where(ClubAchievementGrant.club_id == club_id)).all())
    achievements = {a.id: a for a in db.exec(select(ClubAchievement)).all()}
    out = []
    for r in rows:
        a = achievements.get(r.achievement_id)
        if a is None:
            continue
        out.append({
            "id": r.id, "achievement_id": a.id, "code": a.code, "name": a.name,
            "description": a.description, "icon": a.icon, "granted_at": r.granted_at,
        })
    return out


def _already_granted(db: Session, club_id: UUID, achievement_id: UUID) -> bool:
    return db.exec(
        select(ClubAchievementGrant)
        .where(ClubAchievementGrant.club_id == club_id)
        .where(ClubAchievementGrant.achievement_id == achievement_id)
    ).first() is not None


def _meets_criteria(club: Club, achievement: ClubAchievement, *, extra_flag: Optional[str] = None) -> bool:
    ct = achievement.criteria_type
    if ct == "wins_total":
        return club.wins >= achievement.criteria_value
    if ct == "member_count":
        return club.member_count >= achievement.criteria_value
    if ct == "rating_threshold":
        return club.rating >= achievement.criteria_value
    if ct == "reputation_threshold":
        return club.reputation >= achievement.criteria_value
    if ct == "perfect_match":
        return extra_flag == "perfect_match"
    if ct == "season_champion":
        return extra_flag == "season_champion"
    if ct == "tournament_champion":
        return extra_flag == "tournament_champion"
    return False


def grant_achievement(db: Session, club: Club, achievement: ClubAchievement) -> Optional[ClubAchievementGrant]:
    if _already_granted(db, club.id, achievement.id):
        return None

    grant = ClubAchievementGrant(club_id=club.id, achievement_id=achievement.id)
    db.add(grant)
    db.add(ClubTrophy(
        club_id=club.id, trophy_type="special_event", title=achievement.name,
        description=achievement.description, icon=achievement.icon,
    ))
    db.commit()
    db.refresh(grant)

    reputation_service.award_achievement_reputation(db, club)
    feed_service.record(db, club_id=club.id, event_type="club_achievement_unlocked", data={"achievement_code": achievement.code, "achievement_name": achievement.name})

    members = db.exec(select(ClubMember).where(ClubMember.club_id == club.id).where(ClubMember.status == "active")).all()
    for m in members:
        emit(
            db, event_type="club_achievement_unlocked", user_id=m.user_id,
            context={"club_name": club.name, "achievement_name": achievement.name},
            data={"club_id": str(club.id), "achievement_code": achievement.code},
            dedup_key=f"club_achievement_unlocked:{club.id}:{achievement.id}:{m.user_id}",
        )
    return grant


def check_and_grant(db: Session, club: Club, *, extra_flag: Optional[str] = None) -> List[ClubAchievementGrant]:
    """extra_flag lets a caller signal a one-shot event-based criterion
    ('perfect_match'/'season_champion'/'tournament_champion') that isn't
    derivable from the club row's current counters alone."""
    granted: List[ClubAchievementGrant] = []
    for achievement in list_achievements(db, active_only=True):
        if _already_granted(db, club.id, achievement.id):
            continue
        if _meets_criteria(club, achievement, extra_flag=extra_flag):
            grant = grant_achievement(db, club, achievement)
            if grant is not None:
                granted.append(grant)
    return granted
