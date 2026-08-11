from __future__ import annotations

"""
LiveOps Scheduler — Competitive Arena Phase 15. Background tasks for the
Mission Engine's automatic resets and the Event Manager's activate/end
sweep + reminder system (see beat_schedule in app/workers/celery_app.py).
"""

import logging
from typing import Any, Dict

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


def _with_db():
    from sqlmodel import Session
    from app.database import get_engine
    return Session(get_engine())


@celery_app.task
def task_reset_daily_missions() -> Dict[str, int]:
    from app.services.competitive import mission_service
    with _with_db() as db:
        assigned = mission_service.reset_period_for_all(db, "daily")
    return {"assigned": assigned}


@celery_app.task
def task_reset_weekly_missions() -> Dict[str, int]:
    from app.services.competitive import mission_service
    with _with_db() as db:
        assigned = mission_service.reset_period_for_all(db, "weekly")
    return {"assigned": assigned}


@celery_app.task
def task_reset_monthly_missions() -> Dict[str, int]:
    from app.services.competitive import mission_service
    with _with_db() as db:
        assigned = mission_service.reset_period_for_all(db, "monthly")
    return {"assigned": assigned}


@celery_app.task
def task_liveops_activate_events() -> Dict[str, int]:
    from app.services.competitive import event_service
    with _with_db() as db:
        activated = event_service.activate_due_events(db)
    return {"activated": activated}


@celery_app.task
def task_liveops_end_events() -> Dict[str, int]:
    from app.services.competitive import event_service
    with _with_db() as db:
        ended = event_service.end_expired_events(db)
    return {"ended": ended}


@celery_app.task
def task_liveops_notify_events_ending_soon() -> Dict[str, int]:
    from app.services.competitive import event_service
    with _with_db() as db:
        sent = event_service.notify_events_ending_soon(db)
    return {"sent": sent}


@celery_app.task
def task_liveops_notify_happy_hour_starting_soon() -> Dict[str, int]:
    from app.services.competitive import event_service
    with _with_db() as db:
        sent = event_service.notify_happy_hour_starting_soon(db)
    return {"sent": sent}


@celery_app.task
def task_liveops_notify_login_streak_expiring() -> Dict[str, int]:
    from app.services.competitive import login_service
    with _with_db() as db:
        sent = login_service.notify_streak_expiring(db)
    return {"sent": sent}


@celery_app.task
def task_liveops_notify_missions_almost_done() -> Dict[str, int]:
    from app.services.competitive import mission_service
    with _with_db() as db:
        sent = mission_service.notify_missions_almost_done(db)
    return {"sent": sent}


@celery_app.task
def task_liveops_notify_missions_expire_tonight() -> Dict[str, int]:
    from app.services.competitive import mission_service
    with _with_db() as db:
        sent = mission_service.notify_missions_expire_tonight(db)
    return {"sent": sent}
