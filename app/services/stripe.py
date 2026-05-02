from __future__ import annotations

from typing import Any, Dict, Optional

import stripe as stripe_lib

from app.config import get_settings

settings = get_settings()
stripe_lib.api_key = settings.stripe_secret_key


def create_payment_intent(
    amount_cents: int,
    currency: str = "eur",
    metadata: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Create a Stripe PaymentIntent. Amount is in cents."""
    intent = stripe_lib.PaymentIntent.create(
        amount=amount_cents,
        currency=currency.lower(),
        metadata=metadata or {},
        automatic_payment_methods={"enabled": True},
    )
    return {
        "id": intent.id,
        "client_secret": intent.client_secret,
        "status": intent.status,
        "amount": intent.amount,
        "currency": intent.currency,
    }


def confirm_payment(payment_intent_id: str) -> Dict[str, Any]:
    """Confirm a PaymentIntent by ID."""
    intent = stripe_lib.PaymentIntent.retrieve(payment_intent_id)
    return {
        "id": intent.id,
        "status": intent.status,
        "amount": intent.amount,
        "currency": intent.currency,
    }


def handle_webhook(payload: bytes, sig_header: str) -> Dict[str, Any]:
    """Verify and parse a Stripe webhook event."""
    event = stripe_lib.Webhook.construct_event(
        payload, sig_header, settings.stripe_webhook_secret
    )
    return {
        "type": event["type"],
        "data": event["data"]["object"],
    }


def create_refund(payment_intent_id: str, amount_cents: Optional[int] = None) -> Dict[str, Any]:
    """Create a refund for a PaymentIntent. Pass amount_cents for partial refund."""
    kwargs: Dict[str, Any] = {"payment_intent": payment_intent_id}
    if amount_cents is not None:
        kwargs["amount"] = amount_cents
    refund = stripe_lib.Refund.create(**kwargs)
    return {
        "id": refund.id,
        "status": refund.status,
        "amount": refund.amount,
    }
