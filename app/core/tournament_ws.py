from __future__ import annotations

"""Shared pub/sub helper for the Tournament System's WebSocket channel
(/ws/competitive/tournaments/{tournament_id} in app/main.py) — Phase 9 Part B.
Exact mirror of app/core/competitive_ws.py / app/core/spectator_ws.py's
pattern, but this channel is SIGNAL-ONLY: unlike the per-match player/
spectator channels (which push a full computed gameplay/spectator state on
every event), a tournament bracket payload is comparatively large/complex,
so the simplest-correct design is to just tell every connected client
"something changed, refetch" (GET /api/competitive/tournaments/{id}/bracket)
via one of a small set of typed signal events: bracket_updated,
round_started, round_completed, match_created, participant_advanced,
tournament_completed. No hidden-info concern here (unlike gameplay state) —
the bracket is fully public data, so there's no per-viewer computation to
do in the first place."""

import json
import logging
from typing import Any, Dict
from uuid import UUID

from app.core.redis import get_redis_client

logger = logging.getLogger(__name__)


def tournament_channel(tournament_id) -> str:
    return f"competitive:tournament:{tournament_id}"


def publish_tournament_event(tournament_id, event: Dict[str, Any]) -> None:
    try:
        get_redis_client().publish(tournament_channel(tournament_id), json.dumps(event, default=str))
    except Exception:
        logger.debug("tournament event publish failed tournament_id=%s", tournament_id, exc_info=True)
