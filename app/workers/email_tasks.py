²from __future__ import annotations

import logging
from typing import Any, Dict

import httpx

from app.config import get_settings
from app.workers.celery_app import celery_app

settings = get_settings()
logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"


def _send_via_resend(to: str, subject: str, html: str) -> bool:
    """Send an email using the Resend API."""
    if not settings.resend_api_key:
        logger.warning("RESEND_API_KEY not set, skipping email to %s", to)
        return False

    headers = {
        "Authorization": f"Bearer {settings.resend_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "from": settings.email_from,
        "to": [to],
        "subject": subject,
        "html": html,
    }
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(RESEND_API_URL, json=payload, headers=headers)
            response.raise_for_status()
            return True
    except Exception as exc:
        logger.error("Failed to send email to %s: %s", to, exc)
        return False


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_welcome_email(self, to: str, name: str) -> bool:
    """Send a welcome email to a newly registered user."""
    subject = "Bienvenue sur Karini ! 🎓"
    html = f"""
    <html>
    <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
      <h1 style="color: #4F46E5;">Bienvenue sur Karini, {name} !</h1>
      <p>Nous sommes ravis de vous accueillir sur la plateforme de tutorat la plus complète d'Algérie.</p>
      <h2>Que pouvez-vous faire ?</h2>
      <ul>
        <li>Trouver le meilleur tuteur pour votre matière</li>
        <li>Réserver des sessions en ligne ou en présentiel</li>
        <li>Gagner des points KP en progressant</li>
        <li>Suivre vos devoirs et votre progression</li>
      </ul>
      <p>
        <a href="{settings.frontend_url}"
           style="background:#4F46E5;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;">
          Commencer maintenant
        </a>
      </p>
      <p style="color: #6B7280; font-size: 14px;">L'équipe Karini</p>
    </body>
    </html>
    """
    try:
        return _send_via_resend(to, subject, html)
    except Exception as exc:
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_booking_confirmation(self, to: str, booking_data: Dict[str, Any]) -> bool:
    """Send a booking confirmation email."""
    subject = "Confirmation de votre réservation - Karini"
    teacher_name = booking_data.get("teacher_name", "votre tuteur")
    date_str = booking_data.get("date", "")
    time_str = booking_data.get("slot_time", "")
    amount = booking_data.get("amount_dzd", 0)
    subject_name = booking_data.get("subject", "")

    html = f"""
    <html>
    <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
      <h1 style="color: #4F46E5;">Réservation confirmée ✅</h1>
      <p>Votre session avec <strong>{teacher_name}</strong> est confirmée.</p>
      <table style="width:100%; border-collapse: collapse; margin: 20px 0;">
        <tr style="background: #F3F4F6;">
          <td style="padding: 12px; border: 1px solid #E5E7EB;"><strong>Matière</strong></td>
          <td style="padding: 12px; border: 1px solid #E5E7EB;">{subject_name}</td>
        </tr>
        <tr>
          <td style="padding: 12px; border: 1px solid #E5E7EB;"><strong>Date</strong></td>
          <td style="padding: 12px; border: 1px solid #E5E7EB;">{date_str}</td>
        </tr>
        <tr style="background: #F3F4F6;">
          <td style="padding: 12px; border: 1px solid #E5E7EB;"><strong>Heure</strong></td>
          <td style="padding: 12px; border: 1px solid #E5E7EB;">{time_str}</td>
        </tr>
        <tr>
          <td style="padding: 12px; border: 1px solid #E5E7EB;"><strong>Montant</strong></td>
          <td style="padding: 12px; border: 1px solid #E5E7EB;">{amount} DZD</td>
        </tr>
      </table>
      <p>
        <a href="{settings.frontend_url}/sessions"
           style="background:#4F46E5;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;">
          Voir mes sessions
        </a>
      </p>
      <p style="color: #6B7280; font-size: 14px;">L'équipe Karini</p>
    </body>
    </html>
    """
    try:
        return _send_via_resend(to, subject, html)
    except Exception as exc:
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def send_otp_email(self, to: str, code: str) -> bool:
    """Send an OTP verification code email."""
    subject = "Votre code de vérification - Karini"
    html = f"""
    <html>
    <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
      <h1 style="color: #4F46E5;">Code de vérification</h1>
      <p>Utilisez ce code pour vérifier votre identité :</p>
      <div style="background: #F3F4F6; padding: 24px; text-align: center;
                  border-radius: 8px; margin: 20px 0;">
        <span style="font-size: 48px; font-weight: bold; letter-spacing: 8px; color: #4F46E5;">
          {code}
        </span>
      </div>
      <p style="color: #EF4444;">Ce code expire dans 10 minutes.</p>
      <p style="color: #6B7280; font-size: 14px;">
        Si vous n'avez pas demandé ce code, ignorez cet email.
      </p>
    </body>
    </html>
    """
    try:
        return _send_via_resend(to, subject, html)
    except Exception as exc:
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_password_reset_email(self, to: str, reset_link: str) -> bool:
    """Send a password reset email."""
    subject = "Réinitialisation de mot de passe - Karini"
    html = f"""
    <html>
    <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
      <h1 style="color: #4F46E5;">Réinitialisation du mot de passe</h1>
      <p>Cliquez sur le bouton ci-dessous pour réinitialiser votre mot de passe :</p>
      <p style="margin: 30px 0;">
        <a href="{reset_link}"
           style="background:#4F46E5;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;">
          Réinitialiser le mot de passe
        </a>
      </p>
      <p style="color: #EF4444;">Ce lien expire dans 1 heure.</p>
      <p style="color: #6B7280; font-size: 14px;">
        Si vous n'avez pas fait cette demande, ignorez cet email.
      </p>
    </body>
    </html>
    """
    try:
        return _send_via_resend(to, subject, html)
    except Exception as exc:
        raise self.retry(exc=exc)
