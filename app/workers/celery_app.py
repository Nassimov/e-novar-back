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
    },
)
