from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import stripe as stripe_lib

from app.config import get_settings

settings = get_settings()
stripe_lib.api_key = settings.stripe_secret_key
logger = logging.getLogger(__name__)


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


def dzd_to_eur_cents(amount_dzd: int, dzd_per_eur: float) -> int:
    """Convert a DZD price to EUR cents using the admin-configured rate
    (PlatformSettings.dzd_per_eur — see app/routers/admin/settings.py).
    Stripe requires a minimum charge (~0.50 EUR), enforced with a floor."""
    if dzd_per_eur <= 0:
        dzd_per_eur = 145.00  # defensive fallback — should never happen, admin field has a DB default
    eur_cents = round((amount_dzd / dzd_per_eur) * 100)
    if eur_cents < 50:
        # This floor exists for genuinely tiny/promo prices, but it has also
        # masked real pricing bugs before (a booking resolving to amount_dzd=0
        # silently became a plausible-looking "0.50€" charge instead of an
        # obvious error) — log it so a wrong upstream price is caught fast.
        logger.warning(
            "dzd_to_eur_cents: amount_dzd=%s at rate=%s converts to %sc, floored to 50c — "
            "verify the caller resolved a real price, not a stale/zero default",
            amount_dzd, dzd_per_eur, eur_cents,
        )
    return max(50, eur_cents)


def create_checkout_session(
    amount_dzd: int,
    dzd_per_eur: float,
    booking_id: str,
    teacher_name: str,
    success_url: str,
    cancel_url: str,
) -> Dict[str, Any]:
    """Create a Stripe Checkout Session with manual capture (auth only).
    Charges in EUR — Stripe has no DZD settlement currency — converted from
    the DZD price using the admin-configured exchange rate."""
    unit_amount = dzd_to_eur_cents(amount_dzd, dzd_per_eur)
    session = stripe_lib.checkout.Session.create(
        line_items=[
            {
                "price_data": {
                    "currency": "eur",
                    "product_data": {"name": f"Séance avec {teacher_name}"},
                    "unit_amount": unit_amount,
                },
                "quantity": 1,
            }
        ],
        mode="payment",
        payment_intent_data={"capture_method": "manual"},
        metadata={"booking_id": booking_id, "amount_dzd": str(amount_dzd), "dzd_per_eur": str(dzd_per_eur)},
        success_url=success_url,
        cancel_url=cancel_url,
    )
    return {"session_id": session.id, "url": session.url}


def get_checkout_session(session_id: str) -> Dict[str, Any]:
    """Retrieve a Checkout Session, including its PaymentIntent ID and status.

    `payment_status` on the Session itself only ever means "paid" once funds
    are actually captured — since checkout sessions here use manual capture
    (see create_checkout_session), a freshly-completed checkout reports
    payment_intent_status="requires_capture" (card verified, hold placed,
    nothing charged yet), not "succeeded". Callers that need to know "did the
    card get authorized" should check payment_intent_status, not payment_status.
    """
    session = stripe_lib.checkout.Session.retrieve(session_id, expand=["payment_intent"])
    pi = session.payment_intent
    return {
        "session_id": session.id,
        "payment_intent": pi.id if pi else None,
        "payment_intent_status": pi.status if pi else None,
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
