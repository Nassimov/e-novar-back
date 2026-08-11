from __future__ import annotations

"""
Leaderboard Engine — Competitive Arena Phase 7.

Backend-only ranking (spec: "Ranking must update automatically after every
finished match. Never compute rankings on frontend."). Every board is
computed live from SQL — always correct — with a Redis ZSET used as an
incremental cache ("avoid recalculating the entire leaderboard after every
match — only update impacted players") for the two hottest, unfiltered
boards (global all-time, global current-season): touch_user() is an O(1)
ZADD called once per participant per finished match from gameplay_service,
never a full rebuild. Every other combination (filtered, or
daily/weekly/monthly) reads SQL directly — small enough tables that a live
query is fast, and correctness matters far more than micro-caching a
rarely-hit combination. A Redis outage silently falls back to SQL (same
fail-open posture as presence_service.py).

"Subject"/"Difficulty" filters use CompetitiveStatistics.favorite_subject_id
/favorite_difficulty (a player's dominant subject/difficulty) rather than a
separate per-subject rating: Arena Rating has been a single global skill
score since Phase 1 (matchmaking's subject/level/difficulty are QUEUE
filters, not separate rating pools) — inventing a parallel per-subject MMR
system would silently change that architecture and is out of Phase 7's
scope. "School level"/"club"/"school" leaderboard filters are deferred, per
the spec's own "(future)" tags for those dimensions. "National" reuses the
global board verbatim (E-NOVAR is a single-country — Algeria — platform);
"Regional" stays architecture-ready-only via the wilaya filter, mirroring
Phase 6's `region` column precedent (stored, not yet used to restrict).
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from uuid import UUID

from sqlmodel import Session, func, select

from app.core.redis import get_redis_client
from app.models.arena_cosmetics import ArenaCosmetic, ArenaPlayerShowcase
from app.models.competitive import CompetitiveRatingHistory, CompetitiveSeasonStats, CompetitiveStatistics
from app.models.profile import Profile
from app.services.notification_engine import emit

logger = logging.getLogger(__name__)

PERIODS = ("daily", "weekly", "monthly", "season", "all_time")

#: Phase 13 — "Ranking Filters" (spec). Every sort besides 'rating' reads
#: from CompetitiveStatistics's own lifetime columns directly (wins/streak/
#: BR-wins/tournament-wins/club-wins are inherently lifetime-cumulative —
#: no per-season variant of them is ever stored anywhere, see
#: CompetitiveSeasonStats' own field list) — a period="season" board sorted
#: by one of these still scopes its PARTICIPANT SET to that season
#: (CompetitiveSeasonStats rows), it just orders by the lifetime column.
_SORT_COLUMNS = {
    "rating": CompetitiveStatistics.rating,
    "wins": CompetitiveStatistics.wins,
    "accuracy": CompetitiveStatistics.accuracy_pct,
    "streak": CompetitiveStatistics.longest_streak,
    "battle_royale_wins": CompetitiveStatistics.battle_royale_wins,
    "tournament_wins": CompetitiveStatistics.tournament_wins,
    "club_wins": CompetitiveStatistics.club_wins,
}


def _key_all_time() -> str:
    return "competitive:leaderboard:global:all_time"


def _key_season(season_id: UUID) -> str:
    return f"competitive:leaderboard:season:{season_id}"


def touch_user(*, user_id: UUID, rating: int, season_id: Optional[UUID] = None, season_mmr: Optional[int] = None) -> None:
    try:
        r = get_redis_client()
        r.zadd(_key_all_time(), {str(user_id): rating})
        if season_id is not None and season_mmr is not None:
            r.zadd(_key_season(season_id), {str(user_id): season_mmr})
    except Exception:
        logger.debug("leaderboard_service.touch_user: redis unavailable — cache skipped", exc_info=True)


def invalidate_all() -> None:
    """Admin-triggered full cache rebuild (e.g. after a manual ranking
    recalculation) — just drops the keys, they lazily rebuild from SQL on
    next read (see get_leaderboard's cache-miss fallback)."""
    try:
        r = get_redis_client()
        for key in r.scan_iter("competitive:leaderboard:*"):
            r.delete(key)
    except Exception:
        logger.debug("leaderboard_service.invalidate_all: redis unavailable", exc_info=True)


def _hydrate(db: Session, pairs: List[Tuple[str, float]], *, offset: int) -> List[Dict]:
    ids = [UUID(uid) for uid, _ in pairs]
    profiles = {p.id: p for p in db.exec(select(Profile).where(Profile.id.in_(ids))).all()} if ids else {}
    items = []
    for idx, (uid, score) in enumerate(pairs):
        profile = profiles.get(UUID(uid))
        items.append({
            "rank": offset + idx + 1, "user_id": uid,
            "full_name": profile.full_name if profile else None,
            "avatar_url": profile.avatar_url if profile else None,
            "mmr": int(score),
        })
    return items


def _attach_titles(db: Session, items: List[Dict]) -> List[Dict]:
    """Phase 14 — 'apply showcase cosmetics across surfaces': every
    leaderboard row gets the player's currently-equipped title in a single
    batch query (never per-row), whichever path produced the items (SQL or
    the Redis-cached hydrate path)."""
    if not items:
        return items
    ids = [UUID(it["user_id"]) for it in items]
    rows = db.exec(
        select(ArenaPlayerShowcase.user_id, ArenaCosmetic.name, ArenaCosmetic.rarity)
        .join(ArenaCosmetic, ArenaCosmetic.id == ArenaPlayerShowcase.active_title_id)
        .where(ArenaPlayerShowcase.user_id.in_(ids))
    ).all()
    titles = {str(uid): (name, rarity) for uid, name, rarity in rows}
    for it in items:
        title = titles.get(it["user_id"])
        it["equipped_title"] = title[0] if title else None
        it["title_rarity"] = title[1] if title else None
    return items


def _read_cache(key: str, *, page: int, size: int):
    try:
        r = get_redis_client()
        total = r.zcard(key)
        if not total:
            return None
        start = (page - 1) * size
        end = start + size - 1
        raw = r.zrevrange(key, start, end, withscores=True)
        pairs = [(uid.decode() if isinstance(uid, bytes) else uid, score) for uid, score in raw]
        return pairs, total, start
    except Exception:
        logger.debug("leaderboard_service: redis read failed — falling back to SQL", exc_info=True)
        return None


def _period_start(period: str) -> datetime:
    now = datetime.now(timezone.utc)
    return {"daily": now - timedelta(days=1), "weekly": now - timedelta(days=7), "monthly": now - timedelta(days=30)}[period]


def _apply_common_filters(query, *, subject_id, difficulty, league_id, language, wilaya, school_level_id):
    """Shared by all three query builders — every filter Phase 7 already
    had, plus Phase 13's school_level_id ('Academic Level', spec)."""
    if subject_id:
        query = query.where(CompetitiveStatistics.favorite_subject_id == subject_id)
    if difficulty:
        query = query.where(CompetitiveStatistics.favorite_difficulty == difficulty)
    if league_id:
        query = query.where(CompetitiveStatistics.league_id == league_id)
    if language:
        query = query.where(Profile.language == language)
    if wilaya:
        query = query.where(Profile.wilaya == wilaya)
    if school_level_id:
        from app.models.profile import StudentProfile
        query = query.join(StudentProfile, StudentProfile.user_id == Profile.id).where(StudentProfile.level_main == school_level_id)
    return query


def _sort_column(sort: Optional[str]):
    return _SORT_COLUMNS.get(sort, CompetitiveStatistics.rating)


def _query_all_time(db: Session, *, subject_id, difficulty, league_id, language, wilaya, school_level_id, sort, page, size) -> Tuple[List[Dict], int]:
    query = select(CompetitiveStatistics, Profile).join(Profile, Profile.id == CompetitiveStatistics.user_id)
    query = _apply_common_filters(query, subject_id=subject_id, difficulty=difficulty, league_id=league_id, language=language, wilaya=wilaya, school_level_id=school_level_id)

    total = db.exec(select(func.count()).select_from(query.subquery())).one()
    rows = db.exec(query.order_by(_sort_column(sort).desc()).offset((page - 1) * size).limit(size)).all()
    items = [
        {
            "rank": (page - 1) * size + idx + 1, "user_id": str(stats.user_id),
            "full_name": profile.full_name, "avatar_url": profile.avatar_url,
            "mmr": stats.rating, "matches_played": stats.matches_played,
            "wins": stats.wins, "accuracy_pct": stats.accuracy_pct,
            "longest_streak": stats.longest_streak, "battle_royale_wins": stats.battle_royale_wins,
            "tournament_wins": stats.tournament_wins, "club_wins": stats.club_wins,
        }
        for idx, (stats, profile) in enumerate(rows)
    ]
    return items, total


def _query_season(db: Session, *, season_id, subject_id, difficulty, league_id, language, wilaya, school_level_id, sort, page, size) -> Tuple[List[Dict], int]:
    query = (
        select(CompetitiveSeasonStats, Profile)
        .join(Profile, Profile.id == CompetitiveSeasonStats.user_id)
        .where(CompetitiveSeasonStats.season_id == season_id)
    )
    if league_id:
        query = query.where(CompetitiveSeasonStats.league_id == league_id)
    # Phase 13 — a non-'rating' sort (or subject/difficulty/school_level
    # filters) needs CompetitiveStatistics joined in; 'rating' sort alone
    # doesn't (CompetitiveSeasonStats.mmr is this season's own rating).
    needs_stats_join = sort not in (None, "rating") or subject_id or difficulty or school_level_id
    if needs_stats_join:
        query = query.join(CompetitiveStatistics, CompetitiveStatistics.user_id == CompetitiveSeasonStats.user_id)
        if subject_id:
            query = query.where(CompetitiveStatistics.favorite_subject_id == subject_id)
        if difficulty:
            query = query.where(CompetitiveStatistics.favorite_difficulty == difficulty)
    if language:
        query = query.where(Profile.language == language)
    if wilaya:
        query = query.where(Profile.wilaya == wilaya)
    if school_level_id:
        from app.models.profile import StudentProfile
        query = query.join(StudentProfile, StudentProfile.user_id == Profile.id).where(StudentProfile.level_main == school_level_id)

    total = db.exec(select(func.count()).select_from(query.subquery())).one()
    order_col = _sort_column(sort) if (sort and sort != "rating") else CompetitiveSeasonStats.mmr
    rows = db.exec(query.order_by(order_col.desc()).offset((page - 1) * size).limit(size)).all()
    items = [
        {
            "rank": row.final_rank or ((page - 1) * size + idx + 1), "user_id": str(row.user_id),
            "full_name": profile.full_name, "avatar_url": profile.avatar_url,
            "mmr": row.mmr, "matches_played": row.matches_played,
            "wins": row.wins, "accuracy_pct": row.accuracy_pct,
        }
        for idx, (row, profile) in enumerate(rows)
    ]
    return items, total


def _query_period(db: Session, *, period, subject_id, difficulty, league_id, language, wilaya, school_level_id, page, size) -> Tuple[List[Dict], int]:
    since = _period_start(period)
    agg = (
        select(
            CompetitiveRatingHistory.user_id.label("user_id"),
            func.sum(CompetitiveRatingHistory.delta).label("net_gain"),
        )
        .where(CompetitiveRatingHistory.created_at >= since)
        .group_by(CompetitiveRatingHistory.user_id)
        .subquery()
    )
    query = (
        select(agg.c.user_id, agg.c.net_gain, CompetitiveStatistics, Profile)
        .join(CompetitiveStatistics, CompetitiveStatistics.user_id == agg.c.user_id)
        .join(Profile, Profile.id == agg.c.user_id)
    )
    if subject_id:
        query = query.where(CompetitiveStatistics.favorite_subject_id == subject_id)
    if difficulty:
        query = query.where(CompetitiveStatistics.favorite_difficulty == difficulty)
    if league_id:
        query = query.where(CompetitiveStatistics.league_id == league_id)
    if language:
        query = query.where(Profile.language == language)
    if wilaya:
        query = query.where(Profile.wilaya == wilaya)
    if school_level_id:
        from app.models.profile import StudentProfile
        query = query.join(StudentProfile, StudentProfile.user_id == Profile.id).where(StudentProfile.level_main == school_level_id)

    total = db.exec(select(func.count()).select_from(query.subquery())).one()
    rows = db.exec(query.order_by(agg.c.net_gain.desc()).offset((page - 1) * size).limit(size)).all()
    items = [
        {
            "rank": (page - 1) * size + idx + 1, "user_id": str(user_id),
            "full_name": profile.full_name, "avatar_url": profile.avatar_url,
            "mmr": stats.rating, "net_gain": int(net_gain),
            "matches_played": stats.matches_played, "wins": stats.wins, "accuracy_pct": stats.accuracy_pct,
        }
        for idx, (user_id, net_gain, stats, profile) in enumerate(rows)
    ]
    return items, total


def get_leaderboard(
    db: Session, *, period: str = "all_time", season_id: Optional[UUID] = None,
    subject_id: Optional[UUID] = None, difficulty: Optional[str] = None,
    league_id: Optional[UUID] = None, language: Optional[str] = None, wilaya: Optional[str] = None,
    school_level_id: Optional[str] = None, sort: Optional[str] = None,
    page: int = 1, size: int = 50,
) -> Tuple[List[Dict], int]:
    items, total = _get_leaderboard_raw(
        db, period=period, season_id=season_id, subject_id=subject_id, difficulty=difficulty,
        league_id=league_id, language=language, wilaya=wilaya, school_level_id=school_level_id,
        sort=sort, page=page, size=size,
    )
    return _attach_titles(db, items), total


def _get_leaderboard_raw(
    db: Session, *, period: str = "all_time", season_id: Optional[UUID] = None,
    subject_id: Optional[UUID] = None, difficulty: Optional[str] = None,
    league_id: Optional[UUID] = None, language: Optional[str] = None, wilaya: Optional[str] = None,
    school_level_id: Optional[str] = None, sort: Optional[str] = None,
    page: int = 1, size: int = 50,
) -> Tuple[List[Dict], int]:
    if period not in PERIODS:
        raise ValueError(f"period must be one of {PERIODS}")
    page = max(1, page)
    size = max(1, min(size, 100))
    unfiltered = not any([subject_id, difficulty, league_id, language, wilaya, school_level_id]) and sort in (None, "rating")

    if period == "all_time":
        if unfiltered:
            cached = _read_cache(_key_all_time(), page=page, size=size)
            if cached is not None:
                pairs, total, offset = cached
                return _hydrate(db, pairs, offset=offset), total
        return _query_all_time(db, subject_id=subject_id, difficulty=difficulty, league_id=league_id, language=language, wilaya=wilaya, school_level_id=school_level_id, sort=sort, page=page, size=size)

    if period == "season":
        if season_id is None:
            from app.services.competitive import season_service
            active = season_service.get_active_season(db)
            season_id = active.id if active else None
        if season_id is None:
            return [], 0
        if unfiltered:
            cached = _read_cache(_key_season(season_id), page=page, size=size)
            if cached is not None:
                pairs, total, offset = cached
                return _hydrate(db, pairs, offset=offset), total
        return _query_season(db, season_id=season_id, subject_id=subject_id, difficulty=difficulty, league_id=league_id, language=language, wilaya=wilaya, school_level_id=school_level_id, sort=sort, page=page, size=size)

    return _query_period(db, period=period, subject_id=subject_id, difficulty=difficulty, league_id=league_id, language=language, wilaya=wilaya, school_level_id=school_level_id, page=page, size=size)


# ─── Personal rank position (profile display) ───────────────────────────────

def get_global_rank(db: Session, user_id: UUID) -> Optional[int]:
    stats = db.get(CompetitiveStatistics, user_id)
    if stats is None:
        return None
    higher = db.exec(
        select(func.count()).select_from(CompetitiveStatistics).where(CompetitiveStatistics.rating > stats.rating)
    ).one()
    return higher + 1


def get_season_rank(db: Session, season_id: UUID, user_id: UUID) -> Optional[int]:
    row = db.exec(
        select(CompetitiveSeasonStats)
        .where(CompetitiveSeasonStats.season_id == season_id)
        .where(CompetitiveSeasonStats.user_id == user_id)
    ).first()
    if row is None:
        return None
    if row.final_rank is not None:
        return row.final_rank
    higher = db.exec(
        select(func.count()).select_from(CompetitiveSeasonStats)
        .where(CompetitiveSeasonStats.season_id == season_id)
        .where(CompetitiveSeasonStats.mmr > row.mmr)
    ).one()
    return higher + 1


def check_and_notify_rank_milestones(db: Session, user_id: UUID) -> None:
    """Best-effort — a leaderboard-position notification must never break
    the match-completion flow that triggers it."""
    try:
        rank = get_global_rank(db, user_id)
        if rank is None:
            return
        today = datetime.now(timezone.utc).date().isoformat()
        if rank <= 10:
            emit(db, event_type="competitive_top_10_entry", user_id=user_id,
                 dedup_key=f"competitive_top_10_entry:{user_id}:{today}")
        elif rank <= 100:
            emit(db, event_type="competitive_top_100_entry", user_id=user_id,
                 dedup_key=f"competitive_top_100_entry:{user_id}:{today}")
    except Exception:
        logger.exception("leaderboard_service.check_and_notify_rank_milestones failed for user=%s", user_id)
