from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class UserListParams(BaseModel):
    page: int = Field(default=1, ge=1)
    size: int = Field(default=20, ge=1, le=100)
    role: Optional[str] = None
    is_active: Optional[bool] = None
    search: Optional[str] = None


class UserStatusUpdate(BaseModel):
    is_active: bool
    reason: Optional[str] = None


class TeacherApprovalAction(BaseModel):
    action: str  # "approve" or "reject"
    reason: Optional[str] = None


class ReviewModerationAction(BaseModel):
    action: str  # "flag" or "unflag" or "delete"
    reason: Optional[str] = None


class PromoCodeCreate(BaseModel):
    code: str = Field(min_length=3, max_length=30)
    title: Optional[str] = Field(default=None, max_length=80)
    description: Optional[str] = Field(default=None, max_length=300)
    # Instant KP reward (0 = none)
    kp_reward: int = Field(default=0, ge=0)
    # Booking discount (optional)
    discount_type: Optional[str] = None        # 'percent' | 'fixed' | None
    discount_value: int = Field(default=0, ge=0)
    # Validity
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    max_uses: Optional[int] = Field(default=None, gt=0)
    # Targeting
    target_role: str = Field(default="all")
    active: bool = True

    @field_validator("target_role")
    @classmethod
    def validate_target_role(cls, v: str) -> str:
        from app.services.promo_targeting import VALID_TARGET_ROLES
        if v not in VALID_TARGET_ROLES:
            raise ValueError(f"target_role invalide : '{v}'.")
        return v


class PromoCodeUpdate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=80)
    description: Optional[str] = Field(default=None, max_length=300)
    kp_reward: Optional[int] = Field(default=None, ge=0)
    discount_type: Optional[str] = None
    discount_value: Optional[int] = Field(default=None, ge=0)
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    max_uses: Optional[int] = Field(default=None, gt=0)
    target_role: Optional[str] = None

    @field_validator("target_role")
    @classmethod
    def validate_target_role(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        from app.services.promo_targeting import VALID_TARGET_ROLES
        if v not in VALID_TARGET_ROLES:
            raise ValueError(f"target_role invalide : '{v}'.")
        return v
    active: Optional[bool] = None


class StatsResponse(BaseModel):
    total_users: int
    total_teachers: int
    total_students: int
    total_bookings: int
    total_sessions_completed: int
    total_revenue_dzd: int
    active_users_this_week: int
    new_users_this_month: int
    pending_withdrawals: int
    pending_teacher_approvals: int


class WithdrawalProcessRequest(BaseModel):
    action: str                       # "approve" or "reject"
    dzd_amount: Optional[int] = None  # obligatoire si action == "approve"
    admin_note: Optional[str] = None


class PlatformPricingSettings(BaseModel):
    pack5_discount_percent: int = Field(ge=0, le=100)
    pack10_discount_percent: int = Field(ge=0, le=100)
    # Strictly > 0 (not >= 0 like the packs): a group session must always be
    # cheaper than a single one, by business rule — there is no "0% group
    # discount" option.
    group_discount_percent: int = Field(gt=0, le=100)

    @field_validator("pack10_discount_percent")
    @classmethod
    def validate_pack10_better(cls, v: int, info) -> int:
        pack5 = info.data.get("pack5_discount_percent")
        if pack5 is not None and v <= pack5:
            raise ValueError(
                "La réduction du pack de 10 doit être supérieure à celle du pack de 5."
            )
        return v


class CompetitiveArenaSettings(BaseModel):
    """Phase 7's MMR Calculator knobs (shipped in migration 082, but never
    exposed via an admin endpoint until now — league/season CRUD existed,
    the raw MMR tuning knobs didn't) folded together with Phase 8's
    spectator/prediction/reaction/chat knobs (migration 083) into one
    section, mirroring app/routers/admin/settings.py's existing
    _serialize/GET/PUT pattern (e.g. /pricing)."""

    # Phase 7 — MMR Calculator (app/services/competitive/ranking_service.py)
    competitive_mmr_initial_rating: int = Field(ge=0, le=5000)
    competitive_mmr_k_factor: int = Field(ge=1, le=200)
    competitive_mmr_streak_bonus_per_win: int = Field(ge=0, le=50)
    competitive_mmr_streak_bonus_max: int = Field(ge=0, le=500)
    competitive_mmr_floor: int = Field(ge=0, le=5000)
    competitive_mmr_inflation_dampening_threshold: int = Field(ge=0, le=10000)
    competitive_mmr_inflation_dampening_factor: float = Field(ge=0, le=1)
    competitive_season_default_reset_strategy: str
    competitive_season_default_reset_percentage: int = Field(ge=0, le=100)

    # Phase 8 — Spectator Mode, Live Reactions & Predictions
    competitive_spectator_max_default: Optional[int] = Field(default=None, ge=0)
    competitive_prediction_lock_before_position: int = Field(ge=0, le=100)
    competitive_prediction_reward_arena_xp: int = Field(ge=0, le=1000)
    competitive_prediction_reward_spectator_xp: int = Field(ge=0, le=1000)
    competitive_reaction_rate_limit_per_10s: int = Field(ge=1, le=100)
    competitive_chat_rate_limit_per_10s: int = Field(ge=1, le=100)

    # Phase 9 — Tournament System scheduling knobs
    competitive_tournament_match_start_delay_seconds: int = Field(ge=0, le=3600)
    competitive_tournament_round_advance_grace_minutes: int = Field(ge=0, le=1440)

    # Phase 11 — Clash Club (Club vs Club) config knobs
    competitive_club_creation_cost_ep: int = Field(ge=0, le=100000)
    competitive_club_min_level: Optional[int] = Field(default=None, ge=0, le=100)
    competitive_club_min_rating: Optional[int] = Field(default=None, ge=0, le=5000)
    competitive_club_min_account_age_days: Optional[int] = Field(default=None, ge=0, le=3650)
    competitive_club_default_max_members: int
    competitive_club_name_min_length: int = Field(ge=1, le=100)
    competitive_club_name_max_length: int = Field(ge=1, le=200)

    # Phase 2 — Duel invitation lifecycle (app/services/competitive/)
    competitive_question_count_options: List[int] = Field(min_length=1, max_length=10)
    competitive_invitation_expiry_minutes: int = Field(ge=1, le=1440)
    competitive_max_scheduling_days: int = Field(ge=1, le=365)
    competitive_disconnect_grace_minutes: int = Field(ge=1, le=60)
    competitive_disconnect_policy: str
    competitive_reminder_minutes_before: List[int] = Field(min_length=0, max_length=10)
    competitive_max_invitations_per_day: int = Field(ge=1, le=1000)
    competitive_max_pending_invitations: int = Field(ge=1, le=100)
    competitive_invitation_cooldown_seconds: int = Field(ge=0, le=3600)

    # Phase 3/5 — Live Match Engine gameplay timings
    competitive_match_countdown_seconds: int = Field(ge=0, le=60)
    competitive_reading_time_seconds: int = Field(ge=0, le=120)
    competitive_transition_time_seconds: int = Field(ge=0, le=60)
    competitive_points_per_correct: int = Field(ge=0, le=10000)
    competitive_speed_bonus_enabled: bool
    competitive_speed_bonus_max_points: int = Field(ge=0, le=10000)
    competitive_ingame_disconnect_grace_seconds: int = Field(ge=1, le=600)
    competitive_heartbeat_timeout_seconds: int = Field(ge=1, le=600)

    # Phase 6 — Matchmaking & Queue Engine
    competitive_mmr_expansion_seconds: List[int] = Field(min_length=0, max_length=10)
    competitive_mmr_expansion_radius: List[int] = Field(min_length=0, max_length=10)
    competitive_matchmaking_accept_seconds: int = Field(ge=1, le=300)
    competitive_min_match_quality_score: int = Field(ge=0, le=100)
    competitive_language_fallback_seconds: int = Field(ge=0, le=600)
    competitive_queue_default_wait_estimate_sec: int = Field(ge=0, le=3600)

    # Phase 7 — Ranking, seasons & leagues (season lifecycle)
    competitive_season_ending_soon_hours: int = Field(ge=1, le=8760)

    # Phase 11 Part B/C — Club reputation & club-vs-club battles
    competitive_club_reputation_battle_win: int = Field(ge=0, le=1000)
    competitive_club_reputation_activity_daily: int = Field(ge=0, le=100)
    competitive_club_reputation_achievement: int = Field(ge=0, le=1000)
    competitive_club_reputation_abuse_report: int = Field(ge=-1000, le=0)
    competitive_club_battle_team_size_default: int = Field(ge=1, le=50)
    competitive_club_battle_challenge_expiry_hours: int = Field(ge=1, le=336)
    competitive_club_rating_victory_gain: int = Field(ge=0, le=1000)
    competitive_club_rating_defeat_loss: int = Field(ge=0, le=1000)
    competitive_club_rating_protection_battles: int = Field(ge=0, le=100)
    competitive_club_rating_floor: int = Field(ge=0, le=5000)
    competitive_club_battle_ep_reward_winner: int = Field(ge=0, le=100000)
    competitive_club_battle_ep_reward_participation: int = Field(ge=0, le=100000)
    competitive_club_battle_xp_reward_winner: int = Field(ge=0, le=10000)
    competitive_club_battle_xp_reward_participation: int = Field(ge=0, le=10000)

    # Phase 12 — Replay System & AI Match Analysis (grade thresholds, accuracy %)
    competitive_grade_a_plus_min_accuracy: int = Field(ge=0, le=100)
    competitive_grade_a_min_accuracy: int = Field(ge=0, le=100)
    competitive_grade_b_plus_min_accuracy: int = Field(ge=0, le=100)
    competitive_grade_b_min_accuracy: int = Field(ge=0, le=100)
    competitive_grade_c_min_accuracy: int = Field(ge=0, le=100)
    competitive_grade_d_min_accuracy: int = Field(ge=0, le=100)
    competitive_replay_default_visibility: str

    # Phase 13 — Ranked Ladder V2
    competitive_placement_matches_required: int = Field(ge=1, le=100)
    competitive_placement_k_factor_multiplier: float = Field(ge=1, le=10)
    competitive_ranked_match_types: str
    competitive_casual_ep_reward_winner: int = Field(ge=0, le=10000)
    competitive_casual_ep_reward_participation: int = Field(ge=0, le=10000)
    competitive_fair_play_disconnect_penalty: int = Field(ge=-100, le=0)
    competitive_fair_play_report_penalty: int = Field(ge=-100, le=0)
    competitive_fair_play_afk_penalty: int = Field(ge=-100, le=0)
    competitive_fair_play_clean_match_bonus: int = Field(ge=0, le=100)
    competitive_fair_play_min_for_ranked: int = Field(ge=-1000, le=100)
    competitive_inactivity_decay_enabled: bool
    competitive_inactivity_decay_after_days: int = Field(ge=1, le=3650)
    competitive_inactivity_decay_amount: int = Field(ge=0, le=1000)
    competitive_inactivity_decay_floor: int = Field(ge=0, le=5000)
    competitive_ranked_min_account_age_days: int = Field(ge=0, le=3650)
    competitive_ranked_require_onboarding: bool
    competitive_ranked_min_ep_balance: int = Field(ge=0, le=1000000)
    competitive_ranked_require_phone_verified: bool
    competitive_ranked_require_email_verified: bool

    # Phase 14 — Achievements, Titles, Cosmetics & Progression (showcase caps)
    competitive_badge_showcase_max: int = Field(ge=0, le=50)
    competitive_sticker_showcase_max: int = Field(ge=0, le=50)
    competitive_achievement_showcase_max: int = Field(ge=0, le=50)

    # Phase 15 — Events, Missions, Daily Challenges & LiveOps
    competitive_daily_missions_count: int = Field(ge=0, le=20)
    competitive_weekly_missions_count: int = Field(ge=0, le=20)
    competitive_monthly_missions_count: int = Field(ge=0, le=20)
    competitive_mission_free_rerolls_daily: int = Field(ge=0, le=10)
    competitive_mission_free_rerolls_weekly: int = Field(ge=0, le=10)
    competitive_login_streak_grace_hours: int = Field(ge=0, le=72)
    competitive_login_calendar_length: int = Field(ge=1, le=365)
    competitive_event_ending_soon_hours: int = Field(ge=1, le=8760)
    competitive_happy_hour_starting_soon_minutes: int = Field(ge=1, le=1440)
    competitive_mission_almost_done_pct: int = Field(ge=1, le=99)

    # Phase 16 — Production Hardening: feature flags, rate limits, moderation
    feature_battle_royale_enabled: bool
    feature_tournament_enabled: bool
    feature_replay_enabled: bool
    feature_ai_analysis_enabled: bool
    feature_ranked_enabled: bool
    feature_liveops_enabled: bool
    feature_clubs_enabled: bool
    feature_spectator_enabled: bool
    rate_limit_match_creation_per_10s: int = Field(ge=1, le=1000)
    rate_limit_answer_submit_per_10s: int = Field(ge=1, le=1000)
    rate_limit_replay_request_per_10s: int = Field(ge=1, le=1000)
    rate_limit_leaderboard_refresh_per_10s: int = Field(ge=1, le=1000)
    rate_limit_report_submit_per_10s: int = Field(ge=1, le=1000)
    rate_limit_invitation_create_per_10s: int = Field(ge=1, le=1000)
    moderation_default_mute_hours: int = Field(ge=1, le=8760)
    moderation_default_suspension_days: int = Field(ge=1, le=365)

    @field_validator("competitive_disconnect_policy")
    @classmethod
    def _valid_disconnect_policy(cls, v: str) -> str:
        if v not in ("cancel", "forfeit"):
            raise ValueError("competitive_disconnect_policy must be one of cancel|forfeit")
        return v

    @field_validator("competitive_replay_default_visibility")
    @classmethod
    def _valid_replay_visibility(cls, v: str) -> str:
        if v not in ("private", "friends", "club", "public"):
            raise ValueError("competitive_replay_default_visibility must be one of private|friends|club|public")
        return v

    @field_validator("competitive_club_default_max_members")
    @classmethod
    def _valid_club_default_max_members(cls, v: int) -> int:
        if v not in (20, 50, 100, 250, 500):
            raise ValueError("competitive_club_default_max_members must be one of 20|50|100|250|500")
        return v

    @field_validator("competitive_season_default_reset_strategy")
    @classmethod
    def _valid_reset_strategy(cls, v: str) -> str:
        if v not in ("soft", "hard", "percentage"):
            raise ValueError("competitive_season_default_reset_strategy must be one of soft|hard|percentage")
        return v


class BankTransferSettings(BaseModel):
    bank_beneficiary_name: str = Field(min_length=1, max_length=200)
    platform_rib_cib: Optional[str] = Field(default=None, pattern=r"^\d{20}$")
    platform_rib_edahabia: Optional[str] = Field(default=None, pattern=r"^\d{20}$")
    # How many DZD = 1 EUR — Stripe (international CIB payment) settles in
    # EUR, so this is what converts the DZD-priced booking at checkout time.
    dzd_per_eur: float = Field(gt=0, le=1000)


def _validate_escalating_days(v: List[int]) -> List[int]:
    if any(d <= 0 for d in v):
        raise ValueError("Chaque palier doit être un nombre de jours positif.")
    if any(v[i] > v[i + 1] for i in range(len(v) - 1)):
        raise ValueError("Les paliers de suspension doivent être croissants (ex: 2, 5, 10).")
    return v


class BookingPolicySettings(BaseModel):
    """See docs/migrations/067_booking_safety_rules.sql and
    docs/migrations/069_fairness_pass.sql for the full rule write-up."""
    booking_teacher_response_hours: int = Field(ge=1, le=168)
    booking_refusal_block_threshold: int = Field(ge=1, le=10)
    booking_no_response_suspension_days: List[int] = Field(min_length=1, max_length=10)
    booking_no_response_reset_days: int = Field(ge=1, le=365)
    online_no_show_grace_minutes: int = Field(ge=1, le=120)
    # Student no-show — booking-only suspension (never a full account lock,
    # see app/services/booking_safety.py's apply_student_strike). First
    # offense is always a warning regardless of this list — these paliers
    # only apply from the 2nd incident onward.
    student_no_show_suspension_days: List[int] = Field(min_length=1, max_length=10)
    student_no_show_reset_days: int = Field(ge=1, le=365)
    # In-person (at_home/at_student) absence reports auto-resolve in the
    # filer's favor after this many hours if the other party never counters.
    in_person_dispute_auto_resolve_hours: int = Field(ge=1, le=336)
    # cash/transfer/rib_cib/rib_edahabia auto-cancel if an admin never
    # confirms/rejects the payment within this window — never strikes anyone.
    manual_payment_expiry_hours: int = Field(ge=1, le=336)

    @field_validator("booking_no_response_suspension_days")
    @classmethod
    def validate_escalating(cls, v: List[int]) -> List[int]:
        return _validate_escalating_days(v)

    @field_validator("student_no_show_suspension_days")
    @classmethod
    def validate_student_escalating(cls, v: List[int]) -> List[int]:
        return _validate_escalating_days(v)


class KpEconomySettings(BaseModel):
    """Business/EP audit (2026-09-08, migration 110) — every value here was
    previously hardcoded in Python (referral bonuses, boost plan costs) or
    a no-op (commission_percent=0, kp_source_daily_caps=None). Changing one
    of these is the only way any of it takes effect — nothing is decided
    by this schema itself."""
    commission_percent: int = Field(ge=0, le=100)
    kp_boost_cost_7d: int = Field(ge=0)
    kp_boost_cost_30d: int = Field(ge=0)
    kp_boost_cost_90d: int = Field(ge=0)
    kp_referral_referrer_student: int = Field(ge=0)
    kp_referral_referrer_teacher: int = Field(ge=0)
    kp_referral_referrer_parent: int = Field(ge=0)
    kp_referral_referee_student: int = Field(ge=0)
    kp_referral_referee_teacher: int = Field(ge=0)
    kp_referral_referee_parent: int = Field(ge=0)
    # {"<kp_source>": max_ep_per_user_per_day}. Omit a source, or the whole
    # field (null/{}), to leave it uncapped. Valid keys are KpSource values
    # (lesson, quiz, badge, reward, challenge, bonus, referral, homework,
    # evaluation, promo, competitive) — an unknown key is simply never
    # matched by award_kp, not rejected, so a typo silently has no effect
    # rather than breaking the whole settings save.
    kp_source_daily_caps: Optional[Dict[str, int]] = None
    # Monitoring-only threshold (Point 5.3) — a user whose last-24h EP earn
    # total crosses this appears in GET /admin/kp/suspicious for manual
    # review. Never auto-blocks anyone.
    kp_suspicious_daily_threshold: int = Field(ge=1)

    @field_validator("kp_source_daily_caps")
    @classmethod
    def validate_caps_non_negative(cls, v: Optional[Dict[str, int]]) -> Optional[Dict[str, int]]:
        if v and any(cap < 0 for cap in v.values()):
            raise ValueError("Un plafond EP quotidien ne peut pas être négatif.")
        return v


class HomeworkSettings(BaseModel):
    """Migration 113 — previously a hardcoded `le=500` in
    app/schemas/homework.py (HomeworkCreate.kp_reward) with no admin
    control, and no daily-volume limit at all. Enforced in
    app/routers/homework.py's create_homework."""
    homework_kp_reward_max: int = Field(ge=1, le=5000)
    homework_max_per_student_per_day: int = Field(ge=1, le=100)


class SessionValidationSettings(BaseModel):
    """Trust-score engine knobs for the post-lesson validation workflow —
    see app/services/session_validation.py's compute_trust_score. Weights
    don't need to sum to 100 (the engine normalizes by whatever total is
    actually configured), but keeping them close to 100 keeps the two
    threshold fields below intuitive to read as percentages."""
    trust_weight_student_validation: int = Field(ge=0, le=100)
    trust_weight_teacher_confirmation: int = Field(ge=0, le=100)
    trust_weight_session_completed: int = Field(ge=0, le=100)
    trust_weight_online_duration: int = Field(ge=0, le=100)
    trust_weight_gps_proximity: int = Field(ge=0, le=100)
    trust_weight_clean_history: int = Field(ge=0, le=100)
    trust_auto_approve_threshold: int = Field(ge=0, le=100)
    trust_manual_review_threshold: int = Field(ge=0, le=100)
    room_join_minutes_before: int = Field(ge=0, le=1440)
    student_validation_window_hours: int = Field(ge=1, le=336)
    teacher_confirmation_window_hours: int = Field(ge=1, le=336)
    gps_proximity_threshold_meters: int = Field(ge=1, le=50000)
    # Group lessons only (meaningless for individual/1-student sessions) —
    # see app/models/admin.py's PlatformSettings.trust_group_validation_threshold_percent.
    trust_group_validation_threshold_percent: int = Field(ge=1, le=100)

    @field_validator("trust_manual_review_threshold")
    @classmethod
    def _manual_below_auto(cls, v: int, info) -> int:
        auto = info.data.get("trust_auto_approve_threshold")
        if auto is not None and v > auto:
            raise ValueError(
                "trust_manual_review_threshold must be <= trust_auto_approve_threshold "
                "(a session below the review threshold should never score higher than the auto-approve one)."
            )
        return v
