from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

ONESIGNAL_API_URL = "https://onesignal.com/api/v1/notifications"


def send_push(
    user_ids: List[str],
    title: str,
    body: str,
    data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Send a push notification to specific users via OneSignal."""
    if not settings.onesignal_app_id or not settings.onesignal_rest_api_key:
        logger.warning("OneSignal not configured, skipping push notification")
        return {"status": "skipped", "reason": "not_configured"}

    payload: Dict[str, Any] = {
        "app_id": settings.onesignal_app_id,
        "include_external_user_ids": user_ids,
        "headings": {"en": title, "ar": title, "fr": title},
        "contents": {"en": body, "ar": body, "fr": body},
        "channel_for_external_user_ids": "push",
    }
    if data:
        payload["data"] = data

    headers = {
        "Authorization": f"Basic {settings.onesignal_rest_api_key}",
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=10.0) as client:
        response = client.post(ONESIGNAL_API_URL, json=payload, headers=headers)
        response.raise_for_status()
        return response.json()


def send_push_to_all(title: str, body: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Send a broadcast push notification to all subscribers."""
    if not settings.onesignal_app_id or not settings.onesignal_rest_api_key:
        logger.warning("OneSignal not configured, skipping broadcast push")
        return {"status": "skipped", "reason": "not_configured"}

    payload: Dict[str, Any] = {
        "app_id": settings.onesignal_app_id,
        "included_segments": ["All"],
        "headings": {"en": title, "ar": title, "fr": title},
        "contents": {"en": body, "ar": body, "fr": body},
    }
    if data:
        payload["data"] = data

    headers = {
        "Authorization": f"Basic {settings.onesignal_rest_api_key}",
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=10.0) as client:
        response = client.post(ONESIGNAL_API_URL, json=payload, headers=headers)
        response.raise_for_status()
        return response.json()
