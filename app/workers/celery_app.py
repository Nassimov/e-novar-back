from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "enovar",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "app.workers.email_tasks",
        "app.workers.sms_tasks",
        "app.workers.pdf_tasks",
        "app.workers.notification_tasks",
        "app.workers.booking_tasks",
        "app.workers.campaign_tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Africa/Algiers",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    result_expires=3600,
    task_routes={
        "app.workers.email_tasks.*": {"queue": "email"},
        "app.workers.sms_tasks.*": {"queue": "sms"},
        "app.workers.pdf_tasks.*": {"queue": "pdf"},
        "app.workers.notification_tasks.*": {"queue": "notifications"},
        "app.workers.booking_tasks.*": {"queue": "notifications"},
        "app.workers.campaign_tasks.*": {"queue": "notifications"},
    },
    beat_schedule={
        # Every day at 09:00 Algiers time — send reminders for tomorrow's sessions
        "session-reminders-daily": {
            "task": "app.workers.notification_tasks.task_send_session_reminders",
            "schedule": crontab(hour=9, minute=0),
        },
        # Every Monday at 10:00 — re-engage inactive students
        "inactivity-reminders-weekly": {
            "task": "app.workers.notification_tasks.task_send_inactivity_reminders",
            "schedule": crontab(hour=10, minute=0, day_of_week=1),
        },
        # Every 15 min — auto-cancel bookings the teacher never answered in
        # time (see app/workers/booking_tasks.py for the full rule).
        "booking-auto-cancel-unanswered": {
            "task": "app.workers.booking_tasks.task_auto_cancel_unanswered_bookings",
            "schedule": crontab(minute="*/15"),
        },
        # Every 15 min — lift auto-suspensions whose timer has expired.
        "teacher-suspension-auto-reinstate": {
            "task": "app.workers.booking_tasks.task_reinstate_expired_teacher_suspensions",
            "schedule": crontab(minute="*/15"),
        },
        # Every 5 min — online sessions have a short grace window (default
        # 20 min), so this needs tighter polling than the other two.
        "online-teacher-no-show-detection": {
            "task": "app.workers.booking_tasks.task_detect_online_teacher_no_show",
            "schedule": crontab(minute="*/5"),
        },
        "online-student-no-show-detection": {
            "task": "app.workers.booking_tasks.task_detect_online_student_no_show",
            "schedule": crontab(minute="*/5"),
        },
        # Every 30 min — resolve in-person absence reports the other party
        # never countered within the configured window.
        "in-person-dispute-auto-resolve": {
            "task": "app.workers.booking_tasks.task_auto_resolve_disputes",
            "schedule": crontab(minute="*/30"),
        },
        # Every 30 min — expire cash/transfer/RIB bookings an admin never
        # confirmed/rejected in time.
        "manual-payment-expiry": {
            "task": "app.workers.booking_tasks.task_expire_unconfirmed_manual_payments",
            "schedule": crontab(minute="*/30"),
        },
        # Every 2 min — safety net for the notification delivery queue: the
        # normal path is notification_engine.emit() triggering .delay()
        # on-demand, this just catches anything that slipped through (worker
        # restart, lost broker message, backoff retry becoming due).
        "notification-queue-safety-net": {
            "task": "app.workers.notification_tasks.task_process_notification_queue",
            "schedule": crontab(minute="*/2"),
        },
        # Sunday 18:00 — weekly recap email (sessions done, EP earned, rank).
        "weekly-summary-sunday": {
            "task": "app.workers.notification_tasks.task_send_weekly_summary",
            "schedule": crontab(hour=18, minute=0, day_of_week=0),
        },
        # Every 5 min — fire due scheduled/recurring admin campaigns.
        "notification-campaigns-due": {
            "task": "app.workers.campaign_tasks.task_dispatch_due_campaigns",
            "schedule": crontab(minute="*/5"),
        },
    },
)
