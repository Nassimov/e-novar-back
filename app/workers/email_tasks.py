from __future__ import annotations

"""
Email tasks — all emails are sent via OneSignal (push + email centralized).
Resend is intentionally NOT used here; it is kept in config for medium-term migration.
"""

import logging
from typing import Any, Dict

from app.config import get_settings
from app.workers.celery_app import celery_app

settings = get_settings()
logger = logging.getLogger(__name__)


def _send(to: str, subject: str, html: str) -> bool:
    from app.services.onesignal import send_email
    result = send_email(to=to, subject=subject, html=html)
    if result.get("status") == "error":
        logger.error("OneSignal email failed to %s: %s", to, result.get("reason"))
        return False
    return True


# ─── Transactional emails ─────────────────────────────────────────────────────

@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_welcome_email(self, to: str, name: str) -> bool:
    html = f"""
    <html>
    <body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#111;">
      <h1 style="color:#4F46E5;">Bienvenue sur Enovar, {name} !</h1>
      <p>Nous sommes ravis de vous accueillir sur la plateforme de tutorat la plus complète d'Algérie.</p>
      <h2 style="color:#374151;">Ce que vous pouvez faire</h2>
      <ul>
        <li>Trouver le meilleur tuteur pour votre matière</li>
        <li>Réserver des sessions en ligne ou en présentiel</li>
        <li>Gagner des points KP en progressant</li>
        <li>Suivre vos devoirs et votre progression avec l'IA</li>
      </ul>
      <p style="margin-top:32px;">
        <a href="{settings.frontend_url}"
           style="background:#4F46E5;color:#fff;padding:12px 28px;border-radius:8px;
                  text-decoration:none;font-weight:bold;">
          Commencer maintenant
        </a>
      </p>
      <p style="color:#6B7280;font-size:13px;margin-top:40px;">L'équipe Enovar</p>
    </body>
    </html>
    """
    try:
        return _send(to, "Bienvenue sur Enovar !", html)
    except Exception as exc:
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_booking_confirmation(self, to: str, booking_data: Dict[str, Any]) -> bool:
    teacher_name = booking_data.get("teacher_name", "votre tuteur")
    date_str = booking_data.get("date", "")
    time_str = booking_data.get("slot_time", "")
    amount = booking_data.get("amount_dzd", 0)
    subject_name = booking_data.get("subject", "")

    html = f"""
    <html>
    <body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#111;">
      <h1 style="color:#4F46E5;">Session confirmée</h1>
      <p>Votre session avec <strong>{teacher_name}</strong> est confirmée.</p>
      <table style="width:100%;border-collapse:collapse;margin:24px 0;">
        <tr style="background:#F3F4F6;">
          <td style="padding:12px;border:1px solid #E5E7EB;"><strong>Matière</strong></td>
          <td style="padding:12px;border:1px solid #E5E7EB;">{subject_name}</td>
        </tr>
        <tr>
          <td style="padding:12px;border:1px solid #E5E7EB;"><strong>Date</strong></td>
          <td style="padding:12px;border:1px solid #E5E7EB;">{date_str}</td>
        </tr>
        <tr style="background:#F3F4F6;">
          <td style="padding:12px;border:1px solid #E5E7EB;"><strong>Heure</strong></td>
          <td style="padding:12px;border:1px solid #E5E7EB;">{time_str}</td>
        </tr>
        <tr>
          <td style="padding:12px;border:1px solid #E5E7EB;"><strong>Montant</strong></td>
          <td style="padding:12px;border:1px solid #E5E7EB;">{amount} DZD</td>
        </tr>
      </table>
      <p>
        <a href="{settings.frontend_url}/student/sessions"
           style="background:#4F46E5;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;">
          Voir mes sessions
        </a>
      </p>
      <p style="color:#6B7280;font-size:13px;margin-top:40px;">L'équipe Enovar</p>
    </body>
    </html>
    """
    try:
        return _send(to, "Confirmation de réservation — Enovar", html)
    except Exception as exc:
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def send_otp_email(self, to: str, code: str) -> bool:
    html = f"""
    <html>
    <body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#111;">
      <h1 style="color:#4F46E5;">Code de vérification</h1>
      <p>Utilisez ce code pour vérifier votre identité :</p>
      <div style="background:#F3F4F6;padding:32px;text-align:center;border-radius:12px;margin:24px 0;">
        <span style="font-size:48px;font-weight:bold;letter-spacing:10px;color:#4F46E5;">{code}</span>
      </div>
      <p style="color:#EF4444;font-weight:bold;">Ce code expire dans 10 minutes.</p>
      <p style="color:#6B7280;font-size:13px;">
        Si vous n'avez pas demandé ce code, ignorez cet email.
      </p>
    </body>
    </html>
    """
    try:
        return _send(to, "Votre code de vérification — Enovar", html)
    except Exception as exc:
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_password_reset_email(self, to: str, reset_link: str) -> bool:
    html = f"""
    <html>
    <body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#111;">
      <h1 style="color:#4F46E5;">Réinitialisation du mot de passe</h1>
      <p>Cliquez sur le bouton ci-dessous pour définir un nouveau mot de passe :</p>
      <p style="margin:32px 0;">
        <a href="{reset_link}"
           style="background:#4F46E5;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;">
          Réinitialiser le mot de passe
        </a>
      </p>
      <p style="color:#EF4444;">Ce lien expire dans 1 heure.</p>
      <p style="color:#6B7280;font-size:13px;">
        Si vous n'avez pas fait cette demande, ignorez cet email.
      </p>
    </body>
    </html>
    """
    try:
        return _send(to, "Réinitialisation de mot de passe — Enovar", html)
    except Exception as exc:
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_session_reminder_email(
    self, to: str, name: str, teacher_name: str, date_str: str, time_str: str
) -> bool:
    html = f"""
    <html>
    <body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#111;">
      <h1 style="color:#4F46E5;">Rappel de session</h1>
      <p>Bonjour {name},</p>
      <p>Votre session avec <strong>{teacher_name}</strong> est prévue demain :</p>
      <div style="background:#F3F4F6;padding:20px;border-radius:8px;margin:20px 0;">
        <p style="margin:0;font-size:18px;"><strong>{date_str} à {time_str}</strong></p>
      </div>
      <p>
        <a href="{settings.frontend_url}/student/sessions"
           style="background:#4F46E5;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;">
          Voir les détails
        </a>
      </p>
      <p style="color:#6B7280;font-size:13px;margin-top:40px;">L'équipe Enovar</p>
    </body>
    </html>
    """
    try:
        return _send(to, f"Rappel de session demain — {teacher_name}", html)
    except Exception as exc:
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_withdrawal_processed_email(self, to: str, name: str, amount: int, status: str) -> bool:
    status_label = "approuvée" if status == "approved" else "refusée"
    color = "#10B981" if status == "approved" else "#EF4444"
    html = f"""
    <html>
    <body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#111;">
      <h1 style="color:{color};">Retrait {status_label}</h1>
      <p>Bonjour {name},</p>
      <p>Votre demande de retrait de <strong>{amount} DZD</strong> a été <strong>{status_label}</strong>.</p>
      <p>
        <a href="{settings.frontend_url}/teacher/wallet"
           style="background:#4F46E5;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;">
          Voir mon portefeuille
        </a>
      </p>
      <p style="color:#6B7280;font-size:13px;margin-top:40px;">L'équipe Enovar</p>
    </body>
    </html>
    """
    try:
        return _send(to, f"Demande de retrait {status_label} — Enovar", html)
    except Exception as exc:
        raise self.retry(exc=exc)
