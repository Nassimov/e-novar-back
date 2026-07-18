from __future__ import annotations

"""
Celery tasks that actually send notification_campaigns — see
app/routers/admin/notification_campaigns.py for the admin-facing API and
app/models/notification.py for NotificationCampaign/NotificationCampaignTarget.

A campaign send persists one in_app Notification per recipient (deduped via
`campaign:{id}` so re-running a send never double-notifies) and queues
push/email exactly like app/services/notification_engine.py's emit() does —
same NotificationQueue table, same task_process_notification_queue consumer.
Kept in its own module (not notification_engine.py / notification_tasks.py)
since campaigns don't go through the template catalogue.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from uuid import UUID

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


def _dispatch_campaign_notification(db, campaign, user_id: UUID) -> str:
    """Persists the in_app row + queues other channels for one recipient.
    Returns 'sent' (new) or 'skipped' (already notified — dedup hit)."""
    from app.models.notification import Notification, NotificationQueue
    from app.services.notification_engine import channel_allowed

    notif = Notification(
        user_id=user_id,
        type=f"campaign:{campaign.id}",
        category=campaign.category,
        priority=campaign.priority,
        title=campaign.title,
        body=campaign.body,
        deep_link=campaign.deep_link,
        data={"campaign_id": str(campaign.id)},
        dedup_key=f"campaign:{campaign.id}",
        channel="in_app",
    )
    db.add(notif)
    try:
        db.commit()
    except Exception:
        db.rollback()
        return "skipped"
    db.refresh(notif)

    for channel in campaign.channels:
        if channel == "in_app":
            continue
        if not channel_allowed(db, user_id=user_id, category=campaign.category, channel=channel, priority=campaign.priority):
            continue
        db.add(NotificationQueue(
            event_type=f"campaign:{campaign.id}",
            user_id=user_id,
            context={
                "notification_id": str(notif.id), "channel": channel,
                "title": campaign.title, "body": campaign.body, "deep_link": campaign.deep_link,
            },
            dedup_key=f"campaign:{campaign.id}:{channel}",
        ))
    db.commit()
    return "sent"


@celery_app.task(bind=True, max_retries=1)
def task_send_campaign(self, campaign_id: str) -> Dict[str, Any]:
    from sqlmodel import Session, select

    from app.database import engine
    from app.models.notification import NotificationCampaign, NotificationCampaignTarget
    from app.routers.admin.notification_campaigns import resolve_campaign_targets
    from app.workers.notification_tasks import task_process_notification_queue

    with Session(engine) as db:
        campaign = db.get(NotificationCampaign, UUID(campaign_id))
        if campaign is None:
            return {"error": "campaign not found"}
        if campaign.status not in ("sending", "scheduled", "draft"):
            return {"error": f"campaign status '{campaign.status}' is not sendable"}

        campaign.status = "sending"
        db.add(campaign)
        db.commit()

        user_ids = resolve_campaign_targets(db, campaign.target_type, campaign.target_filters or {})
        campaign.recipients_count = len(user_ids)
        db.add(campaign)
        db.commit()

        sent = failed = 0
        for uid in user_ids:
            existing = db.exec(
                select(NotificationCampaignTarget).where(
                    NotificationCampaignTarget.campaign_id == campaign.id,
                    NotificationCampaignTarget.user_id == uid,
                )
            ).first()
            if existing and existing.status == "sent":
                sent += 1
                continue

            result = _dispatch_campaign_notification(db, campaign, uid)
            now = datetime.now(timezone.utc)
            if existing is None:
                existing = NotificationCampaignTarget(campaign_id=campaign.id, user_id=uid)
            existing.status = "sent" if result in ("sent", "skipped") else "failed"
            existing.sent_at = now if existing.status == "sent" else None
            db.add(existing)
            db.commit()
            if existing.status == "sent":
                sent += 1
            else:
                failed += 1

        campaign.sent_count = sent
        campaign.failed_count = failed
        campaign.sent_at = datetime.now(timezone.utc)
        campaign.status = "sent"

        # Recurring campaigns: {"freq": "daily"|"weekly"|"monthly", "interval": int, "until": "YYYY-MM-DD"}
        rule = campaign.recurring_rule or {}
        if rule.get("freq"):
            interval = int(rule.get("interval", 1))
            unit_days = {"daily": 1, "weekly": 7, "monthly": 30}.get(rule["freq"], 1)
            next_run = campaign.sent_at + timedelta(days=unit_days * interval)
            until = rule.get("until")
            if not until or next_run.date().isoformat() <= until:
                campaign.next_run_at = next_run
                campaign.status = "scheduled"
                campaign.scheduled_at = next_run

        db.add(campaign)
        db.commit()

    task_process_notification_queue.delay()
    logger.info("campaign %s sent: %d ok, %d failed", campaign_id, sent, failed)
    return {"sent": sent, "failed": failed}


@celery_app.task
def task_dispatch_due_campaigns() -> Dict[str, int]:
    """Beat sweep (every 5 min, see celery_app.py) — fires any campaign whose
    scheduled_at has arrived, including recurring campaigns rescheduled by
    task_send_campaign above."""
    from datetime import datetime, timezone

    from sqlmodel import Session, select

    from app.database import engine
    from app.models.notification import NotificationCampaign

    dispatched = 0
    with Session(engine) as db:
        due = db.exec(
            select(NotificationCampaign).where(
                NotificationCampaign.status == "scheduled",
                NotificationCampaign.scheduled_at <= datetime.now(timezone.utc),
            )
        ).all()
        for campaign in due:
            task_send_campaign.delay(str(campaign.id))
            dispatched += 1
    return {"dispatched": dispatched}
