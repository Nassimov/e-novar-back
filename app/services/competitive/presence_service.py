from __future__ import annotations

"""
Presence Service — Competitive Arena Phase 2 Waiting Room.

Per-match presence lives in Redis (ephemeral, like the existing global
"chat:online_users" set) — never in Postgres, since it's a live/volatile
concept, not durable state. Statuses: online | connecting | disconnected.
"""

import json
from datetime import datetime, timezone
from typing import Dict
from uuid import UUID

from app.core.redis import get_redis_client

STATUS_ONLINE = "online"
STATUS_CONNECTING = "connecting"
STATUS_DISCONNECTED = "disconnected"


def _key(match_id: UUID) -> str:
    return f"competitive:match:{match_id}:presence"


def set_status(match_id: UUID, user_id: UUID, status: str) -> None:
    try:
        r = get_redis_client()
        r.hset(_key(match_id), str(user_id), json.dumps({
            "status": status,
            "last_seen": datetime.now(timezone.utc).isoformat(),
        }))
        r.expire(_key(match_id), 6 * 3600)  # matches are short-lived; avoid unbounded growth
    except Exception:
        pass


def get_presence(match_id: UUID) -> Dict[str, Dict[str, str]]:
    try:
        raw = get_redis_client().hgetall(_key(match_id))
    except Exception:
        return {}
    result = {}
    for user_id, payload in raw.items():
        try:
            result[user_id] = json.loads(payload)
        except Exception:
            continue
    return result


def clear_match(match_id: UUID) -> None:
    try:
        get_redis_client().delete(_key(match_id))
    except Exception:
        pass
