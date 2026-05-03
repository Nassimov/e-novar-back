from __future__ import annotations

"""
Celery tasks for push notifications and periodic automations.
All emails and push use OneSignal — no Resend, no direct DB calls in push tasks.
"""

import logging
from typing import Any, Dict, List, Optional

from app.services import onesignal
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


# ─── Push tasks ───────────────────────────────────────────────────────────────

@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def task_push(
    self,
    user_ids: List[str],
    title: str,
    body: str,
    data: Optional[Dict[str, Any]] = None,
) -> bool:
    """Send an async push notification to specific users via OneSignal."""
    try:
        result = onesignal.send_push(user_ids=user_ids, title=title, body=body, data=data)
        return result.get("status") != "error"
    except Exception as exc:
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def task_push_segment(
    self,
    segment: str,
    title: str,
    body: str,
    data: Optional[Dict[str, Any]] = None,
) -> bool:
    """Send an async push campaign to a OneSignal segment."""
    try:
        result = onesignal.send_push_to_segment(segment=segment, title=title, body=body, data=data)
        return result.get("status") != "error"
    except Exception as exc:
        raise self.retry(exc=exc)


# ─── Email campaign task ──────────────────────────────────────────────────────

@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def task_email_campaign(
    self,
    segment: str,
    subject: str,
    html: str,
) -> bool:
    """
    Send a marketing/automation email campaign to a OneSignal segment.
    segment: 'All' | 'students' | 'teachers' | 'parents'
    """
    try:
        result = onesignal.send_email_campaign(segment=segment, subject=subject, html=html)
        return result.get("status") != "error"
    except Exception as exc:
        raise self.retry(exc=exc)


# ─── Periodic automations ─────────────────────────────────────────────────────

@celery_app.task
def task_send_session_reminders() -> Dict[str, int]:
    """
    Daily at 09:00 → send push + email reminders for sessions scheduled tomorrow.
    Runs via Celery Beat (configured in celery_app.py).
    """
    from datetime import date, timedelta

    from sqlmodel import Session, select

    from app.database import engine
    from app.models.booking import TutoringSession
    from app.models.profile import Profile
    from app.services.notification import notify_session_reminder

    tomorrow = date.today() + timedelta(days=1)
    sent = 0

    with Session(engine) as db:
        sessions = db.exec(
            select(TutoringSession).where(
                TutoringSession.status == "scheduled",
                TutoringSession.scheduled_at >= tomorrow,
                TutoringSession.scheduled_at < tomorrow + timedelta(days=1),
            )
        ).all()

        for session in sessions:
            student = db.get(Profile, session.student_id)
            teacher = db.get(Profile, session.teacher_id)
            if student is None or teacher is None:
                continue
            notify_session_reminder(
                db,
                user_id=session.student_id,
                email=student.email,
                phone=getattr(student, "phone", None),
                name=student.first_name or "",
                teacher_name=teacher.full_name or "",
                date_str=str(session.scheduled_at.date()),
                time_str=session.scheduled_at.strftime("%H:%M"),
            )
            sent += 1

    logger.info("Session reminders sent: %d", sent)
    return {"sent": sent}


@celery_app.task
def task_send_inactivity_reminders() -> Dict[str, int]:
    """
    Weekly on Monday at 10:00 → push + email to students inactive for 7+ days.
    Uses OneSignal segment email for the email part (no individual calls).
    """
    from datetime import datetime, timedelta, timezone

    from sqlmodel import Session, select

    from app.database import engine
    from app.models.profile import Profile
    from app.config import get_settings

    settings = get_settings()
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    sent = 0

    with Session(engine) as db:
        inactive_students = db.exec(
            select(Profile).where(Profile.updated_at < cutoff)
        ).all()

        user_ids = [str(s.id) for s in inactive_students if s.id]
        if user_ids:
            # Push
            onesignal.send_push(
                user_ids=user_ids,
                title="Tu nous manques sur Enovar !",
                body="Reprends tes sessions et continue à progresser.",
                data={"action": "open_home"},
            )
            sent = len(user_ids)

    # Email campaign via OneSignal segment (all registered students)
    html = f"""
    <html>
    <body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#111;">
      <h1 style="color:#4F46E5;">Tu nous manques !</h1>
      <p>Cela fait un moment que tu n'as pas réservé de session sur Enovar.</p>
      <p>Tes tuteurs préférés sont disponibles et prêts à t'aider à progresser.</p>
      <p style="margin-top:32px;">
        <a href="{settings.frontend_url}/teachers"
           style="background:#4F46E5;color:#fff;padding:12px 28px;border-radius:8px;
                  text-decoration:none;font-weight:bold;">
          Reprendre mes sessions
        </a>
      </p>
      <p style="color:#6B7280;font-size:13px;margin-top:40px;">L'équipe Enovar</p>
    </body>
    </html>
    """
    onesignal.send_email_campaign(
        segment="students",
        subject="Tu nous manques sur Enovar 👋",
        html=html,
    )

    logger.info("Inactivity reminders sent: %d push + 1 email campaign (students segment)", sent)
    return {"sent": sent}
