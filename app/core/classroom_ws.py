"""Shared pub/sub helper for the live classroom WS channel
(/ws/classroom/{session_id} in app/main.py).

REST mutation endpoints (app/routers/classroom.py — create/advance chapter,
dispatch quiz, tally answers, add file) call `publish_classroom_event()`
directly (synchronous, same convention as the existing chat presence
broadcasts) so every connected participant's WS subscriber picks the event
up immediately, regardless of which backend worker process handled the
REST call vs which one holds their WS connection.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

from app.core.redis import get_redis_client

logger = logging.getLogger(__name__)


def classroom_channel(room_key: str) -> str:
    return f"classroom:room:{room_key}"


def publish_classroom_event(room_key: str, event: Dict[str, Any]) -> None:
    try:
        get_redis_client().publish(classroom_channel(room_key), json.dumps(event))
    except Exception:
        logger.debug("classroom event publish failed room=%s", room_key, exc_info=True)
