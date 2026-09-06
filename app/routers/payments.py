from __future__ import annotations

import math
from typing import Any, Dict
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.booking import Booking
from app.models.enums import PaymentMethodType
from app.models.payment import Payment, PaymentStatus
from app.models.profile import Profile
from app.models.user import User
from app.schemas.payment import (
    CibPaymentRequest,
    InvoiceResponse,
    PaymentInitiate,
    PaymentResponse,
    TransferVerify,
)

router = APIRouter(tags=["payments"])


@router.post("/", response_model=PaymentResponse, status_code=status.HTTP_201_CREATED)
def initiate_payment(
    payload: PaymentInitiate,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Initiate a payment for a booking."""
    from datetime import datetime

    user = db.get(Profile, UUID(current_user["id"]))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    booking = db.get(Booking, payload.booking_id)
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")

    if booking.student_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    payment = Payment(
        user_id=user.id,
        booking_id=payload.booking_id,
        amount=payload.amount,
        currency=payload.currency,
        method_type=PaymentMethodType(payload.method),
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)
    return PaymentResponse.model_validate(payment)


@router.post("/cib", response_model=PaymentResponse, status_code=status.HTTP_201_CREATED)
def pay_with_cib(
    payload: CibPaymentRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Process a CIB card payment via Stripe."""
    from datetime import datetime
    from app.models.admin import PlatformSettings
    from app.services.stripe import create_payment_intent, dzd_to_eur_cents

    user = db.get(Profile, UUID(current_user["id"]))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    booking = db.get(Booking, payload.booking_id)
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")

    if booking.student_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    settings_row = db.get(PlatformSettings, True) or PlatformSettings()

    try:
        intent = create_payment_intent(
            amount_cents=dzd_to_eur_cents(booking.amount, settings_row.dzd_per_eur),
            currency="eur",
            metadata={"booking_id": str(booking.id), "user_id": str(user.id)},
        )
    except Exception as exc:
        raise HTTPException(status_code=402, detail=f"Payment failed: {exc}")

    payment = Payment(
        user_id=user.id,
        booking_id=payload.booking_id,
        amount=booking.amount,
        currency="DZD",
        method_type=PaymentMethodType.cib,
        status=PaymentStatus.pending,
        provider_ref=intent["id"],
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)

    # Schedule invoice generation
    try:
        from app.workers.pdf_tasks import generate_invoice_pdf
        generate_invoice_pdf.delay(str(payment.id))
    except Exception:
        pass

    return PaymentResponse.model_validate(payment)


@router.post("/verify-transfer", response_model=PaymentResponse)
def verify_transfer(
    payload: TransferVerify,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Verify a bank transfer payment by reference number."""
    from datetime import datetime

    payment = db.get(Payment, payload.payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")

    user = db.get(Profile, UUID(current_user["id"]))
    if user is None or payment.user_id != user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    payment.reference = payload.reference
    payment.status = PaymentStatus.pending  # Admin will confirm
    payment.notes = f"Transfer reference: {payload.reference}"
    if payload.screenshot_url:
        payment.notes += f" | Screenshot: {payload.screenshot_url}"
    payment.updated_at = datetime.utcnow()
    db.add(payment)
    db.commit()
    db.refresh(payment)
    return PaymentResponse.model_validate(payment)


@router.get("/invoices")
def list_invoices(
    page: int = 1,
    size: int = 20,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List invoices for the current user."""
    user = db.get(Profile, UUID(current_user["id"]))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    payments = db.exec(
        select(Payment).where(Payment.user_id == user.id, Payment.status == PaymentStatus.succeeded)
    ).all()
    total = len(payments)
    offset = (page - 1) * size
    paginated = payments[offset: offset + size]

    return {
        "items": [PaymentResponse.model_validate(p) for p in paginated],
        "total": total,
        "page": page,
        "size": size,
        "pages": math.ceil(total / size) if total else 0,
    }


@router.get("/invoices/{payment_id}", response_model=PaymentResponse)
def get_invoice(
    payment_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get a specific invoice."""
    payment = db.get(Payment, payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail="Invoice not found")

    user = db.get(Profile, UUID(current_user["id"]))
    if user and payment.user_id != user.id and current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    return PaymentResponse.model_validate(payment)


@router.get("/invoices/{payment_id}/pdf")
def get_invoice_pdf(
    payment_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get or generate invoice PDF URL."""
    payment = db.get(Payment, payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail="Invoice not found")

    if payment.invoice_url:
        return {"url": payment.invoice_url}

    # Trigger async PDF generation
    from app.workers.pdf_tasks import generate_invoice_pdf
    generate_invoice_pdf.delay(str(payment_id))
    return {"message": "PDF is being generated, please retry in a few seconds"}


@router.post("/webhook", include_in_schema=False)
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    """Handle Stripe webhooks."""
    from app.services.stripe import handle_webhook
    from datetime import datetime

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = handle_webhook(payload, sig_header)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Webhook error: {exc}")

    event_type = event.get("type")
    data = event.get("data", {})

    if event_type == "payment_intent.succeeded":
        payment_intent_id = data.get("id")
        if payment_intent_id:
            payment = db.exec(
                select(Payment).where(Payment.stripe_payment_intent_id == payment_intent_id)
            ).first()
            if payment:
                payment.status = PaymentStatus.succeeded
                payment.updated_at = datetime.utcnow()
                db.add(payment)

                if payment.booking_id:
                    booking = db.get(Booking, payment.booking_id)
                    if booking:
                        booking.payment_status = "completed"
                        db.add(booking)
                db.commit()

    elif event_type == "payment_intent.payment_failed":
        payment_intent_id = data.get("id")
        if payment_intent_id:
            payment = db.exec(
                select(Payment).where(Payment.stripe_payment_intent_id == payment_intent_id)
            ).first()
            if payment:
                payment.status = PaymentStatus.failed
                payment.updated_at = datetime.utcnow()
                db.add(payment)
                db.commit()

    return {"received": True}
