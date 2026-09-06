"""Redis-backed pairing for second-camera devices (see app/models/classroom.py's
SessionCamera). The pairing secret (QR token + human-entry code) is
deliberately never persisted to Postgres — only a short-lived Redis record,
single-use (GETDEL), TTL-bound. Postgres only ever sees the resulting
SessionCamera row (metadata/audit), never the secret itself.
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from app.core.redis import get_redis_client

logger = logging.getLogger(__name__)

PAIRING_TTL_SECONDS = 120
_CODE_ATTEMPTS = 5


def _pairing_key(token: str) -> str:
    return f"camera_pairing:{token}"


def _code_key(code: str) -> str:
    return f"camera_pairing_code:{code}"


def create_pairing(*, camera_id: str, session_id: str, teacher_id: str, room_key: str) -> Dict[str, Any]:
    """Generates a fresh, single-use pairing token + 6-digit fallback code,
    both TTL'd at PAIRING_TTL_SECONDS. The code is just a second Redis key
    pointing at the same record — resolved to the real token via
    resolve_code() before claiming, never a separate source of truth."""
    r = get_redis_client()
    record = {"camera_id": camera_id, "session_id": session_id, "teacher_id": teacher_id, "room_key": room_key}
    payload = json.dumps(record)

    token = secrets.token_urlsafe(24)

    code = None
    for _ in range(_CODE_ATTEMPTS):
        candidate = f"{secrets.randbelow(1_000_000):06d}"
        # NX — never clobber a still-active pairing another camera happens
        # to have generated the same 6-digit code for (rare, but the retry
        # loop makes it a non-issue either way).
        if r.set(_code_key(candidate), token, ex=PAIRING_TTL_SECONDS, nx=True):
            code = candidate
            break
    if code is None:
        logger.warning("camera pairing: failed to allocate a unique code after %d attempts", _CODE_ATTEMPTS)
        code = f"{secrets.randbelow(1_000_000):06d}"
        r.set(_code_key(code), token, ex=PAIRING_TTL_SECONDS)

    r.set(_pairing_key(token), payload, ex=PAIRING_TTL_SECONDS)

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=PAIRING_TTL_SECONDS)
    return {"token": token, "code": code, "expires_at": expires_at}


def resolve_code(code: str) -> Optional[str]:
    """Human-entry fallback — resolves a 6-digit code to its pairing token
    (does NOT consume it; the token still needs to go through claim_pairing)."""
    return get_redis_client().get(_code_key(code))


def claim_pairing(token: str) -> Optional[Dict[str, Any]]:
    """Single-use claim: atomically pops the pairing record so a second
    claim of the same token (replay, or two devices scanning the same QR)
    always fails with None, even under a race."""
    r = get_redis_client()
    raw = r.getdel(_pairing_key(token))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def revoke_camera(camera_id: str, ttl_seconds: int = 60 * 60 * 12) -> None:
    """Immediately locks out a camera's JWT (teacher disconnect, or session
    end cleanup) — get_current_camera checks this key on every request. TTL
    matches the JWT's own generous session-length expiry so the key doesn't
    outlive what it's guarding against forever."""
    get_redis_client().set(f"camera:revoked:{camera_id}", "1", ex=ttl_seconds)
