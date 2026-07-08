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


def create_checkout_session(
    amount_dzd: int,
    booking_id: str,
    teacher_name: str,
    success_url: str,
    cancel_url: str,
) -> Dict[str, Any]:
    """Create a Stripe Checkout Session with manual capture (auth only)."""
    session = stripe_lib.checkout.Session.create(
        line_items=[
            {
                "price_data": {
                    "currency": "eur",
                    "product_data": {"name": f"Séance avec {teacher_name}"},
                    "unit_amount": max(50, amount_dzd),  # treat DZD as EUR cents for test
                },
                "quantity": 1,
            }
        ],
        mode="payment",
        payment_intent_data={"capture_method": "manual"},
        metadata={"booking_id": booking_id},
        success_url=success_url,
        cancel_url=cancel_url,
    )
    return {"session_id": session.id, "url": session.url}


def get_checkout_session(session_id: str) -> Dict[str, Any]:
    """Retrieve a Checkout Session, including its PaymentIntent ID."""
    session = stripe_lib.checkout.Session.retrieve(session_id)
    return {
        "session_id": session.id,
        "payment_intent": session.payment_intent,
        "payment_status": session.payment_status,
    }


def capture_payment_intent(pi_id: str) -> Dict[str, Any]:
    """Capture an authorized PaymentIntent (teacher accepts booking)."""
    intent = stripe_lib.PaymentIntent.capture(pi_id)
    return {"id": intent.id, "status": intent.status, "amount": intent.amount}


def cancel_payment_intent(pi_id: str) -> Dict[str, Any]:
    """Cancel an authorized PaymentIntent (teacher refuses booking)."""
    intent = stripe_lib.PaymentIntent.cancel(pi_id)
    return {"id": intent.id, "status": intent.status}
