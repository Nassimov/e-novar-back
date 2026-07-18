from __future__ import annotations

"""
Email tasks — all emails are sent via OneSignal (push + email centralized).
Resend is intentionally NOT used here; it is kept in config for medium-term migration.
"""

import logging
from typing import Any, Dict, Optional

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
def send_store_redeem_email(
    self,
    to: str,
    name: str,
    item_name: str,
    item_category: str,
    effect_type: str,
    is_auto: bool,
    cost: int,
) -> bool:
    """Confirmation email after an EP marketplace exchange."""
    if is_auto:
        status_block = f"""
        <div style="background:#ECFDF5;border-left:4px solid #10B981;padding:16px;border-radius:6px;margin:24px 0;">
          <p style="margin:0;font-weight:bold;color:#065F46;">✅ Avantage activé immédiatement</p>
          <p style="margin:8px 0 0;color:#065F46;">Votre accès est disponible dès maintenant dans votre espace.</p>
        </div>"""
        cta_url = f"{settings.frontend_url}/student/store"
        cta_label = "Voir mes avantages actifs"
    else:
        delay = "72h" if item_category == "travel" else "48h"
        status_block = f"""
        <div style="background:#FFF7ED;border-left:4px solid #F59E0B;padding:16px;border-radius:6px;margin:24px 0;">
          <p style="margin:0;font-weight:bold;color:#92400E;">⏳ Demande enregistrée</p>
          <p style="margin:8px 0 0;color:#92400E;">L'équipe E-NOVAR vous contactera sous {delay} pour finaliser votre commande.</p>
        </div>"""
        cta_url = f"{settings.frontend_url}/student/store"
        cta_label = "Voir mes échanges"

    html = f"""
    <html>
    <body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#111;">
      <h1 style="color:#4F46E5;">Échange EP confirmé 🎁</h1>
      <p>Bonjour {name},</p>
      <p>Votre échange EP a bien été enregistré :</p>
      <table style="width:100%;border-collapse:collapse;margin:20px 0;">
        <tr style="background:#F3F4F6;">
          <td style="padding:12px;border:1px solid #E5E7EB;"><strong>Article</strong></td>
          <td style="padding:12px;border:1px solid #E5E7EB;">{item_name}</td>
        </tr>
        <tr>
          <td style="padding:12px;border:1px solid #E5E7EB;"><strong>Coût</strong></td>
          <td style="padding:12px;border:1px solid #E5E7EB;">{cost:,} EP</td>
        </tr>
      </table>
      {status_block}
      <p>
        <a href="{cta_url}"
           style="background:#4F46E5;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:bold;">
          {cta_label}
        </a>
      </p>
      <p style="color:#6B7280;font-size:13px;margin-top:40px;">L'équipe E-NOVAR</p>
    </body>
    </html>
    """
    try:
        return _send(to, f"Échange EP confirmé : {item_name}", html)
    except Exception as exc:
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_teacher_approved_email(self, to: str, name: str) -> bool:
    """Sent when an admin approves a teacher's profile."""
    dashboard_url = f"{settings.frontend_url}/teacher"
    html = f"""
    <html>
    <body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#111;">
      <h1 style="color:#4F46E5;">🎉 Félicitations, {name} !</h1>
      <p>Votre profil enseignant <strong>E-NOVAR</strong> a été <strong>validé</strong> par notre équipe.</p>
      <div style="background:#ECFDF5;border-left:4px solid #10B981;padding:16px;border-radius:6px;margin:24px 0;">
        <p style="margin:0;font-weight:bold;color:#065F46;">✅ Votre compte est maintenant actif</p>
        <p style="margin:8px 0 0;color:#065F46;">
          Configurez vos disponibilités et commencez à recevoir des réservations d'élèves.
        </p>
      </div>
      <p>En cadeau de bienvenue, <strong>50 EP</strong> ont été crédités sur votre compte.</p>
      <p style="margin-top:28px;">
        <a href="{dashboard_url}"
           style="background:#4F46E5;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:bold;">
          Accéder à mon espace enseignant
        </a>
      </p>
      <p style="color:#6B7280;font-size:13px;margin-top:40px;">L'équipe E-NOVAR</p>
    </body>
    </html>
    """
    try:
        return _send(to, "✅ Votre profil enseignant a été validé — E-NOVAR", html)
    except Exception as exc:
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_teacher_rejected_email(self, to: str, name: str, reason: str) -> bool:
    """Sent when an admin rejects a teacher's profile."""
    html = f"""
    <html>
    <body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#111;">
      <h1 style="color:#4F46E5;">E-NOVAR — Candidature enseignant</h1>
      <p>Bonjour {name},</p>
      <p>Après examen de votre dossier, nous ne sommes pas en mesure d'approuver votre candidature.</p>
      <div style="background:#FEF2F2;border-left:4px solid #EF4444;padding:16px;border-radius:6px;margin:24px 0;">
        <p style="margin:0;font-weight:bold;color:#991B1B;">Motif du refus</p>
        <p style="margin:8px 0 0;color:#991B1B;">{reason}</p>
      </div>
      <p>
        Si vous souhaitez soumettre une nouvelle candidature avec des documents mis à jour,
        vous pouvez créer un nouveau compte sur E-NOVAR.
      </p>
      <p style="color:#6B7280;font-size:13px;margin-top:40px;">
        Cordialement,<br/>L'équipe E-NOVAR
      </p>
    </body>
    </html>
    """
    try:
        return _send(to, "Candidature enseignant E-NOVAR", html)
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


# ─── Notification engine emails ───────────────────────────────────────────────
# Shared branded wrapper for the generic/catalogue-driven emails below (the
# bespoke templates above keep their own inline markup untouched).

def _brand_wrap(preheader: str, body_html: str, cta_label: Optional[str] = None, cta_url: Optional[str] = None) -> str:
    cta_html = ""
    if cta_label and cta_url:
        cta_html = f"""
        <tr><td style="padding:28px 40px 8px;">
          <a href="{cta_url}"
             style="display:inline-block;background:linear-gradient(135deg,#4F46E5,#7C3AED);color:#fff;
                    padding:13px 30px;border-radius:10px;text-decoration:none;font-weight:700;
                    font-size:14px;letter-spacing:.02em;">
            {cta_label}
          </a>
        </td></tr>
        """
    return f"""
    <html>
    <body style="margin:0;padding:0;background:#F3F4F6;font-family:Arial,Helvetica,sans-serif;">
      <span style="display:none;max-height:0;overflow:hidden;">{preheader}</span>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F3F4F6;padding:32px 12px;">
        <tr><td align="center">
          <table role="presentation" width="600" cellpadding="0" cellspacing="0"
                 style="background:#fff;border-radius:16px;overflow:hidden;max-width:600px;width:100%;">
            <tr><td style="background:linear-gradient(135deg,#000666,#4F46E5);padding:24px 40px;">
              <span style="color:#fff;font-size:20px;font-weight:800;letter-spacing:.02em;">E-NOVAR</span>
            </td></tr>
            <tr><td style="padding:32px 40px 8px;color:#111827;font-size:15px;line-height:1.6;">
              {body_html}
            </td></tr>
            {cta_html}
            <tr><td style="padding:28px 40px 32px;color:#9CA3AF;font-size:12px;border-top:1px solid #F3F4F6;margin-top:20px;">
              L'équipe E-NOVAR — la plateforme algérienne de cours particuliers.<br/>
              Gérez vos préférences de notification depuis votre profil.
            </td></tr>
          </table>
        </td></tr>
      </table>
    </body>
    </html>
    """


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_generic_notification_email(self, to: str, title: str, body: str, deep_link: Optional[str] = None) -> bool:
    """Fallback branded email for any notification_templates event that
    doesn't have a bespoke template above — used by
    app/services/notification_engine.py's async delivery queue."""
    url = f"{settings.frontend_url}{deep_link}" if deep_link and deep_link.startswith("/") else (deep_link or settings.frontend_url)
    html = _brand_wrap(
        preheader=body[:120],
        body_html=f"<h2 style='margin:0 0 12px;font-size:19px;color:#111827;'>{title}</h2><p style='margin:0;color:#374151;'>{body}</p>",
        cta_label="Voir sur Enovar",
        cta_url=url,
    )
    try:
        return _send(to, title, html)
    except Exception as exc:
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_weekly_summary_email(
    self,
    to: str,
    name: str,
    sessions_completed: int,
    ep_earned: int,
    ep_balance: int,
    rank: Optional[int] = None,
) -> bool:
    """Weekly recap — sent by task_send_weekly_summary (celery beat, Sunday evenings)."""
    rank_html = f"<li>Classement actuel : <strong>#{rank}</strong></li>" if rank else ""
    html = _brand_wrap(
        preheader=f"{sessions_completed} séance(s) cette semaine, +{ep_earned} EP.",
        body_html=f"""
          <h2 style="margin:0 0 12px;font-size:19px;">Ton récap de la semaine, {name} !</h2>
          <ul style="margin:0;padding-left:18px;color:#374151;">
            <li>Séances terminées : <strong>{sessions_completed}</strong></li>
            <li>EP gagnés cette semaine : <strong>+{ep_earned}</strong></li>
            <li>Solde EP total : <strong>{ep_balance}</strong></li>
            {rank_html}
          </ul>
        """,
        cta_label="Continuer ma progression",
        cta_url=settings.frontend_url,
    )
    try:
        return _send(to, "Ton récap de la semaine sur Enovar", html)
    except Exception as exc:
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_admin_announcement_email(self, to: str, title: str, body: str, deep_link: Optional[str] = None) -> bool:
    """Admin-authored announcement/campaign email (see
    app/routers/admin/notification_campaigns.py)."""
    url = f"{settings.frontend_url}{deep_link}" if deep_link and deep_link.startswith("/") else (deep_link or settings.frontend_url)
    html = _brand_wrap(
        preheader=body[:120],
        body_html=f"<h2 style='margin:0 0 12px;font-size:19px;'>{title}</h2><p style='margin:0;color:#374151;white-space:pre-line;'>{body}</p>",
        cta_label="Découvrir",
        cta_url=url,
    )
    try:
        return _send(to, title, html)
    except Exception as exc:
        raise self.retry(exc=exc)
