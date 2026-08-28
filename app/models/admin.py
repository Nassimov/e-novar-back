from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlmodel import Field, SQLModel


class PlatformSettings(SQLModel, table=True):
    """
    Mirrors public.platform_settings.
    Singleton row (id is always TRUE) holding platform-wide, admin-configurable
    business parameters. Currently: pack5/pack10/group booking discount
    percentages — the only place these percentages are ever defined; see
    app/services/pricing.py.
    """

    __tablename__ = "platform_settings"

    id: bool = Field(default=True, primary_key=True)
    pack5_discount_percent: int = Field(default=10)
    pack10_discount_percent: int = Field(default=15)
    group_discount_percent: int = Field(default=20)

    # Session validation trust score (see app/services/session_validation.py)
    # — weights need not sum to 100, the engine normalizes by whatever total
    # is actually configured. Never hardcode these values elsewhere.
    trust_weight_student_validation: int = Field(default=40)
    trust_weight_teacher_confirmation: int = Field(default=20)
    trust_weight_session_completed: int = Field(default=10)
    trust_weight_online_duration: int = Field(default=20)
    trust_weight_gps_proximity: int = Field(default=10)
    trust_weight_clean_history: int = Field(default=10)
    trust_auto_approve_threshold: int = Field(default=80)
    trust_manual_review_threshold: int = Field(default=50)
    token_visible_minutes_before: int = Field(default=30)
    student_validation_window_hours: int = Field(default=24)
    # A session left in "validated" (student done, waiting on the teacher's
    # secondary confirm) with no bounded timeout would stall the payout AND
    # the student's review eligibility forever if the teacher never clicks
    # confirm — this window auto-finalizes it (same trust-score evaluation,
    # just without the teacher_confirmation signal) once it elapses. See
    # app/routers/session_validation.py's _check_expiry.
    teacher_confirmation_window_hours: int = Field(default=48)
    gps_proximity_threshold_meters: int = Field(default=500)

    # Booking safety rules (see app/routers/student_teachers.py +
    # app/workers/booking_tasks.py). A teacher has this many hours to
    # accept/refuse a paid booking before it's auto-cancelled (student not
    # charged). Consecutive no-responses escalate a self-expiring suspension
    # (one entry of booking_no_response_suspension_days per consecutive
    # strike, last value repeats past the end of the list); the strike
    # counter resets after booking_no_response_reset_days of clean history.
    # Separately, booking_refusal_block_threshold refused/no-response
    # attempts on the exact same (teacher, subject, weekday, time) combo
    # permanently blocks the student from re-booking that exact combo.
    booking_teacher_response_hours: int = Field(default=24)
    booking_refusal_block_threshold: int = Field(default=2)
    booking_no_response_suspension_days: List[int] = Field(
        default=[2, 5, 10],
        sa_column=sa.Column(ARRAY(sa.Integer), nullable=False, server_default="'{2,5,10}'"),
    )
    booking_no_response_reset_days: int = Field(default=60)

    # Online-session no-show detection (see app/workers/booking_tasks.py's
    # task_detect_online_teacher_no_show). If the teacher hasn't joined the
    # LiveKit room within this many minutes of the scheduled start, the
    # session is marked no_show, the student is refunded 100%, and the
    # teacher gets a strike via the same escalating suspension counter above.
    online_no_show_grace_minutes: int = Field(default=20)

    # Platform's own receiving accounts for the manual RIB CIB / RIB Edahabia
    # bank-transfer payment methods (see docs/migrations/068_rib_payment_methods.sql
    # and app/routers/public.py's bank-transfer-info endpoint). A student
    # paying via one of these rails transfers into whichever of these RIBs is
    # set here — real automated debit isn't built yet, this phase is manual/
    # admin-reconciled only, same as the existing cash/transfer flow.
    bank_beneficiary_name: str = Field(default="E-NOVAR SARL")
    platform_rib_cib: Optional[str] = Field(default=None)
    platform_rib_edahabia: Optional[str] = Field(default=None)

    # International card payments (CIB via Stripe) settle in EUR — Stripe
    # has no DZD settlement currency, so the DZD price must be converted at
    # checkout time. How many DZD = 1 EUR — see app/services/stripe.py's
    # create_checkout_session and app/routers/student_teachers.py's
    # book_teacher_slot for where this is actually applied.
    dzd_per_eur: float = Field(default=145.00)

    # Student no-show enforcement — mirrors the teacher escalation below but
    # only ever blocks new bookings (see StudentProfile.booking_suspended_until).
    student_no_show_suspension_days: List[int] = Field(
        default=[3, 7, 14],
        sa_column=sa.Column(ARRAY(sa.Integer), nullable=False, server_default="'{3,7,14}'"),
    )
    student_no_show_reset_days: int = Field(default=60)

    # In-person (at_home/at_student) absence reports (see
    # app/routers/session_validation.py's dispute_reason_code and
    # app/workers/booking_tasks.py's task_auto_resolve_disputes) — auto-
    # resolved in the filer's favor if the other party never counters.
    in_person_dispute_auto_resolve_hours: int = Field(default=48)

    # cash/transfer/rib_cib/rib_edahabia bookings stay "pending" until an
    # admin manually confirms or rejects the payment (see
    # app/routers/admin/bookings.py) — nothing auto-cancels these otherwise.
    # Not the teacher's fault (they never even got a chance to respond), so
    # this never strikes anyone — see app/workers/booking_tasks.py's
    # task_expire_unconfirmed_manual_payments.
    manual_payment_expiry_hours: int = Field(default=48)

    # Competitive Arena Phase 2 — Duel lifecycle, all admin-configurable per
    # the spec's explicit "do not hardcode" requirements. See
    # app/services/competitive/ for where each is actually read.
    competitive_question_count_options: List[int] = Field(
        default=[5, 10, 15, 20, 30],
        sa_column=sa.Column(ARRAY(sa.Integer), nullable=False, server_default="'{5,10,15,20,30}'"),
    )
    competitive_invitation_expiry_minutes: int = Field(default=15)
    competitive_max_scheduling_days: int = Field(default=60)
    competitive_disconnect_grace_minutes: int = Field(default=5)
    competitive_disconnect_policy: str = Field(default="cancel")  # cancel|forfeit
    competitive_reminder_minutes_before: List[int] = Field(
        default=[1440, 60, 15],
        sa_column=sa.Column(ARRAY(sa.Integer), nullable=False, server_default="'{1440,60,15}'"),
    )
    competitive_max_invitations_per_day: int = Field(default=20)
    competitive_max_pending_invitations: int = Field(default=5)
    competitive_invitation_cooldown_seconds: int = Field(default=30)

    # Competitive Arena Phase 3 — Gameplay Engine timings, all admin-
    # configurable per the spec. Per-question ANSWER time deliberately
    # reuses questions.estimated_time_sec (same value solo practice already
    # uses) rather than a new global — see app/services/competitive/
    # gameplay_service.py.
    competitive_match_countdown_seconds: int = Field(default=3)
    competitive_reading_time_seconds: int = Field(default=4)
    competitive_transition_time_seconds: int = Field(default=3)
    competitive_points_per_correct: int = Field(default=100)

    # Competitive Arena Phase 5 — Live Match Engine hardening. Speed bonus
    # was explicitly deferred in Phase 3 ("prepare architecture only"); this
    # is that future phase. In-game disconnect grace is deliberately
    # separate from (and much shorter than) Phase 2's waiting-room grace —
    # a mid-question disconnect can't wait minutes.
    competitive_speed_bonus_enabled: bool = Field(default=True)
    competitive_speed_bonus_max_points: int = Field(default=50)
    competitive_ingame_disconnect_grace_seconds: int = Field(default=60)
    competitive_heartbeat_timeout_seconds: int = Field(default=45)

    # Competitive Arena Phase 6 — Matchmaking & Queue Engine. Initial MMR is
    # read from here (not hardcoded) whenever a fresh CompetitiveStatistics
    # row is created — see app/services/competitive/statistics_service.py.
    # Expansion seconds/radius are parallel arrays: after
    # expansion_seconds[i] elapsed, the search radius becomes
    # expansion_radius[i] (spec example: ±50 at 20s, ±100 at 40s, ±150 at 60s).
    competitive_mmr_initial_rating: int = Field(default=1000)
    competitive_mmr_expansion_seconds: List[int] = Field(
        default=[20, 40, 60],
        sa_column=sa.Column(ARRAY(sa.Integer), nullable=False, server_default="'{20,40,60}'"),
    )
    competitive_mmr_expansion_radius: List[int] = Field(
        default=[50, 100, 150],
        sa_column=sa.Column(ARRAY(sa.Integer), nullable=False, server_default="'{50,100,150}'"),
    )
    competitive_matchmaking_accept_seconds: int = Field(default=15)
    competitive_min_match_quality_score: int = Field(default=40)
    competitive_language_fallback_seconds: int = Field(default=30)
    competitive_queue_default_wait_estimate_sec: int = Field(default=60)

    # Competitive Arena Phase 7 — Ranking System, Seasons & Leagues. MMR
    # Calculator knobs (see app/services/competitive/ranking_service.py):
    # k_factor is the classic Elo K-factor (max rating swing per match
    # before streak/inflation adjustments); streak_bonus rewards a hot
    # streak on top of the base Elo delta (win-only, capped); floor is the
    # rating never-goes-below clamp; inflation dampening shrinks *gains*
    # once a player is already above the threshold (spec: "rating inflation
    # protection"). Season defaults are used whenever a
    # CompetitiveSeason.reset_percentage is left NULL.
    competitive_mmr_k_factor: int = Field(default=32)
    competitive_mmr_streak_bonus_per_win: int = Field(default=1)
    competitive_mmr_streak_bonus_max: int = Field(default=10)
    competitive_mmr_floor: int = Field(default=0)
    competitive_mmr_inflation_dampening_threshold: int = Field(default=2000)
    competitive_mmr_inflation_dampening_factor: float = Field(default=0.5)
    competitive_season_default_reset_strategy: str = Field(default="soft")
    competitive_season_default_reset_percentage: int = Field(default=50)
    competitive_season_ending_soon_hours: int = Field(default=72)

    # Competitive Arena Phase 8 — Spectator Mode, Live Reactions & Predictions.
    # Migration 083 already added these columns to platform_settings and
    # app/routers/admin/settings.py's _serialize_competitive() already reads
    # them, but the SQLModel class itself never declared them — a model/
    # migration drift bug that made GET /api/admin/settings/competitive 500.
    # Fixed here (found while building Phase 11 Part A, unrelated to Clash
    # Club, but a one-line-per-field fix worth closing immediately).
    competitive_spectator_max_default: Optional[int] = Field(default=None)
    competitive_prediction_lock_before_position: int = Field(default=3)
    competitive_prediction_reward_arena_xp: int = Field(default=10)
    competitive_prediction_reward_spectator_xp: int = Field(default=5)
    competitive_reaction_rate_limit_per_10s: int = Field(default=5)
    competitive_chat_rate_limit_per_10s: int = Field(default=3)

    # Competitive Arena Phase 9 — Tournament System scheduling knobs. Match
    # start delay is the grace period between a round starting and matches
    # auto-beginning (gives players time to see they've advanced); round
    # advance grace is how long to wait for a disconnected/no-show player
    # before the round can be force-progressed — see app/services/
    # competitive/tournament_service.py (Part B consumes both).
    competitive_tournament_match_start_delay_seconds: int = Field(default=30)
    competitive_tournament_round_advance_grace_minutes: int = Field(default=5)

    # Competitive Arena Phase 11 — Clash Club (Club vs Club), Part A config
    # knobs. Unlike tournament/battle royale's entry_cost_ep (reserved-only
    # in V1), club creation cost IS actually spent here — see
    # app/services/club/club_service.py's create_club (reuses
    # app/services/kp.py's spend_kp). min_level/min_rating/min_account_age_
    # days are nullable = no requirement; competitive_club_default_max_
    # members is the fallback a club's own max_members resolves to when not
    # explicitly chosen at creation (from the fixed menu — see
    # app/models/club.py's CLUB_MAX_MEMBERS_OPTIONS, mirrors Battle
    # Royale's BATTLE_ROYALE_MAX_PLAYERS_OPTIONS pattern).
    competitive_club_creation_cost_ep: int = Field(default=0)
    competitive_club_min_level: Optional[int] = Field(default=None)
    competitive_club_min_rating: Optional[int] = Field(default=None)
    competitive_club_min_account_age_days: Optional[int] = Field(default=None)
    competitive_club_default_max_members: int = Field(default=50)
    competitive_club_name_min_length: int = Field(default=3)
    competitive_club_name_max_length: int = Field(default=40)

    # Phase 11, Part B — Club Reputation gain amounts (migration 090).
    competitive_club_reputation_battle_win: int = Field(default=10)
    competitive_club_reputation_activity_daily: int = Field(default=1)
    competitive_club_reputation_achievement: int = Field(default=15)
    competitive_club_reputation_abuse_report: int = Field(default=-25)

    # Phase 11, Part C — Club Battle / Club Rating Engine config (migration
    # 091). Victory gain/defeat loss are flat amounts (not full Elo, per
    # spec's own "Victory gain / Defeat loss" wording) — protection_battles
    # halves defeat_loss for a club's first N battles (spec: "Protection").
    competitive_club_battle_team_size_default: int = Field(default=3)
    competitive_club_battle_challenge_expiry_hours: int = Field(default=48)
    competitive_club_rating_victory_gain: int = Field(default=25)
    competitive_club_rating_defeat_loss: int = Field(default=20)
    competitive_club_rating_protection_battles: int = Field(default=5)
    competitive_club_rating_floor: int = Field(default=100)
    competitive_club_battle_ep_reward_winner: int = Field(default=0)
    competitive_club_battle_ep_reward_participation: int = Field(default=0)
    competitive_club_battle_xp_reward_winner: int = Field(default=20)
    competitive_club_battle_xp_reward_participation: int = Field(default=5)

    # Phase 12 — Replay System & AI Match Analysis V2 (migration 092).
    # Grade thresholds are accuracy-% cutoffs (descending) — see
    # app/services/competitive/analysis_service.py's compute_match_grade.
    competitive_grade_a_plus_min_accuracy: int = Field(default=95)
    competitive_grade_a_min_accuracy: int = Field(default=85)
    competitive_grade_b_plus_min_accuracy: int = Field(default=75)
    competitive_grade_b_min_accuracy: int = Field(default=65)
    competitive_grade_c_min_accuracy: int = Field(default=50)
    competitive_grade_d_min_accuracy: int = Field(default=35)
    competitive_replay_default_visibility: str = Field(default="private")

    # Phase 13 — Ranked Ladder V2 (migration 093).
    competitive_placement_matches_required: int = Field(default=10)
    competitive_placement_k_factor_multiplier: float = Field(default=2.0)
    competitive_ranked_match_types: str = Field(default="duel,battle_royale,tournament")
    competitive_casual_ep_reward_winner: int = Field(default=15)
    competitive_casual_ep_reward_participation: int = Field(default=5)
    competitive_fair_play_disconnect_penalty: int = Field(default=-5)
    competitive_fair_play_report_penalty: int = Field(default=-10)
    competitive_fair_play_afk_penalty: int = Field(default=-8)
    competitive_fair_play_clean_match_bonus: int = Field(default=1)
    competitive_fair_play_min_for_ranked: int = Field(default=30)
    competitive_inactivity_decay_enabled: bool = Field(default=False)
    competitive_inactivity_decay_after_days: int = Field(default=30)
    competitive_inactivity_decay_amount: int = Field(default=25)
    competitive_inactivity_decay_floor: int = Field(default=800)
    competitive_ranked_min_account_age_days: int = Field(default=0)
    competitive_ranked_require_onboarding: bool = Field(default=False)
    competitive_ranked_min_ep_balance: int = Field(default=0)
    competitive_ranked_require_phone_verified: bool = Field(default=False)
    competitive_ranked_require_email_verified: bool = Field(default=False)
    competitive_promotion_series_enabled: bool = Field(default=False)

    # Phase 14 — Achievements, Titles, Cosmetics & Player Progression (migration 094).
    competitive_badge_showcase_max: int = Field(default=6)
    competitive_sticker_showcase_max: int = Field(default=6)
    competitive_achievement_showcase_max: int = Field(default=3)

    # Phase 15 — Events, Missions, Daily Challenges & Live Operations (migration 095).
    competitive_daily_missions_count: int = Field(default=3)
    competitive_weekly_missions_count: int = Field(default=3)
    competitive_monthly_missions_count: int = Field(default=2)
    competitive_mission_free_rerolls_daily: int = Field(default=1)
    competitive_mission_free_rerolls_weekly: int = Field(default=1)
    competitive_login_streak_grace_hours: int = Field(default=6)
    competitive_login_calendar_length: int = Field(default=30)
    competitive_event_ending_soon_hours: int = Field(default=24)
    competitive_happy_hour_starting_soon_minutes: int = Field(default=30)
    competitive_mission_almost_done_pct: int = Field(default=80)

    # Phase 16 — Production Hardening: feature flags, rate limits, moderation.
    feature_battle_royale_enabled: bool = Field(default=True)
    feature_tournament_enabled: bool = Field(default=True)
    feature_replay_enabled: bool = Field(default=True)
    feature_ai_analysis_enabled: bool = Field(default=True)
    feature_ranked_enabled: bool = Field(default=True)
    feature_liveops_enabled: bool = Field(default=True)
    feature_clubs_enabled: bool = Field(default=True)
    feature_spectator_enabled: bool = Field(default=True)
    rate_limit_match_creation_per_10s: int = Field(default=5)
    rate_limit_answer_submit_per_10s: int = Field(default=20)
    rate_limit_replay_request_per_10s: int = Field(default=20)
    rate_limit_leaderboard_refresh_per_10s: int = Field(default=10)
    rate_limit_report_submit_per_10s: int = Field(default=3)
    rate_limit_invitation_create_per_10s: int = Field(default=5)
    moderation_default_mute_hours: int = Field(default=24)
    moderation_default_suspension_days: int = Field(default=7)

    updated_at: datetime = Field(default_factory=datetime.utcnow)


class CmsPage(SQLModel, table=True):
    """
    Mirrors public.cms_pages.
    Admin-editable static pages (terms, privacy, help, cookies).
    slug is the unique identifier (e.g. 'terms-of-service').
    """

    __tablename__ = "cms_pages"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    slug: str = Field(sa_column=sa.Column(sa.String, unique=True, nullable=False))
    title: str = Field()
    body_md: Optional[str] = Field(default=None)
    version: int = Field(default=1)
    published_at: Optional[datetime] = Field(default=None)


class LegalAcceptance(SQLModel, table=True):
    """
    Mirrors public.legal_acceptances.
    UNIQUE on (user_id, page_slug, version) — tracks which legal version a user accepted.
    """

    __tablename__ = "legal_acceptances"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    page_slug: str = Field()
    version: int = Field()
    accepted_at: datetime = Field(default_factory=datetime.utcnow)

    __table_args__ = (
        sa.UniqueConstraint("user_id", "page_slug", "version", name="uq_legal_acceptance"),
    )


class PromoCode(SQLModel, table=True):
    """
    Mirrors public.promo_codes.

    Two mutually exclusive reward modes:
    - KP bonus  (kp_reward > 0)          : awarded immediately when user applies the code.
    - Discount  (discount_type is set)    : applied at booking checkout to reduce the price.
    A code may have both (e.g. +50 EP bonus + -10% discount).

    valid_from / valid_to : optional activation window.
    max_uses              : None = unlimited global uses.
    target_role           : 'all' | 'student' | 'teacher' | 'parent'
    """

    __tablename__ = "promo_codes"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    code: str = Field(sa_column=sa.Column(sa.String, unique=True, nullable=False))
    title: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None)
    # Booking-discount reward
    discount_type: Optional[str] = Field(
        default=None,
        sa_column=sa.Column(sa.String, nullable=True),
    )  # 'percent' | 'fixed' | None
    discount_value: int = Field(default=0)
    # Instant KP reward
    kp_reward: int = Field(default=0)
    # Validity
    valid_from: Optional[datetime] = Field(default=None)
    valid_to: Optional[datetime] = Field(default=None)
    max_uses: Optional[int] = Field(default=None)
    uses: int = Field(default=0)
    active: bool = Field(default=True)
    target_role: str = Field(default="all")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class PromoRedemption(SQLModel, table=True):
    """
    Mirrors public.promo_redemptions.
    UNIQUE on (code_id, user_id) — a user can redeem a code only once.
    """

    __tablename__ = "promo_redemptions"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    code_id: UUID = Field(foreign_key="promo_codes.id", index=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    booking_id: Optional[UUID] = Field(default=None, foreign_key="bookings.id")
    redeemed_at: datetime = Field(default_factory=datetime.utcnow)

    __table_args__ = (
        sa.UniqueConstraint("code_id", "user_id", name="uq_promo_redemption"),
    )


class AuditLog(SQLModel, table=True):
    """
    Mirrors public.audit_logs.
    Immutable record of admin/system actions.
    meta: jsonb — action-specific payload.
    """

    __tablename__ = "audit_logs"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    actor_id: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    action: str = Field()
    target_type: Optional[str] = Field(default=None)
    target_id: Optional[UUID] = Field(default=None)
    meta: Optional[Any] = Field(
        default=None,
        sa_column=sa.Column(JSONB, nullable=True, server_default="'{}'::jsonb"),
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Report(SQLModel, table=True):
    """
    Mirrors public.reports.
    Moderation reports submitted by users against any entity.
    target_type: 'review' | 'user' | 'teacher' | 'message' | 'challenge'
    """

    __tablename__ = "reports"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    reporter_id: UUID = Field(foreign_key="profiles.id", index=True)
    target_type: str = Field()
    target_id: UUID = Field()
    reason: Optional[str] = Field(default=None)
    status: str = Field(default="open")                  # public.report_status
    created_at: datetime = Field(default_factory=datetime.utcnow)

    # Phase 16 (Competitive Arena) — migration 096. Widened so this ONE
    # moderation queue also serves Arena reports (cheating, harassment,
    # offensive username/club, ...) instead of a parallel arena_reports
    # table — category/evidence are Arena-specific but harmless no-ops for
    # the pre-existing review-report flow (which never sets them).
    category: Optional[str] = Field(default=None)
    evidence: Any = Field(default_factory=list, sa_column=sa.Column(JSONB, nullable=False, server_default="'[]'::jsonb"))
    reviewed_by: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    reviewed_at: Optional[datetime] = Field(default=None)
    resolution_note: Optional[str] = Field(default=None)
    merged_into_id: Optional[UUID] = Field(default=None, foreign_key="reports.id")


class Invitation(SQLModel, table=True):
    """
    Mirrors public.invitations.
    Direct email invitations (distinct from referral codes).
    """

    __tablename__ = "invitations"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    inviter_id: UUID = Field(foreign_key="profiles.id", index=True)
    email: str = Field()
    message: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
