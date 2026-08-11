from __future__ import annotations

"""
Domain Event subscribers — Competitive Arena Phase 16.

Imported once from app/main.py at startup (import-for-side-effects: calling
domain_events.subscribe()). Kept in its own module, separate from
domain_events.py itself, so publishers (gameplay_service, reward_service,
...) never need to import metrics_service/moderation_service just to
publish an event — avoids the import cycle that would otherwise force.
"""

import logging
from datetime import datetime, timezone

from app.core import domain_events

logger = logging.getLogger(__name__)


def _on_match_ended(**payload) -> None:
    try:
        from app.core.redis import get_redis_client
        r = get_redis_client()
        today = datetime.now(timezone.utc).date().isoformat()
        key = f"arena:metrics:matches_completed:{today}"
        r.incr(key)
        r.expire(key, 60 * 60 * 48)  # 48h — comfortably covers a day-boundary read
    except Exception:
        logger.debug("domain_events_bootstrap._on_match_ended: redis metrics counter failed", exc_info=True)


def register() -> None:
    domain_events.subscribe(domain_events.MATCH_ENDED, _on_match_ended)
