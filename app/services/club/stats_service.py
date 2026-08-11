from __future__ import annotations

"""
Club Statistics Service — Competitive Arena Phase 11, Part C.

Most counters (matches_played/wins/losses/draws/current_streak/best_streak/
total_ep_earned) are plain reads of the denormalized clubs.* columns
maintained by battle_service.finalize_club_battle — mirrors
CompetitiveStatistics' own "raw counters written once at match completion"
convention. Average accuracy / questions answered are aggregated live from
CompetitiveMatchPlayerStats via the club_id tag on
CompetitiveMatchParticipant (Phase 11, Part C's own addition to that table)
— bounded to THIS club's own battles, so a live aggregate stays cheap even
at the "tens of thousands of clubs" scale the spec calls for (each query is
scoped by club_id, using the new idx_competitive_match_participants_club
index).
"""

from typing import Any, Dict
from uuid import UUID

from sqlmodel import Session, func, select

from app.models.club import Club, ClubSeason
from app.models.competitive import CompetitiveMatchParticipant, CompetitiveMatchPlayerStats
from app.services.club.permission_service import get_club_or_404


def get_club_statistics(db: Session, club_id: UUID) -> Dict[str, Any]:
    club = get_club_or_404(db, club_id)

    active_members = db.exec(
        select(func.count()).select_from(
            select(CompetitiveMatchParticipant.user_id).where(CompetitiveMatchParticipant.club_id == club_id).distinct().subquery()
        )
    ).one()

    participant_ids = db.exec(select(CompetitiveMatchParticipant.id).where(CompetitiveMatchParticipant.club_id == club_id)).all()
    total_questions = 0
    accuracy_sum = 0.0
    accuracy_n = 0
    if participant_ids:
        # CompetitiveMatchPlayerStats has no participant_id FK — it's keyed
        # by (match_id, user_id), same shape battle_service reads from —
        # so aggregate via a (match_id, user_id) pair scan instead.
        pairs = db.exec(
            select(CompetitiveMatchParticipant.match_id, CompetitiveMatchParticipant.user_id)
            .where(CompetitiveMatchParticipant.club_id == club_id)
        ).all()
        for match_id, user_id in pairs:
            row = db.exec(
                select(CompetitiveMatchPlayerStats)
                .where(CompetitiveMatchPlayerStats.match_id == match_id)
                .where(CompetitiveMatchPlayerStats.user_id == user_id)
            ).first()
            if row is None:
                continue
            total_questions += (row.correct_count or 0) + (row.wrong_count or 0)
            accuracy_sum += row.accuracy_pct or 0
            accuracy_n += 1

    average_accuracy = round(accuracy_sum / accuracy_n, 2) if accuracy_n else 0.0
    win_rate = round((club.wins / club.matches_played) * 100, 2) if club.matches_played else 0.0

    season_points = 0
    active_season = db.exec(select(ClubSeason).where(ClubSeason.status == "active")).first()
    if active_season is not None:
        from app.models.club import ClubRatingHistory
        deltas = db.exec(
            select(ClubRatingHistory.delta)
            .where(ClubRatingHistory.club_id == club_id)
            .where(ClubRatingHistory.season_id == active_season.id)
        ).all()
        season_points = sum(deltas)

    return {
        "club_id": club.id,
        "members": club.member_count,
        "active_members": active_members or 0,
        "matches_played": club.matches_played,
        "wins": club.wins,
        "losses": club.losses,
        "draws": club.draws,
        "average_accuracy": average_accuracy,
        "questions_answered": total_questions,
        "win_rate": win_rate,
        "current_streak": club.current_streak,
        "best_streak": club.best_streak,
        "season_points": season_points,
        "total_ep_earned": club.total_ep_earned,
        "rating": club.rating,
        "reputation": club.reputation,
        "xp": club.xp,
    }
