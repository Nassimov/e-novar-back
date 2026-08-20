from __future__ import annotations

import logging

from fastapi import HTTPException, Request, status

from app.core.redis import get_redis_client

logger = logging.getLogger(__name__)


def get_client_ip(request: Request) -> str:
    """Best-effort client IP, respecting X-Forwarded-For.

    Railway deployments sit behind a proxy, so `request.client.host` is the
    proxy's address, not the caller's. Mirrors the convention already used
    in app/routers/admin/auth.py (`_ip`).
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return (request.client.host if request.client else "") or "unknown"


def check_rate_limit(
    key: str,
    *,
    limit: int,
    window_seconds: int,
    fail_open: bool = True,
    detail: str = "Too many attempts. Please try again later.",
) -> None:
    """Fixed-window rate limiter backed by plain Redis INCR + EXPIRE.

    Raises HTTP 429 (with a `Retry-After` header) once more than `limit`
    calls have been made for `key` within the current `window_seconds`
    window. The window resets `window_seconds` after the first call that
    created the counter.

    Redis outage behaviour:
      - fail_open=True  (default): the check is skipped and the request is
        allowed through — a Redis blip should never take down login for
        regular users.
      - fail_open=False: the request is rejected with 503 instead — used for
        higher-value targets (admin login) where failing closed is safer
        than failing open.
    """
    try:
        redis_client = get_redis_client()
        current = redis_client.incr(key)
        if current == 1:
            # First hit in this window — start the TTL clock.
            redis_client.expire(key, window_seconds)
    except HTTPException:
        raise
    except Exception as exc:  # redis.exceptions.RedisError and friends
        logger.warning("Rate limiter unavailable (redis error) for key=%s: %s", key, exc)
        if fail_open:
            return
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service temporarily unavailable. Please try again shortly.",
        )

    if current > limit:
        try:
            ttl = redis_client.ttl(key)
        except Exception:
            ttl = -1
        retry_after = ttl if ttl and ttl > 0 else window_seconds
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=detail,
            headers={"Retry-After": str(retry_after)},
        )
