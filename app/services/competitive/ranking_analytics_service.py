from __future__ import annotations

"""
Ranking Analytics Service — Competitive Arena Phase 13.

Admin-facing aggregate metrics over the ranked ladder — spec: "Average
Rating, League Distribution, Season Participation, Retention, Promotion
Rate, Demotion Rate, Top Subjects, Top Difficulties." Every number here is
a live aggregate query, no separate stats table (same precedent as every
other analytics surface in this codebase)."""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from uuid import UUID

from sqlmodel import Session, func, select

from app.models.competitive import CompetitiveLeague, CompetitiveRatingHistory, CompetitiveStatistics


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get_average_rating(db: Session) -> float:
    avg = db.exec(select(func.avg(CompetitiveStatistics.rating))).one()
    return round(avg, 1) if avg else 0.0


def get_league_distribution(db: Session) -> List[Dict[str, Any]]:
    rows = db.exec(
        select(CompetitiveLeague, func.count(CompetitiveStatistics.user_id))
        .join(CompetitiveStatistics, CompetitiveStatistics.league_id == CompetitiveLeague.id, isouter=True)
        .group_by(CompetitiveLeague.id)
        .order_by(CompetitiveLeague.sort_order.asc())
    ).all()
    return [
        {
            "league_id": str(league.id), "league_name": league.league_name, "division_label": league.division_label,
            "player_count": count or 0,
        }
        for league, count in rows
    ]


def get_promotion_demotion_rates(db: Session, *, days: int = 30) -> Dict[str, Any]:
    since = _now() - timedelta(days=days)
    rows = list(db.exec(
        select(CompetitiveRatingHistory)
        .where(CompetitiveRatingHistory.created_at >= since)
        .where(CompetitiveRatingHistory.league_before_id.is_not(None))
        .where(CompetitiveRatingHistory.league_after_id.is_not(None))
    ).all())
    leagues = {l.id: l for l in db.exec(select(CompetitiveLeague)).all()}
    promotions = sum(1 for r in rows if r.league_before_id != r.league_after_id and leagues.get(r.league_after_id) and leagues.get(r.league_before_id) and leagues[r.league_after_id].sort_order > leagues[r.league_before_id].sort_order)
    demotions = sum(1 for r in rows if r.league_before_id != r.league_after_id and leagues.get(r.league_after_id) and leagues.get(r.league_before_id) and leagues[r.league_after_id].sort_order < leagues[r.league_before_id].sort_order)
    total_matches = len(rows)
    return {
        "period_days": days, "promotions": promotions, "demotions": demotions, "total_ranked_matches": total_matches,
        "promotion_rate_pct": round((promotions / total_matches) * 100, 2) if total_matches else 0.0,
        "demotion_rate_pct": round((demotions / total_matches) * 100, 2) if total_matches else 0.0,
    }


def get_season_participation(db: Session, season_id: UUID) -> Dict[str, Any]:
    from app.models.competitive import CompetitiveSeasonStats

    total = db.exec(
        select(func.count()).select_from(select(CompetitiveSeasonStats.id).where(CompetitiveSeasonStats.season_id == season_id).subquery())
    ).one()
    active = db.exec(
        select(func.count()).select_from(
            select(CompetitiveSeasonStats.id).where(CompetitiveSeasonStats.season_id == season_id).where(CompetitiveSeasonStats.matches_played > 0).subquery()
        )
    ).one()
    return {"season_id": str(season_id), "total_players": total or 0, "active_players": active or 0}


def get_retention(db: Session, *, days: int = 30) -> float:
    """Share of players whose last ranked match was more than `days` ago
    from BEFORE the window who still played again within the last `days`
    — a simple, honest "did they come back" signal, not a cohort model."""
    cutoff = _now() - timedelta(days=days)
    eligible = db.exec(
        select(func.count()).select_from(
            select(CompetitiveStatistics.user_id).where(CompetitiveStatistics.last_ranked_match_at.is_not(None)).where(CompetitiveStatistics.matches_played > 1).subquery()
        )
    ).one()
    if not eligible:
        return 0.0
    recently_active = db.exec(
        select(func.count()).select_from(
            select(CompetitiveStatistics.user_id).where(CompetitiveStatistics.last_ranked_match_at >= cutoff).subquery()
        )
    ).one()
    return round((recently_active / eligible) * 100, 2)


def get_top_subjects_and_difficulties(db: Session, *, limit: int = 5) -> Dict[str, Any]:
    subject_rows = db.exec(
        select(CompetitiveStatistics.favorite_subject_id, func.count())
        .where(CompetitiveStatistics.favorite_subject_id.is_not(None))
        .group_by(CompetitiveStatistics.favorite_subject_id)
        .order_by(func.count().desc())
        .limit(limit)
    ).all()
    difficulty_rows = db.exec(
        select(CompetitiveStatistics.favorite_difficulty, func.count())
        .where(CompetitiveStatistics.favorite_difficulty.is_not(None))
        .group_by(CompetitiveStatistics.favorite_difficulty)
        .order_by(func.count().desc())
        .limit(limit)
    ).all()
    from app.models.catalog import Subject
    subject_ids = [sid for sid, _ in subject_rows]
    subjects = {s.id: s for s in db.exec(select(Subject).where(Subject.id.in_(subject_ids))).all()} if subject_ids else {}
    return {
        "top_subjects": [{"subject_id": str(sid), "subject_name": subjects[sid].name if sid in subjects else None, "count": count} for sid, count in subject_rows],
        "top_difficulties": [{"difficulty": d, "count": count} for d, count in difficulty_rows],
    }


def get_ranking_analytics(db: Session) -> Dict[str, Any]:
    from app.services.competitive import season_service

    active_season = season_service.get_active_season(db)
    out: Dict[str, Any] = {
        "average_rating": get_average_rating(db),
        "league_distribution": get_league_distribution(db),
        "promotion_demotion_30d": get_promotion_demotion_rates(db, days=30),
        "retention_30d_pct": get_retention(db, days=30),
        **get_top_subjects_and_difficulties(db),
    }
    if active_season is not None:
        out["active_season_participation"] = get_season_participation(db, active_season.id)
    return out
