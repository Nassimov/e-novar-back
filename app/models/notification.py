from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlmodel import Field, SQLModel


class Notification(SQLModel, table=True):
    """
    Mirrors public.notifications.
    type: specific event_type from notification_templates (booking_confirmed,
      session_reminder, kp_earned, badge_unlocked, new_message, system, ...) —
      fine-grained, used e.g. for sidebar nav badge routing (see
      src/lib/nav-badges.tsx on the frontend).
    category: broader grouping (chat/lessons/homework/challenges/leaderboard/
      badges/ep_rewards/payments/ai/marketing/account/security/system/
      administration) — matches NotificationPreference.category_prefs keys,
      used for per-category channel opt-out.
    priority: critical | high | normal | marketing — see
      app/services/notification_engine.py for how this affects delivery
      (critical/security bypass most opt-outs; marketing always respects them).
    data: jsonb — extra context for deep-linking (e.g. booking_id, session_id).
    deep_link: precomputed frontend route to open when the notification is clicked.
    dedup_key: optional caller-supplied key (e.g. f"lesson_booked:{booking_id}")
      — the partial unique index on (user_id, dedup_key) makes emit() safe to
      call more than once for the same real-world event without duplicating rows.
    channel: in_app | push | email | sms — the channel THIS row represents
      (the engine may persist one in_app row and additionally deliver push/email
      for the same event; those don't get separate Notification rows, they get
      notification_delivery_logs rows instead).
    read_at = None → unread; set when user opens it.
    archived_at = None → active; set when the user archives it (still visible
      in "archived" filter, excluded from the default/unread views).
    """

    __tablename__ = "notifications"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    type: str = Field()
    category: str = Field(default="system")
    priority: str = Field(default="normal")
    title: Optional[str] = Field(default=None)
    body: Optional[str] = Field(default=None)
    data: Optional[Any] = Field(
        default=None,
        sa_column=sa.Column(JSONB, nullable=True, server_default="'{}'::jsonb"),
    )
    deep_link: Optional[str] = Field(default=None)
    dedup_key: Optional[str] = Field(default=None)
    channel: str = Field(default="in_app")               # public.notif_channel
    read_at: Optional[datetime] = Field(default=None)
    archived_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)


@sa.event.listens_for(Notification, "after_insert")
def _publish_badge_event(mapper, connection, target: "Notification") -> None:
    """Push a real-time badge event on every new notification row, regardless
    of which router/task created it (direct `db.add(Notification(...))` or
    `notification_service.persist()`) — one hook instead of patching every
    call site. Reuses the existing chat WS channel: app/main.py's
    /ws/messages already forwards any JSON published to `chat:user:{id}`
    straight to the connected client's socket, so no new WS endpoint is
    needed. Never allowed to break the insert it's attached to.
    """
    try:
        import json
        from app.core.redis import get_redis_client
        get_redis_client().publish(
            f"chat:user:{target.user_id}",
            json.dumps({
                "type": "notification",
                "notification_type": target.type,
                "id": str(target.id),
                "created_at": target.created_at.isoformat(),
            }),
        )
    except Exception:
        pass


#: Categories every user has independent push/email/in_app control over —
#: keys of NotificationPreference.category_prefs, seeded by migration 070.
NOTIFICATION_CATEGORIES = [
    "account", "security", "chat", "lessons", "homework", "challenges",
    "leaderboard", "badges", "ep_rewards", "payments", "ai", "marketing",
    "system", "administration",
]

#: Categories a user can never fully silence — critical/security-adjacent.
#: The in_app row is always persisted regardless of preferences; only the
#: push/email fan-out respects (most of) the opt-out for everything else.
ALWAYS_IN_APP_CATEGORIES = {"security", "account"}


class NotificationPreference(SQLModel, table=True):
    """
    Mirrors public.notification_preferences.
    PK = user_id — one preference row per user.
    Created automatically by the handle_new_user() trigger in Supabase.

    push/email/sms/reminders/sessions/kp/rewards/messages: legacy coarse
    toggles, kept for backward compat with any old reads — no longer written
    by new code (see category_prefs below).

    category_prefs: JSONB matrix, one entry per NOTIFICATION_CATEGORIES key,
    each {"push": bool, "email": bool, "in_app": bool} — this is what
    app/services/notification_engine.py actually consults.
    """

    __tablename__ = "notification_preferences"

    user_id: UUID = Field(primary_key=True, foreign_key="profiles.id")
    push: bool = Field(default=True)
    email: bool = Field(default=True)
    sms: bool = Field(default=False)
    reminders: bool = Field(default=True)
    sessions: bool = Field(default=True)
    kp: bool = Field(default=True)
    rewards: bool = Field(default=True)
    messages: bool = Field(default=True)
    category_prefs: Optional[Any] = Field(
        default=None,
        sa_column=sa.Column(JSONB, nullable=True),
    )


class NotificationTemplate(SQLModel, table=True):
    """
    Mirrors public.notification_templates — the event catalogue. One row per
    event_type; app/services/notification_engine.py's emit(event_type, ...)
    looks the row up to resolve category/priority/channels and render
    title/body from Python .format()-style placeholders (see
    app/services/notification_templates_seed.py for the full seed list).
    """

    __tablename__ = "notification_templates"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    event_type: str = Field(sa_column=sa.Column(sa.Text, unique=True, nullable=False))
    category: str = Field()
    priority: str = Field(default="normal")
    channels: List[str] = Field(
        default_factory=lambda: ["in_app"],
        sa_column=sa.Column(ARRAY(sa.Text), nullable=False, server_default="{in_app}"),
    )
    title_template: str = Field()
    body_template: str = Field()
    deep_link_template: Optional[str] = Field(default=None)
    active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class NotificationCampaign(SQLModel, table=True):
    """
    Mirrors public.notification_campaigns — admin-authored one-off or
    scheduled/recurring broadcasts. See app/routers/admin/notification_campaigns.py.
    """

    __tablename__ = "notification_campaigns"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field()
    description: Optional[str] = Field(default=None)
    category: str = Field(default="marketing")
    priority: str = Field(default="marketing")
    channels: List[str] = Field(
        default_factory=lambda: ["push", "email"],
        sa_column=sa.Column(ARRAY(sa.Text), nullable=False, server_default="{push,email}"),
    )
    title: str = Field()
    body: str = Field()
    deep_link: Optional[str] = Field(default=None)
    target_type: str = Field(default="all")  # all|students|teachers|parents|admins|custom
    target_filters: Optional[Any] = Field(
        default=None,
        sa_column=sa.Column(JSONB, nullable=False, server_default="'{}'::jsonb"),
    )
    status: str = Field(default="draft")  # draft|scheduled|sending|sent|cancelled|failed
    scheduled_at: Optional[datetime] = Field(default=None)
    recurring_rule: Optional[Any] = Field(
        default=None,
        sa_column=sa.Column(JSONB, nullable=True),
    )
    created_by: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    # Admin accounts authenticate via a separate custom-JWT system (see
    # app/dependencies.py get_admin_user) and are never rows in `profiles`,
    # so `created_by` above stays null for admin-created campaigns — this
    # free-text label (the admin's email) is what the UI actually displays.
    created_by_label: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    sent_at: Optional[datetime] = Field(default=None)
    recipients_count: int = Field(default=0)
    sent_count: int = Field(default=0)
    failed_count: int = Field(default=0)
    # Set by app/workers/campaign_tasks.py's task_send_campaign after a
    # recurring send completes — when its next occurrence should fire
    # (status is set back to 'scheduled' and scheduled_at moved to this
    # value too, so task_dispatch_due_campaigns' single scheduled_at poll
    # keeps working unchanged for recurring campaigns).
    next_run_at: Optional[datetime] = Field(default=None)


class NotificationCampaignTarget(SQLModel, table=True):
    """Mirrors public.notification_campaign_targets — materialized recipient list per send."""

    __tablename__ = "notification_campaign_targets"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    campaign_id: UUID = Field(foreign_key="notification_campaigns.id", index=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    status: str = Field(default="pending")  # pending|sent|failed|skipped
    sent_at: Optional[datetime] = Field(default=None)
    error: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class NotificationDeliveryLog(SQLModel, table=True):
    """Mirrors public.notification_delivery_logs — one row per channel attempt."""

    __tablename__ = "notification_delivery_logs"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    notification_id: Optional[UUID] = Field(default=None, foreign_key="notifications.id")
    campaign_id: Optional[UUID] = Field(default=None, foreign_key="notification_campaigns.id")
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    channel: str = Field()  # push|email|in_app|ws
    status: str = Field()   # success|failed|skipped
    error: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class NotificationQueue(SQLModel, table=True):
    """
    Mirrors public.notification_queue — DB-backed async delivery queue on top
    of Celery's broker: gives retry state + a dedup key that survives worker
    restarts (Celery's broker alone doesn't). Processed by
    app/workers/notification_tasks.py's task_process_notification_queue.
    """

    __tablename__ = "notification_queue"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    event_type: str = Field()
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    context: Any = Field(
        default_factory=dict,
        sa_column=sa.Column(JSONB, nullable=False, server_default="'{}'::jsonb"),
    )
    dedup_key: Optional[str] = Field(default=None)
    status: str = Field(default="pending")  # pending|processing|done|failed
    attempts: int = Field(default=0)
    next_attempt_at: datetime = Field(default_factory=datetime.utcnow)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    processed_at: Optional[datetime] = Field(default=None)


class NotificationFailure(SQLModel, table=True):
    """Mirrors public.notification_failures — terminal failures after retry exhaustion."""

    __tablename__ = "notification_failures"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    queue_id: Optional[UUID] = Field(default=None, foreign_key="notification_queue.id")
    notification_id: Optional[UUID] = Field(default=None, foreign_key="notifications.id")
    user_id: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    channel: str = Field()
    error: str = Field()
    retry_count: int = Field(default=0)
    resolved: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow)
