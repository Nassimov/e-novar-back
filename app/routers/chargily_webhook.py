from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session, select

from app.dependencies import get_db
from app.models.booking import Booking
from app.services.chargily import verify_webhook_signature

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Payments"])


@router.post("/chargily/webhook")
async def chargily_webhook(request: Request, db: Session = Depends(get_db)):
    """Receive Chargily Pay events. Only `checkout.paid` matters here — it's
    the sole trigger that lets a teacher accept an Edahabia booking (see
    app.routers.teachers.accept_booking, gated on `chargily_paid_at`).

    Signature: HMAC-SHA256(raw body, secret key) in the `signature` header —
    verified before touching anything, per Chargily's docs.
    """
    raw_body = await request.body()
    signature = request.headers.get("signature")

    if not verify_webhook_signature(raw_body, signature):
        raise HTTPException(status_code=403, detail="Invalid webhook signature")

    event: Dict[str, Any] = await request.json()
    event_type = event.get("type")
    data = event.get("data", {})
    checkout_id = data.get("id")

    if not checkout_id:
        return {"received": True}

    booking = db.exec(
        select(Booking).where(Booking.chargily_checkout_id == checkout_id)
    ).first()
    if booking is None:
        logger.warning("Chargily webhook: no booking for checkout_id=%s", checkout_id)
        return {"received": True}

    if event_type == "checkout.paid":
        if booking.chargily_paid_at is None:
            booking.chargily_paid_at = datetime.utcnow()
            db.add(booking)
            db.commit()

            from app.services.notification_engine import emit
            emit(
                db, event_type="booking", user_id=booking.teacher_id,
                title_override="💰 Nouveau paiement Edahabia reçu",
                body_override="Un élève a payé sa réservation via Edahabia. Acceptez ou refusez la demande.",
                data={"booking_id": str(booking.id)},
            )
    elif event_type in ("checkout.failed", "checkout.canceled"):
        if booking.status == "pending" and booking.chargily_paid_at is None:
            booking.status = "cancelled"
            db.add(booking)
            db.commit()

    return {"received": True}
