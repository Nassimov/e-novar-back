from __future__ import annotations

"""
Celery tasks for push notifications and periodic automations.
All emails and push use OneSignal — no Resend, no direct DB calls in push tasks.
"""

import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

from app.services import onesignal
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

_MAX_QUEUE_ATTEMPTS = 5


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


# ─── Notification engine: async delivery queue ───────────────────────────────

def _deliver_queue_row(db, row: Dict[str, Any]) -> Optional[str]:
    """Attempt one channel delivery for one notification_queue row.
    Returns None on success, or an error string on failure."""
    ctx = row["context"] or {}
    channel = ctx.get("channel")
    title = ctx.get("title", "")
    body = ctx.get("body", "")
    data = ctx.get("data") or {}
    deep_link = ctx.get("deep_link")

    try:
        if channel == "push":
            result = onesignal.send_push(user_ids=[str(row["user_id"])], title=title, body=body, data=data)
            if result.get("status") == "error":
                return result.get("reason", "onesignal push error")
            return None

        if channel == "email":
            from app.models.profile import Profile
            from app.workers.email_tasks import send_generic_notification_email

            profile = db.get(Profile, row["user_id"])
            if not profile or not profile.email:
                return "no email on file"
            ok = send_generic_notification_email.run(profile.email, title, body, deep_link)
            return None if ok else "email send failed"

        return f"unknown channel: {channel}"
    except Exception as exc:
        logger.exception("notification delivery failed channel=%s user=%s", channel, row["user_id"])
        return str(exc)


@celery_app.task(bind=True, max_retries=1)
def task_process_notification_queue(self, batch_size: int = 200) -> Dict[str, int]:
    """
    Claims up to `batch_size` pending/retry-due rows from notification_queue
    (FOR UPDATE SKIP LOCKED so multiple workers never double-process the same
    row), attempts delivery, and logs every attempt to
    notification_delivery_logs. Failures get exponential backoff up to
    _MAX_QUEUE_ATTEMPTS, after which they're recorded in
    notification_failures for admin visibility and marked 'failed' (no
    further retries).

    Triggered on-demand by notification_engine.emit() and as a safety-net by
    Celery Beat (see celery_app.py) in case a .delay() call is ever lost.
    """
    from sqlalchemy import text
    from sqlmodel import Session

    from app.database import engine
    from app.models.notification import NotificationDeliveryLog, NotificationFailure

    processed = 0
    failed = 0

    with Session(engine) as db:
        rows = db.execute(
            text(
                """
                UPDATE notification_queue
                SET status = 'processing'
                WHERE id IN (
                    SELECT id FROM notification_queue
                    WHERE status IN ('pending', 'failed') AND next_attempt_at <= now()
                    ORDER BY created_at
                    LIMIT :limit
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING id, event_type, user_id, context, attempts
                """
            ),
            {"limit": batch_size},
        ).mappings().all()

        for row in rows:
            error = _deliver_queue_row(db, row)
            notification_id = (row["context"] or {}).get("notification_id")
            channel = (row["context"] or {}).get("channel", "unknown")

            db.add(NotificationDeliveryLog(
                notification_id=UUID(notification_id) if notification_id else None,
                user_id=row["user_id"],
                channel=channel,
                status="failed" if error else "success",
                error=error,
            ))

            if error is None:
                db.execute(
                    text("UPDATE notification_queue SET status='done', processed_at=now() WHERE id=:id"),
                    {"id": row["id"]},
                )
                processed += 1
            else:
                attempts = row["attempts"] + 1
                failed += 1
                if attempts >= _MAX_QUEUE_ATTEMPTS:
                    db.execute(
                        text("UPDATE notification_queue SET status='failed', attempts=:a, processed_at=now() WHERE id=:id"),
                        {"a": attempts, "id": row["id"]},
                    )
                    db.add(NotificationFailure(
                        queue_id=row["id"],
                        notification_id=UUID(notification_id) if notification_id else None,
                        user_id=row["user_id"],
                        channel=channel,
                        error=error,
                        retry_count=attempts,
                    ))
                else:
                    backoff_minutes = 2 ** attempts
                    db.execute(
                        text(
                            "UPDATE notification_queue SET status='pending', attempts=:a, "
                            "next_attempt_at = now() + (:m * interval '1 minute') WHERE id=:id"
                        ),
                        {"a": attempts, "m": backoff_minutes, "id": row["id"]},
                    )
            db.commit()

    if processed or failed:
        logger.info("notification queue processed=%d failed=%d", processed, failed)
    return {"processed": processed, "failed": failed}


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


@celery_app.task
def task_send_weekly_summary() -> Dict[str, int]:
    """
    Sunday 18:00 → per-student weekly recap email (sessions completed this
    week, EP earned this week, EP balance, current weekly KP leaderboard
    rank if any). Only sent to students with at least one completed session
    or EP earned this week — an empty recap isn't worth an email.
    """
    from datetime import datetime, timedelta, timezone as _tz

    from sqlmodel import Session, func, select

    from app.database import engine
    from app.models.booking import TutoringSession
    from app.models.kp import KpBalance
    from app.models.profile import Profile, StudentProfile
    from app.models.progress import LeaderboardRankSnapshot

    week_ago = datetime.now(_tz.utc) - timedelta(days=7)
    sent = 0

    with Session(engine) as db:
        students = db.exec(select(StudentProfile)).all()
        for sp in students:
            profile = db.get(Profile, sp.user_id)
            balance = db.get(KpBalance, sp.user_id)
            if not profile or not profile.email or not balance:
                continue
            if balance.week_earned <= 0:
                continue

            sessions_completed = db.exec(
                select(func.count()).select_from(TutoringSession).where(
                    TutoringSession.student_id == sp.user_id,
                    TutoringSession.status == "completed",
                    TutoringSession.scheduled_at >= week_ago,
                )
            ).one()

            snapshot = db.exec(
                select(LeaderboardRankSnapshot).where(
                    LeaderboardRankSnapshot.user_id == sp.user_id,
                    LeaderboardRankSnapshot.period_type == "weekly",
                    LeaderboardRankSnapshot.audience == "students",
                    LeaderboardRankSnapshot.sort_by == "kp",
                )
            ).first()

            from app.workers.email_tasks import send_weekly_summary_email
            send_weekly_summary_email.delay(
                profile.email,
                profile.first_name or "",
                sessions_completed,
                balance.week_earned,
                balance.balance,
                snapshot.rank if snapshot else None,
            )
            sent += 1

    logger.info("Weekly summary emails queued: %d", sent)
    return {"sent": sent}
