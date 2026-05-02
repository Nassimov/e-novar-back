from __future__ import annotations

import logging

from app.config import get_settings
from app.workers.celery_app import celery_app

settings = get_settings()
logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_sms(self, to_phone: str, message: str) -> bool:
    """Send an SMS message.

    Currently logs the message. Wire in Twilio or another provider
    by installing twilio and using:
        from twilio.rest import Client
        client = Client(account_sid, auth_token)
        client.messages.create(body=message, from_=from_number, to=to_phone)
    """
    if not settings.sms_provider_api_key:
        logger.info("[SMS STUB] To: %s | Message: %s", to_phone, message)
        return True

    # Twilio integration placeholder
    try:
        # from twilio.rest import Client
        # client = Client(settings.sms_provider_api_key, settings.sms_provider_secret)
        # msg = client.messages.create(
        #     body=message,
        #     from_=settings.sms_provider_from,
        #     to=to_phone,
        # )
        # logger.info("SMS sent to %s: SID=%s", to_phone, msg.sid)
        logger.info("[SMS] Would send to %s: %s", to_phone, message)
        return True
    except Exception as exc:
        logger.error("Failed to send SMS to %s: %s", to_phone, exc)
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def send_otp_sms(self, to_phone: str, code: str) -> bool:
    """Send an OTP code via SMS."""
    message = f"Votre code de vérification Karini : {code}\nValable 10 minutes. Ne le partagez pas."
    return send_sms.apply(args=[to_phone, message]).get()


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_booking_sms(self, to_phone: str, teacher_name: str, date_str: str, time_str: str) -> bool:
    """Send a booking confirmation SMS."""
    message = (
        f"Karini: Votre session avec {teacher_name} est confirmée pour le {date_str} à {time_str}. "
        f"Bon courage !"
    )
    return send_sms.apply(args=[to_phone, message]).get()
