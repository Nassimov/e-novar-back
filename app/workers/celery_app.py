from __future__ import annotations

from celery import Celery

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "karini",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "app.workers.email_tasks",
        "app.workers.sms_tasks",
        "app.workers.pdf_tasks",
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
    },
)
