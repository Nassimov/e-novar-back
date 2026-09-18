from __future__ import annotations

"""
Finalizes scheduled account deletions once their 60-day grace period has
elapsed (see app/routers/account_security.py for where deletion_scheduled_for
gets set/cleared). Reuses the same proven mechanism as
app/routers/profile.py::reject_teacher — deleting the Supabase auth user
cascades (ON DELETE CASCADE) to profiles and everything that references it.
"""

import logging
from typing import Dict

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task
def task_process_scheduled_account_deletions() -> Dict[str, int]:
    from datetime import datetime

    from sqlmodel import Session, select

    from app.database import get_engine, get_supabase_service
    from app.models.profile import Profile

    deleted = 0
    failed = 0
    engine = get_engine()

    with Session(engine) as db:
        due = db.exec(
            select(Profile).where(
                Profile.deletion_scheduled_for.is_not(None),
                Profile.deletion_scheduled_for <= datetime.utcnow(),
            )
        ).all()

        for profile in due:
            try:
                get_supabase_service().auth.admin.delete_user(str(profile.id))
                deleted += 1
            except Exception:
                logger.exception("Scheduled account deletion failed for profile_id=%s", profile.id)
                failed += 1

    return {"deleted": deleted, "failed": failed}
