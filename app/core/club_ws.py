"""Shared pub/sub helper for the live Club WS channel (/ws/club/{club_id} in
app/main.py). Mirrors app/core/competitive_ws.py's convention exactly — REST
mutation endpoints (chat send/pin/delete) call `publish_club_event()`
directly so every connected member's WS subscriber picks the event up
immediately, regardless of which backend worker process handled the REST
call vs which one holds the WS connection."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

from app.core.redis import get_redis_client

logger = logging.getLogger(__name__)


def club_channel(club_id: str) -> str:
    return f"club:room:{club_id}"


def publish_club_event(club_id: str, event: Dict[str, Any]) -> None:
    try:
        get_redis_client().publish(club_channel(club_id), json.dumps(event))
    except Exception:
        logger.debug("club event publish failed club=%s", club_id, exc_info=True)
