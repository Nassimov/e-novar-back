from __future__ import annotations

"""
Single entry point for actually moving money back to a student — every
cancellation/no-show path (voluntary student cancellation, teacher
cancelling a confirmed booking, admin rejecting a disputed session, the
no-response auto-cancel job) must go through refund_amount_for_booking()
instead of only computing a number and stopping there.

Payment method behavior:
  cib (Stripe)     — manual capture. If the booking never got captured
                     (still "pending"), there's nothing to refund — the
                     hold is simply released (handled by the caller via
                     cancel_payment_intent, e.g. refuse_booking / the
                     no-response job). Once captured (booking reached
                     "confirmed"), a real refund is issued via the Stripe
                     Refund API for the requested amount.
  edahabia         — Chargily charges immediately, no refund API exists.
                     Every refund request here is flagged to all admins for
                     manual processing through the Chargily dashboard —
                     never silently dropped.
  cash / transfer   — money never touched the platform's payment gateway;
                     nothing to call. Admin is notified to handle it
                     manually (return cash / reverse the transfer).
"""

import logging
from typing import Optional

from sqlmodel import Session, select

from app.models.booking import Booking
from app.models.profile import UserRole
from app.services.notification_engine import emit
from app.services.pricing import PACK_SIZES

logger = logging.getLogger(__name__)


def per_lesson_amount(booking: Booking) -> int:
    """A pack (pack5/pack10) shares ONE Booking row across all its lessons —
    refunding/crediting a single lesson must use its fair share, never the
    whole pack's price."""
    return round(booking.amount / PACK_SIZES.get(booking.formula, 1))


def refund_amount_for_booking(db: Session, booking: Booking, amount_dzd: int, *, note: str) -> dict:
    """
    Attempt to actually refund `amount_dzd` (DZD, same "1 unit = 1 Stripe
    cent" convention used at checkout — see app.services.stripe
    .create_checkout_session) to the student for this booking.

    Returns {"refunded": bool, "requires_manual_action": bool, "method": str}.
    Never raises — a failed/impossible refund is reported back for the
    caller to notify admins, not swallowed silently. Also notifies a linked
    parent (accepted app.models.parent_link.ParentStudentLink), if any —
    they're typically the one who actually paid.
    """
    if amount_dzd <= 0:
        return {"refunded": False, "requires_manual_action": False, "method": booking.payment_method or "unknown"}

    method = booking.payment_method or "unknown"
    result: dict

    if method == "cib":
        if booking.stripe_pi_id:
            try:
                from app.services.stripe import create_refund
                create_refund(booking.stripe_pi_id, amount_cents=amount_dzd)
                logger.info("Stripe refund issued: booking_id=%s amount=%d note=%s", booking.id, amount_dzd, note)
                result = {"refunded": True, "requires_manual_action": False, "method": "cib"}
            except Exception as exc:
                logger.error("Stripe refund FAILED: booking_id=%s amount=%d error=%s", booking.id, amount_dzd, exc)
                _flag_admins_manual_refund(db, booking, amount_dzd, f"Le remboursement Stripe automatique a échoué ({exc}). {note}")
                result = {"refunded": False, "requires_manual_action": True, "method": "cib"}
        else:
            # Never captured (still just an authorization) — nothing was ever
            # charged, so there's nothing to refund; the hold itself is
            # released by the caller (cancel_payment_intent).
            return {"refunded": False, "requires_manual_action": False, "method": "cib"}
    elif method == "edahabia":
        _flag_admins_manual_refund(db, booking, amount_dzd, f"Paiement Edahabia — Chargily n'a pas d'API de remboursement. {note}")
        result = {"refunded": False, "requires_manual_action": True, "method": "edahabia"}
    elif method in ("cash", "transfer", "rib_cib", "rib_edahabia"):
        _flag_admins_manual_refund(db, booking, amount_dzd, f"Paiement {method} — aucune passerelle à rembourser automatiquement. {note}")
        result = {"refunded": False, "requires_manual_action": True, "method": method}
    else:
        return {"refunded": False, "requires_manual_action": False, "method": method}

    if result["refunded"]:
        emit(
            db, event_type="refund_completed", user_id=booking.student_id,
            context={"amount": amount_dzd},
            data={"booking_id": str(booking.id), "amount": amount_dzd},
            dedup_key=f"refund_completed:{booking.id}:{amount_dzd}",
        )
    _notify_parent_of_refund(db, booking, amount_dzd, result)
    return result


def _notify_parent_of_refund(db: Session, booking: Booking, amount_dzd: int, result: dict) -> None:
    from app.models.parent_link import ParentStudentLink

    parent_links = db.exec(
        select(ParentStudentLink).where(
            ParentStudentLink.student_id == booking.student_id,
            ParentStudentLink.status == "accepted",
        )
    ).all()
    if not parent_links:
        return
    body = (
        f"{amount_dzd} DA ont été remboursés pour la réservation de votre enfant."
        if result["refunded"] else
        f"Un remboursement de {amount_dzd} DA est en cours de traitement pour la réservation de votre enfant."
    )
    for link in parent_links:
        emit(
            db, event_type="child_refund_alert", user_id=link.parent_id,
            title_override="Remboursement — séance de votre enfant",
            body_override=body,
            data={"booking_id": str(booking.id), "amount": amount_dzd, "refunded": result["refunded"]},
            dedup_key=f"child_refund_alert:{booking.id}:{amount_dzd}:{link.parent_id}",
        )


def _flag_admins_manual_refund(db: Session, booking: Booking, amount_dzd: int, reason: str) -> None:
    admin_roles = db.exec(select(UserRole).where(UserRole.role == "admin")).all()
    for ar in admin_roles:
        emit(
            db, event_type="system", user_id=ar.user_id,
            title_override="⚠️ Remboursement manuel requis",
            body_override=f"Remboursement de {amount_dzd} DA requis pour la réservation {booking.id}. {reason}",
            data={"booking_id": str(booking.id), "amount": amount_dzd},
            dedup_key=f"manual_refund_needed:{booking.id}:{amount_dzd}:{ar.user_id}",
        )
