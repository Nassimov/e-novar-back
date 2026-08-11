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
        "app.workers.competitive_tasks",
        "app.workers.club_tasks",
        "app.workers.liveops_tasks",
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
    # A broker hiccup must fail an .apply_async() call fast, not hang the
    # HTTP request that triggered it for 20+ seconds — matters most for
    # Competitive Arena's gameplay tick scheduling (called inline from
    # request handlers, e.g. app/services/competitive/gameplay_service.py).
    broker_connection_timeout=3,
    broker_connection_retry_on_startup=False,
    broker_connection_max_retries=1,
    broker_transport_options={"socket_timeout": 3, "socket_connect_timeout": 3, "max_retries": 1},
    task_routes={
        "app.workers.email_tasks.*": {"queue": "email"},
        "app.workers.sms_tasks.*": {"queue": "sms"},
        "app.workers.pdf_tasks.*": {"queue": "pdf"},
        "app.workers.notification_tasks.*": {"queue": "notifications"},
        "app.workers.booking_tasks.*": {"queue": "notifications"},
        "app.workers.campaign_tasks.*": {"queue": "notifications"},
        "app.workers.competitive_tasks.*": {"queue": "notifications"},
        "app.workers.club_tasks.*": {"queue": "notifications"},
        "app.workers.liveops_tasks.*": {"queue": "notifications"},
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
        # Every 5 min — expire stale competitive challenge invitations
        # (default TTL 15 min) and draft matches that were never followed
        # by an invitation.
        "competitive-invitations-expiry": {
            "task": "app.workers.competitive_tasks.task_expire_competitive_invitations",
            "schedule": crontab(minute="*/5"),
        },
        # Every 5 min — 24h/1h/15min (configurable) reminders before a
        # scheduled competitive duel.
        "competitive-match-reminders": {
            "task": "app.workers.competitive_tasks.task_send_competitive_match_reminders",
            "schedule": crontab(minute="*/5"),
        },
        # Every 20s — Phase 5 Heartbeat Manager: catches silent mid-game
        # disconnects (frozen tab, killed process) a clean WS close event
        # never reports. Default heartbeat timeout is 45s, so this cadence
        # detects staleness within one sweep of it going stale.
        "competitive-heartbeat-sweep": {
            "task": "app.workers.competitive_tasks.task_detect_stale_gameplay_connections",
            "schedule": 20.0,
        },
        # Every 5s — Phase 6 Matchmaking Engine: pairs queued players and
        # resolves expired match proposals. Deliberately tight (spec: "the
        # objective is to minimize waiting time") — cheap query, bounded to
        # actively-searching rows only.
        "competitive-matchmaking-sweep": {
            "task": "app.workers.competitive_tasks.task_matchmaking_sweep",
            "schedule": 5.0,
        },
        # Every 15 min — Phase 7 Season End Workflow: ends the active season
        # once its configured ends_at has elapsed (no-op otherwise).
        "competitive-season-end-check": {
            "task": "app.workers.competitive_tasks.task_check_competitive_season_end",
            "schedule": crontab(minute="*/15"),
        },
        # Every 10 min — Phase 7: notifies participants once the active
        # season enters its configurable "ending soon" window.
        "competitive-season-ending-soon-check": {
            "task": "app.workers.competitive_tasks.task_notify_competitive_season_ending_soon",
            "schedule": crontab(minute="*/10"),
        },
        # Every 15 min — Phase 11 Part C: expires stale pending club battle
        # challenges (default TTL 48h, admin-configurable).
        "club-battle-challenges-expiry": {
            "task": "app.workers.club_tasks.task_expire_club_battle_challenges",
            "schedule": crontab(minute="*/15"),
        },
        # Every 15 min — Phase 11 Part C: Club Season End Workflow, mirrors
        # the player-level season-end check.
        "club-season-end-check": {
            "task": "app.workers.club_tasks.task_check_club_season_end",
            "schedule": crontab(minute="*/15"),
        },
        # Daily at 00:10 — Phase 11 Part B: rolls up yesterday's per-club
        # chat/battle/membership activity into club_daily_activity so
        # analytics stays O(days) at read time.
        "club-daily-activity-rollup": {
            "task": "app.workers.club_tasks.task_rollup_club_daily_activity",
            "schedule": crontab(hour=0, minute=10),
        },
        # Daily at 03:00 — Phase 13: optional Arena Rating inactivity decay
        # (genuine no-op unless an admin enables it via PlatformSettings).
        "competitive-inactivity-decay": {
            "task": "app.workers.competitive_tasks.task_apply_competitive_inactivity_decay",
            "schedule": crontab(hour=3, minute=0),
        },
        # Daily at 00:01 Algiers — Phase 15 Mission Engine: assigns the new
        # day's missions to every existing Arena player (spec: "every player
        # receives a new set of missions every day").
        "liveops-reset-daily-missions": {
            "task": "app.workers.liveops_tasks.task_reset_daily_missions",
            "schedule": crontab(hour=0, minute=1),
        },
        # Monday 00:01 Algiers — weekly mission reset.
        "liveops-reset-weekly-missions": {
            "task": "app.workers.liveops_tasks.task_reset_weekly_missions",
            "schedule": crontab(hour=0, minute=1, day_of_week=1),
        },
        # 1st of month, 00:01 Algiers — monthly mission reset.
        "liveops-reset-monthly-missions": {
            "task": "app.workers.liveops_tasks.task_reset_monthly_missions",
            "schedule": crontab(hour=0, minute=1, day_of_month=1),
        },
        # Every 5 min — Phase 15 Event Manager: activates scheduled events
        # whose starts_at has arrived, ends active events past ends_at.
        "liveops-activate-events": {
            "task": "app.workers.liveops_tasks.task_liveops_activate_events",
            "schedule": crontab(minute="*/5"),
        },
        "liveops-end-events": {
            "task": "app.workers.liveops_tasks.task_liveops_end_events",
            "schedule": crontab(minute="*/5"),
        },
        # Every 30 min — event-ending-soon reminder.
        "liveops-notify-events-ending-soon": {
            "task": "app.workers.liveops_tasks.task_liveops_notify_events_ending_soon",
            "schedule": crontab(minute="*/30"),
        },
        # Every 5 min — Happy Hour "starting in 30 minutes" reminder needs a
        # tight cadence relative to its own window.
        "liveops-notify-happy-hour-starting-soon": {
            "task": "app.workers.liveops_tasks.task_liveops_notify_happy_hour_starting_soon",
            "schedule": crontab(minute="*/5"),
        },
        # Every 30 min — login streak expiring reminder (self-gates to the
        # evening inside login_service.notify_streak_expiring).
        "liveops-notify-login-streak-expiring": {
            "task": "app.workers.liveops_tasks.task_liveops_notify_login_streak_expiring",
            "schedule": crontab(minute="*/30"),
        },
        # Every 30 min — "mission almost complete" nudge.
        "liveops-notify-missions-almost-done": {
            "task": "app.workers.liveops_tasks.task_liveops_notify_missions_almost_done",
            "schedule": crontab(minute="*/30"),
        },
        # Once daily at 20:00 Algiers — "mission expires tonight" reminder.
        "liveops-notify-missions-expire-tonight": {
            "task": "app.workers.liveops_tasks.task_liveops_notify_missions_expire_tonight",
            "schedule": crontab(hour=20, minute=0),
        },
    },
)
