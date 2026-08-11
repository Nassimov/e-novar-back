from __future__ import annotations

"""
Hall of Fame Service — Competitive Arena Phase 13.

Permanent, read-only aggregation over data that already exists and is
already never purged (CompetitiveStatistics.highest_rating, ClubSeasonHistory,
CompetitiveSeasonStats.final_rank, CompetitiveTournament's completed rounds)
— spec: "Permanent page... Everything permanent." No new table: computed
live from already-indexed columns, same "no separate stats table" precedent
used throughout this codebase (e.g. migration 091's header for club
leaderboards). At "hundreds of thousands of players" scale this stays cheap
because every query here is either a small top-N `ORDER BY ... LIMIT` on an
indexed column, or scoped to a handful of ended seasons/completed
tournaments — never a full-table scan.
"""

from typing import Any, Dict, List
from uuid import UUID

from sqlmodel import Session, func, select

from app.models.competitive import CompetitiveStatistics
from app.models.profile import Profile


def get_top_players(db: Session, *, limit: int = 10) -> List[Dict[str, Any]]:
    rows = db.exec(
        select(CompetitiveStatistics, Profile)
        .join(Profile, Profile.id == CompetitiveStatistics.user_id)
        .order_by(CompetitiveStatistics.highest_rating.desc())
        .limit(limit)
    ).all()
    return [
        {
            "user_id": str(s.user_id), "full_name": p.full_name, "avatar_url": p.avatar_url,
            "highest_rating": s.highest_rating, "best_rank_tier": s.best_rank_tier, "current_rating": s.rating,
        }
        for s, p in rows
    ]


def get_legend_players(db: Session, *, limit: int = 50) -> List[Dict[str, Any]]:
    rows = db.exec(
        select(CompetitiveStatistics, Profile)
        .join(Profile, Profile.id == CompetitiveStatistics.user_id)
        .where(CompetitiveStatistics.rank_tier == "legend")
        .order_by(CompetitiveStatistics.rating.desc())
        .limit(limit)
    ).all()
    return [{"user_id": str(s.user_id), "full_name": p.full_name, "avatar_url": p.avatar_url, "rating": s.rating} for s, p in rows]


def get_top_clubs(db: Session, *, limit: int = 10) -> List[Dict[str, Any]]:
    from app.services.club import leaderboard_service as club_leaderboard_service
    res = club_leaderboard_service.get_all_time_leaderboard(db, sort="rating", page=1, size=limit)
    return res["items"]


def get_season_champions(db: Session, *, limit: int = 20) -> List[Dict[str, Any]]:
    from app.models.competitive import CompetitiveSeason, CompetitiveSeasonStats
    seasons = list(db.exec(select(CompetitiveSeason).where(CompetitiveSeason.status == "ended").order_by(CompetitiveSeason.ends_at.desc()).limit(limit)).all())
    out = []
    for season in seasons:
        champion = db.exec(
            select(CompetitiveSeasonStats, Profile)
            .join(Profile, Profile.id == CompetitiveSeasonStats.user_id)
            .where(CompetitiveSeasonStats.season_id == season.id)
            .where(CompetitiveSeasonStats.final_rank == 1)
        ).first()
        if champion is None:
            continue
        stats, profile = champion
        out.append({"season_id": str(season.id), "season_name": season.name, "user_id": str(stats.user_id), "full_name": profile.full_name, "mmr": stats.mmr})
    return out


def get_club_season_champions(db: Session, *, limit: int = 20) -> List[Dict[str, Any]]:
    from app.models.club import ClubSeason, ClubSeasonHistory
    seasons = list(db.exec(select(ClubSeason).where(ClubSeason.status == "ended").order_by(ClubSeason.ends_at.desc()).limit(limit)).all())
    out = []
    for season in seasons:
        champion = db.exec(
            select(ClubSeasonHistory).where(ClubSeasonHistory.season_id == season.id).where(ClubSeasonHistory.final_rank == 1)
        ).first()
        if champion is None:
            continue
        out.append({"season_id": str(season.id), "season_name": season.name, "club_id": str(champion.club_id), "club_name": champion.club_name, "rating": champion.final_rating})
    return out


def get_tournament_champions(db: Session, *, limit: int = 20) -> List[Dict[str, Any]]:
    from app.models.competitive import CompetitiveTournament, CompetitiveTournamentMatch, CompetitiveTournamentRound

    tournaments = list(db.exec(
        select(CompetitiveTournament).where(CompetitiveTournament.status == "completed").order_by(CompetitiveTournament.starts_at.desc()).limit(limit)
    ).all())
    out = []
    for t in tournaments:
        final_round = db.exec(
            select(CompetitiveTournamentRound)
            .where(CompetitiveTournamentRound.tournament_id == t.id)
            .order_by(CompetitiveTournamentRound.round_number.desc())
        ).first()
        if final_round is None:
            continue
        final_match = db.exec(
            select(CompetitiveTournamentMatch).where(CompetitiveTournamentMatch.round_id == final_round.id)
        ).first()
        if final_match is None or final_match.winner_id is None:
            continue
        profile = db.get(Profile, final_match.winner_id)
        out.append({"tournament_id": str(t.id), "tournament_name": t.name, "user_id": str(final_match.winner_id), "full_name": profile.full_name if profile else None})
    return out


def get_historic_records(db: Session) -> Dict[str, Any]:
    highest_rating_ever = db.exec(select(func.max(CompetitiveStatistics.highest_rating))).one()
    longest_streak_ever = db.exec(select(func.max(CompetitiveStatistics.longest_streak))).one()
    most_matches_played = db.exec(
        select(CompetitiveStatistics, Profile).join(Profile, Profile.id == CompetitiveStatistics.user_id)
        .order_by(CompetitiveStatistics.matches_played.desc()).limit(1)
    ).first()
    return {
        "highest_rating_ever": highest_rating_ever or 0,
        "longest_win_streak_ever": longest_streak_ever or 0,
        "most_matches_played": {
            "user_id": str(most_matches_played[0].user_id), "full_name": most_matches_played[1].full_name,
            "matches_played": most_matches_played[0].matches_played,
        } if most_matches_played else None,
    }


def get_hall_of_fame(db: Session) -> Dict[str, Any]:
    return {
        "top_players": get_top_players(db),
        "legend_players": get_legend_players(db),
        "top_clubs": get_top_clubs(db),
        "season_champions": get_season_champions(db),
        "club_season_champions": get_club_season_champions(db),
        "tournament_champions": get_tournament_champions(db),
        "historic_records": get_historic_records(db),
    }
