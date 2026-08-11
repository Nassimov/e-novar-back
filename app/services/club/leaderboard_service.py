from __future__ import annotations

"""
Club Leaderboard Service — Competitive Arena Phase 11, Part C.

Live-computed from clubs.* / club_season_history — no separate materialized
leaderboard table (same choice migration 091's header explains: clubs.rating/
wins are already indexed, and a per-club live query stays cheap even at
scale). Season/All-Time scopes read different sources: All-Time reads the
live `clubs` table directly; Season reads the permanent, frozen
club_season_history archive of a specific (usually ended) season, so a past
season's leaderboard never silently drifts after the season is over.
"""

from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlmodel import Session, select

from app.models.club import Club, ClubSeasonHistory

_SORT_COLUMNS = {"rating": Club.rating, "wins": Club.wins, "reputation": Club.reputation}


def get_all_time_leaderboard(
    db: Session, *, region: Optional[str] = None, sort: str = "rating", page: int = 1, size: int = 20,
) -> Dict[str, Any]:
    column = _SORT_COLUMNS.get(sort, Club.rating)
    query = select(Club).where(Club.status == "active")
    if region:
        query = query.where(Club.region == region)
    if sort == "win_rate":
        clubs = list(db.exec(query).all())
        clubs.sort(key=lambda c: (c.wins / c.matches_played) if c.matches_played else 0, reverse=True)
        total = len(clubs)
        clubs = clubs[(page - 1) * size: (page - 1) * size + size]
    else:
        total = db.exec(select(Club).where(Club.status == "active")).all()
        total = len(total) if not region else len([c for c in total if c.region == region])
        clubs = list(db.exec(query.order_by(column.desc()).offset((page - 1) * size).limit(size)).all())

    items = []
    for idx, c in enumerate(clubs, start=(page - 1) * size + 1):
        win_rate = round((c.wins / c.matches_played) * 100, 2) if c.matches_played else 0.0
        items.append({
            "rank": idx, "club_id": c.id, "name": c.name, "tag": c.tag, "logo_url": c.logo_url,
            "rating": c.rating, "wins": c.wins, "matches_played": c.matches_played, "win_rate": win_rate,
            "reputation": c.reputation, "member_count": c.member_count, "region": c.region,
        })
    return {"items": items, "total": total, "page": page, "size": size, "scope": "all_time"}


def get_season_leaderboard(db: Session, season_id: UUID, *, page: int = 1, size: int = 20) -> Dict[str, Any]:
    total = len(db.exec(select(ClubSeasonHistory).where(ClubSeasonHistory.season_id == season_id)).all())
    rows = list(db.exec(
        select(ClubSeasonHistory).where(ClubSeasonHistory.season_id == season_id)
        .order_by(ClubSeasonHistory.final_rank.asc()).offset((page - 1) * size).limit(size)
    ).all())
    items = [
        {
            "rank": r.final_rank, "club_id": r.club_id, "name": r.club_name, "rating": r.final_rating,
            "wins": r.wins, "losses": r.losses, "draws": r.draws,
        }
        for r in rows
    ]
    return {"items": items, "total": total, "page": page, "size": size, "scope": "season", "season_id": season_id}
