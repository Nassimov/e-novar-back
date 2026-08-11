"""Shared pub/sub helper for the live competitive Waiting Room WS channel
(/ws/competitive/{match_id} in app/main.py). Mirrors app/core/classroom_ws.py's
convention exactly — REST mutation endpoints (ready toggle, schedule accept)
call `publish_competitive_event()` directly so every connected participant's
WS subscriber picks the event up immediately, regardless of which backend
worker process handled the REST call vs which one holds the WS connection.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

from app.core.redis import get_redis_client

logger = logging.getLogger(__name__)


def competitive_channel(match_id: str) -> str:
    return f"competitive:match:{match_id}"


def publish_competitive_event(match_id: str, event: Dict[str, Any]) -> None:
    try:
        get_redis_client().publish(competitive_channel(match_id), json.dumps(event))
    except Exception:
        logger.debug("competitive event publish failed match=%s", match_id, exc_info=True)
