from __future__ import annotations

"""
OneSignal service — single transport layer for push + email.

Push:
  send_push(user_ids, ...)             → push to specific users by profile UUID
  send_push_to_segment(segment, ...)   → push to a named segment (All/students/teachers/parents)

Email (transactional):
  send_email(to, subject, html)        → email to a specific address (no subscription required)

Email (campaigns / automations):
  send_email_campaign(segment, ...)    → email to a whole segment (students/teachers/parents/All)

User registration:
  register_user(user_id, email, role)  → must be called on register + login so push targeting works.
                                         Also registers the email channel so segment campaigns reach them.

OneSignal segments to create in the dashboard:
  - "All"        → everyone
  - "students"   → Tag: role = student
  - "teachers"   → Tag: role = teacher
  - "parents"    → Tag: role = parent
"""

import logging
from typing import Any, Dict, List, Optional

import httpx

from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

_BASE = "https://onesignal.com/api/v1"
_FROM_NAME = "Enovar"
_FROM_ADDRESS = "noreply@enovar.dz"


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Basic {settings.onesignal_rest_api_key}",
        "Content-Type": "application/json",
    }


def _configured() -> bool:
    if not settings.onesignal_app_id or not settings.onesignal_rest_api_key:
        logger.warning("OneSignal not configured — ONESIGNAL_APP_ID or ONESIGNAL_REST_API_KEY missing")
        return False
    return True


def _post(endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.post(f"{_BASE}/{endpoint}", json=payload, headers=_headers())
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        logger.error("OneSignal %s HTTP %s: %s", endpoint, exc.response.status_code, exc.response.text)
        return {"status": "error", "reason": exc.response.text}
    except Exception as exc:
        logger.error("OneSignal %s failed: %s", endpoint, exc)
        return {"status": "error", "reason": str(exc)}


# ─── User Registration ────────────────────────────────────────────────────────

def register_user(user_id: str, email: Optional[str] = None, role: Optional[str] = None) -> bool:
    """
    Register/update a user in OneSignal.

    Push channel: the frontend OneSignal SDK handles device token creation.
    Here we register the email channel (device_type=11) so the user appears
    in segments and receives campaign emails.
    Also sets the `role` tag used for segmentation (students / teachers / parents).
    """
    if not _configured():
        return False

    tags = {}
    if role:
        tags["role"] = role

    # Register email channel (device_type 11) so the user receives campaign emails
    if email:
        email_payload: Dict[str, Any] = {
            "app_id": settings.onesignal_app_id,
            "device_type": 11,
            "identifier": email,
            "external_user_id": user_id,
        }
        if tags:
            email_payload["tags"] = tags
        result = _post("players", email_payload)
        if result.get("status") == "error":
            logger.warning("OneSignal email channel registration failed for %s", user_id)
            return False

    return True


# ─── Push ─────────────────────────────────────────────────────────────────────

def send_push(
    user_ids: List[str],
    title: str,
    body: str,
    data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Send a push notification to specific users by their profile UUID."""
    if not _configured():
        return {"status": "skipped", "reason": "not_configured"}

    payload: Dict[str, Any] = {
        "app_id": settings.onesignal_app_id,
        "include_external_user_ids": user_ids,
        "channel_for_external_user_ids": "push",
        "headings": {"en": title, "ar": title, "fr": title},
        "contents": {"en": body, "ar": body, "fr": body},
    }
    if data:
        payload["data"] = data

    return _post("notifications", payload)


def send_push_to_segment(
    segment: str,
    title: str,
    body: str,
    data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Send a push to a named OneSignal segment.
    segment: 'All' | 'students' | 'teachers' | 'parents'
    Segments must be created first in the OneSignal dashboard using the 'role' tag.
    """
    if not _configured():
        return {"status": "skipped", "reason": "not_configured"}

    payload: Dict[str, Any] = {
        "app_id": settings.onesignal_app_id,
        "included_segments": [segment],
        "headings": {"en": title, "ar": title, "fr": title},
        "contents": {"en": body, "ar": body, "fr": body},
    }
    if data:
        payload["data"] = data

    return _post("notifications", payload)


def send_push_to_all(title: str, body: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return send_push_to_segment("All", title, body, data)


# ─── Email (transactional) ────────────────────────────────────────────────────

def send_email(
    to: str,
    subject: str,
    html: str,
    from_name: str = _FROM_NAME,
    from_address: str = _FROM_ADDRESS,
) -> Dict[str, Any]:
    """
    Send a transactional email to a specific address via OneSignal.
    Uses include_email_tokens so no prior subscription is required.
    """
    if not _configured():
        return {"status": "skipped", "reason": "not_configured"}

    payload: Dict[str, Any] = {
        "app_id": settings.onesignal_app_id,
        "include_email_tokens": [to],
        "email_subject": subject,
        "email_body": html,
        "email_from_name": from_name,
        "email_from_address": from_address,
    }

    return _post("notifications", payload)


# ─── Email (campaigns / automations) ─────────────────────────────────────────

def send_email_campaign(
    segment: str,
    subject: str,
    html: str,
    from_name: str = _FROM_NAME,
    from_address: str = _FROM_ADDRESS,
) -> Dict[str, Any]:
    """
    Send a marketing/automation email to a segment via OneSignal.
    segment: 'All' | 'students' | 'teachers' | 'parents'
    Users must be registered (register_user called) to appear in segments.
    """
    if not _configured():
        return {"status": "skipped", "reason": "not_configured"}

    payload: Dict[str, Any] = {
        "app_id": settings.onesignal_app_id,
        "included_segments": [segment],
        "target_channel": "email",
        "email_subject": subject,
        "email_body": html,
        "email_from_name": from_name,
        "email_from_address": from_address,
    }

    return _post("notifications", payload)
