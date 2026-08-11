from __future__ import annotations

"""
Shared rate limiter — Competitive Arena Phase 16.

Extracted from four previously-duplicated fixed-window Redis implementations
(app/routers/messages.py, app/routers/admin/notification_campaigns.py,
app/services/club/chat_service.py, app/services/competitive/
spectator_service.py) — same fail-open behavior as all four originals
(a Redis hiccup must never block the request it's protecting; rate limiting
is a secondary protection, not a hard dependency), now with one
implementation instead of four.
"""

import logging

from fastapi import HTTPException, status

from app.core.redis import get_redis_client

logger = logging.getLogger(__name__)


def check_rate_limit(*, key: str, limit: int, window_seconds: int = 10, message: str = "Trop de requêtes. Merci de patienter.") -> None:
    """Raises 429 if `key` has been hit more than `limit` times inside the
    current `window_seconds` fixed window. Call inline at the top of the
    endpoint/service function being protected — e.g.
    check_rate_limit(key=f"match_create:{user_id}", limit=settings_row.
    rate_limit_match_creation_per_10s)."""
    try:
        r = get_redis_client()
        n = r.incr(key)
        if n == 1:
            r.expire(key, window_seconds)
        if n > limit:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=message)
    except HTTPException:
        raise
    except Exception:
        logger.debug("rate_limiter.check_rate_limit: redis unavailable — fail-open for key=%s", key, exc_info=True)
