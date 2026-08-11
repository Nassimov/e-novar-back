from __future__ import annotations

"""Shared pub/sub helper for the Competitive Arena Spectator Mode WS channel
(/ws/competitive/{match_id}/spectate in app/main.py) — Phase 8. Exact mirror
of app/core/competitive_ws.py's player channel, kept as a SEPARATE channel
(rather than reusing competitive_channel) so relaying spectator-only chatter
(chat/reactions/predictions/spectator_count) never touches the player WS
handler's message loop, and so a spectator_update signal can never be
confused with a player-facing gameplay_update signal."""

import json
import logging
from typing import Any, Dict

from app.core.redis import get_redis_client

logger = logging.getLogger(__name__)


def spectator_channel(match_id: str) -> str:
    return f"competitive:spectate:{match_id}"


def publish_spectator_event(match_id: str, event: Dict[str, Any]) -> None:
    try:
        get_redis_client().publish(spectator_channel(match_id), json.dumps(event))
    except Exception:
        logger.debug("spectator event publish failed match=%s", match_id, exc_info=True)
