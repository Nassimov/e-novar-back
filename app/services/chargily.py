"""Chargily Pay v2 client — Edahabia (and CIB/chargily_app) payment rail.

Unlike Stripe (authorize now via manual capture, capture later when the
teacher accepts), Chargily has no auth/capture split and no refund
endpoint: a Checkout is charged in full the moment the student completes
payment on Chargily's hosted page. There is no way to reverse that via the
API — a refusal after payment needs a MANUAL refund from the Chargily
merchant dashboard (see app.routers.teachers.refuse_booking, which flags
this case with an admin notification rather than pretending to auto-refund).

Docs: https://dev.chargily.com/pay-v2/api-reference/checkouts/create
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Any, Dict, Optional

import httpx

from app.config import get_settings

settings = get_settings()

_TIMEOUT = 15


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.chargily_secret_key}",
        "Content-Type": "application/json",
    }


def create_checkout(
    amount_dzd: int,
    booking_id: str,
    payment_method: str,
    success_url: str,
    failure_url: str,
    webhook_url: str,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a Chargily Checkout. payment_method: "edahabia" | "cib" | "chargily_app".
    Returns the raw checkout object — callers read `id` and `checkout_url`."""
    payload: Dict[str, Any] = {
        "amount": amount_dzd,
        "currency": "dzd",
        "payment_method": payment_method,
        "success_url": success_url,
        "failure_url": failure_url,
        "webhook_endpoint": webhook_url,
        "metadata": {"booking_id": booking_id},
    }
    if description:
        payload["description"] = description

    resp = httpx.post(
        f"{settings.chargily_base_url}/checkouts",
        json=payload,
        headers=_headers(),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def retrieve_checkout(checkout_id: str) -> Dict[str, Any]:
    resp = httpx.get(
        f"{settings.chargily_base_url}/checkouts/{checkout_id}",
        headers=_headers(),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def verify_webhook_signature(payload: bytes, signature: Optional[str]) -> bool:
    """HMAC-SHA256(raw body, secret key), constant-time compare against the
    `signature` header. Reject if either side is missing."""
    if not signature or not settings.chargily_secret_key:
        return False
    expected = hmac.new(
        settings.chargily_secret_key.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
