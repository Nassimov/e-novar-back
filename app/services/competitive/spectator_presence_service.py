from __future__ import annotations

"""
Spectator Presence Service — Competitive Arena Phase 8.

Live "how many people are watching this match right now" lives in Redis
(ephemeral), exactly like app/services/competitive/presence_service.py's
Phase 2 waiting-room presence — never in Postgres, since it's a live/
volatile concept, not durable state (the durable join/leave record is
CompetitiveSpectator, written by the WS handler in app/main.py).

Tracked as a Redis SET of connected user_ids per match (SCARD = live count)
rather than a per-connection counter: a spectator with two open tabs still
only counts once, which matches "how many distinct people are watching"
better than "how many sockets are open". Trade-off (documented, accepted):
if the same user closes one of two tabs, SREM removes them from the set
even though their other tab is still connected — acceptable for a live
"nice to have" count, never used for anything security-sensitive.
"""

from uuid import UUID

from app.core.redis import get_redis_client


def _key(match_id) -> str:
    return f"competitive:spectate:{match_id}:viewers"


def add_viewer(match_id: UUID, user_id: UUID) -> int:
    """Registers this user as watching; returns the new live count."""
    try:
        r = get_redis_client()
        key = _key(match_id)
        r.sadd(key, str(user_id))
        r.expire(key, 6 * 3600)  # matches are short-lived; avoid unbounded growth
        return int(r.scard(key))
    except Exception:
        return 0


def remove_viewer(match_id: UUID, user_id: UUID) -> int:
    """Unregisters this user; returns the new live count."""
    try:
        r = get_redis_client()
        key = _key(match_id)
        r.srem(key, str(user_id))
        return int(r.scard(key))
    except Exception:
        return 0


def get_count(match_id: UUID) -> int:
    try:
        return int(get_redis_client().scard(_key(match_id)))
    except Exception:
        return 0


def get_counts(match_ids) -> dict:
    """Batch variant for the live-matches discovery/admin-analytics
    endpoints — one round-trip per match is fine at this scale (a handful of
    concurrently in_progress duels), matching the rest of this codebase's
    "don't over-engineer" posture for Phase 8."""
    return {str(mid): get_count(mid) for mid in match_ids}


def clear_match(match_id: UUID) -> None:
    try:
        get_redis_client().delete(_key(match_id))
    except Exception:
        pass
