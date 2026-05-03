from __future__ import annotations

import logging

from app.config import get_settings
from app.workers.celery_app import celery_app

settings = get_settings()
logger = logging.getLogger(__name__)


def _get_twilio_client():
    from twilio.rest import Client
    return Client(settings.twilio_account_sid, settings.twilio_auth_token)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_sms(self, to_phone: str, message: str) -> bool:
    """Send an SMS message via Twilio."""
    if not settings.twilio_account_sid or not settings.twilio_auth_token:
        logger.warning("[SMS] Twilio not configured — skipping SMS to %s", to_phone)
        return False
    try:
        client = _get_twilio_client()
        msg = client.messages.create(
            body=message,
            from_=settings.twilio_from_number,
            to=to_phone,
        )
        logger.info("SMS sent to %s | SID=%s", to_phone, msg.sid)
        return True
    except Exception as exc:
        logger.error("Failed to send SMS to %s: %s", to_phone, exc)
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def send_otp_sms(self, to_phone: str, code: str) -> bool:
    """Send an OTP code via SMS."""
    message = (
        f"Karini - Votre code de vérification : {code}\n"
        "Valable 10 minutes. Ne le partagez jamais."
    )
    try:
        return send_sms.apply(args=[to_phone, message]).get()
    except Exception as exc:
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_booking_sms(
    self, to_phone: str, teacher_name: str, date_str: str, time_str: str
) -> bool:
    """Send a booking confirmation SMS."""
    message = (
        f"Karini : Session confirmée avec {teacher_name} "
        f"le {date_str} à {time_str}. Bon courage !"
    )
    try:
        return send_sms.apply(args=[to_phone, message]).get()
    except Exception as exc:
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_session_reminder_sms(
    self, to_phone: str, teacher_name: str, time_str: str
) -> bool:
    """Send a 30-minute reminder before a session starts."""
    message = (
        f"Karini : Rappel — votre session avec {teacher_name} commence dans 30 minutes ({time_str})."
    )
    try:
        return send_sms.apply(args=[to_phone, message]).get()
    except Exception as exc:
        raise self.retry(exc=exc)
