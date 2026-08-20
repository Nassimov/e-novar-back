from __future__ import annotations

import json
import logging
from typing import Any, Callable

from app.core.redis import get_redis_client

logger = logging.getLogger(__name__)


def cached_json(key: str, ttl: int, compute: Callable[[], Any]) -> Any:
    """Redis-backed cache for cheap, rarely-changing JSON-serializable reads
    (public catalogs, platform settings, leaderboard/stats). Caching is a
    performance optimization, never a hard dependency — any Redis failure
    (down, timeout, bad data) falls straight through to `compute()` so a
    cache outage can never turn into a 500 for the caller.
    """
    client = None
    try:
        client = get_redis_client()
        raw = client.get(key)
        if raw is not None:
            return json.loads(raw)
    except Exception:
        logger.warning("cache read failed for key=%s — falling back to source", key, exc_info=True)

    value = compute()

    if client is not None:
        try:
            client.set(key, json.dumps(value), ex=ttl)
        except Exception:
            logger.warning("cache write failed for key=%s", key, exc_info=True)

    return value


def cache_invalidate(*keys: str) -> None:
    """Best-effort cache-bust after a write to something a cached_json() read
    covers (e.g. admin edits a subject) — safe no-op if Redis is unavailable."""
    if not keys:
        return
    try:
        get_redis_client().delete(*keys)
    except Exception:
        logger.warning("cache invalidate failed for keys=%s", keys, exc_info=True)
