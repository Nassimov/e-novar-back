from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.core.cache import cache_invalidate
from app.dependencies import get_admin_user, get_db
from app.models.admin import PlatformSettings
from app.schemas.admin import (
    BankTransferSettings,
    BookingPolicySettings,
    CompetitiveArenaSettings,
    HomeworkSettings,
    KpEconomySettings,
    PlatformPricingSettings,
    SessionValidationSettings,
)
from app.services.pricing import get_platform_settings

router = APIRouter(tags=["admin-settings"])


def _serialize(s: PlatformSettings) -> dict:
    return {
        "pack5_discount_percent": s.pack5_discount_percent,
        "pack10_discount_percent": s.pack10_discount_percent,
        "group_discount_percent": s.group_discount_percent,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


def _serialize_booking_policy(s: PlatformSettings) -> dict:
    return {
        "booking_teacher_response_hours": s.booking_teacher_response_hours,
        "booking_refusal_block_threshold": s.booking_refusal_block_threshold,
        "booking_no_response_suspension_days": s.booking_no_response_suspension_days,
        "booking_no_response_reset_days": s.booking_no_response_reset_days,
        "online_no_show_grace_minutes": s.online_no_show_grace_minutes,
        "student_no_show_suspension_days": s.student_no_show_suspension_days,
        "student_no_show_reset_days": s.student_no_show_reset_days,
        "in_person_dispute_auto_resolve_hours": s.in_person_dispute_auto_resolve_hours,
        "manual_payment_expiry_hours": s.manual_payment_expiry_hours,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


def _serialize_session_validation(s: PlatformSettings) -> dict:
    return {
        "trust_weight_student_validation": s.trust_weight_student_validation,
        "trust_weight_teacher_confirmation": s.trust_weight_teacher_confirmation,
        "trust_weight_session_completed": s.trust_weight_session_completed,
        "trust_weight_online_duration": s.trust_weight_online_duration,
        "trust_weight_gps_proximity": s.trust_weight_gps_proximity,
        "trust_weight_clean_history": s.trust_weight_clean_history,
        "trust_auto_approve_threshold": s.trust_auto_approve_threshold,
        "trust_manual_review_threshold": s.trust_manual_review_threshold,
        "token_visible_minutes_before": s.token_visible_minutes_before,
        "student_validation_window_hours": s.student_validation_window_hours,
        "teacher_confirmation_window_hours": s.teacher_confirmation_window_hours,
        "gps_proximity_threshold_meters": s.gps_proximity_threshold_meters,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


@router.get("/session-validation")
def get_session_validation_settings(
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Trust-score engine knobs — see app/services/session_validation.py's
    compute_trust_score. Shipped with the session-validation feature itself
    but never exposed via an admin endpoint until now (weights were only
    ever changeable by direct DB access)."""
    return _serialize_session_validation(get_platform_settings(db))


@router.put("/session-validation")
def update_session_validation_settings(
    body: SessionValidationSettings,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    settings = db.get(PlatformSettings, True)
    if settings is None:
        settings = PlatformSettings(id=True)
        db.add(settings)
    for field, value in body.model_dump().items():
        setattr(settings, field, value)
    settings.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(settings)
    return _serialize_session_validation(settings)


@router.get("/pricing")
def get_pricing_settings(
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    return _serialize(get_platform_settings(db))


@router.put("/pricing")
def update_pricing_settings(
    body: PlatformPricingSettings,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    settings = db.get(PlatformSettings, True)
    if settings is None:
        settings = PlatformSettings(id=True)
        db.add(settings)
    settings.pack5_discount_percent = body.pack5_discount_percent
    settings.pack10_discount_percent = body.pack10_discount_percent
    settings.group_discount_percent = body.group_discount_percent
    settings.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(settings)
    cache_invalidate("public:pricing")
    return _serialize(settings)


@router.get("/booking-policy")
def get_booking_policy_settings(
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    return _serialize_booking_policy(get_platform_settings(db))


@router.put("/booking-policy")
def update_booking_policy_settings(
    body: BookingPolicySettings,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    settings = db.get(PlatformSettings, True)
    if settings is None:
        settings = PlatformSettings(id=True)
        db.add(settings)
    settings.booking_teacher_response_hours = body.booking_teacher_response_hours
    settings.booking_refusal_block_threshold = body.booking_refusal_block_threshold
    settings.booking_no_response_suspension_days = body.booking_no_response_suspension_days
    settings.booking_no_response_reset_days = body.booking_no_response_reset_days
    settings.online_no_show_grace_minutes = body.online_no_show_grace_minutes
    settings.student_no_show_suspension_days = body.student_no_show_suspension_days
    settings.student_no_show_reset_days = body.student_no_show_reset_days
    settings.in_person_dispute_auto_resolve_hours = body.in_person_dispute_auto_resolve_hours
    settings.manual_payment_expiry_hours = body.manual_payment_expiry_hours
    settings.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(settings)
    cache_invalidate("public:booking-policy")
    return _serialize_booking_policy(settings)


def _serialize_kp_economy(s: PlatformSettings) -> dict:
    return {
        "commission_percent": s.commission_percent,
        "kp_boost_cost_7d": s.kp_boost_cost_7d,
        "kp_boost_cost_30d": s.kp_boost_cost_30d,
        "kp_boost_cost_90d": s.kp_boost_cost_90d,
        "kp_referral_referrer_student": s.kp_referral_referrer_student,
        "kp_referral_referrer_teacher": s.kp_referral_referrer_teacher,
        "kp_referral_referrer_parent": s.kp_referral_referrer_parent,
        "kp_referral_referee_student": s.kp_referral_referee_student,
        "kp_referral_referee_teacher": s.kp_referral_referee_teacher,
        "kp_referral_referee_parent": s.kp_referral_referee_parent,
        "kp_source_daily_caps": s.kp_source_daily_caps,
        "kp_suspicious_daily_threshold": s.kp_suspicious_daily_threshold,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


@router.get("/kp-economy")
def get_kp_economy_settings(
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    return _serialize_kp_economy(get_platform_settings(db))


@router.put("/kp-economy")
def update_kp_economy_settings(
    body: KpEconomySettings,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    settings = db.get(PlatformSettings, True)
    if settings is None:
        settings = PlatformSettings(id=True)
        db.add(settings)
    settings.commission_percent = body.commission_percent
    settings.kp_boost_cost_7d = body.kp_boost_cost_7d
    settings.kp_boost_cost_30d = body.kp_boost_cost_30d
    settings.kp_boost_cost_90d = body.kp_boost_cost_90d
    settings.kp_referral_referrer_student = body.kp_referral_referrer_student
    settings.kp_referral_referrer_teacher = body.kp_referral_referrer_teacher
    settings.kp_referral_referrer_parent = body.kp_referral_referrer_parent
    settings.kp_referral_referee_student = body.kp_referral_referee_student
    settings.kp_referral_referee_teacher = body.kp_referral_referee_teacher
    settings.kp_referral_referee_parent = body.kp_referral_referee_parent
    settings.kp_source_daily_caps = body.kp_source_daily_caps
    settings.kp_suspicious_daily_threshold = body.kp_suspicious_daily_threshold
    settings.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(settings)
    cache_invalidate("public:kp-economy")
    return _serialize_kp_economy(settings)


def _serialize_bank_transfer(s: PlatformSettings) -> dict:
    return {
        "bank_beneficiary_name": s.bank_beneficiary_name,
        "platform_rib_cib": s.platform_rib_cib,
        "platform_rib_edahabia": s.platform_rib_edahabia,
        "dzd_per_eur": s.dzd_per_eur,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


@router.get("/bank-transfer")
def get_bank_transfer_settings(
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    return _serialize_bank_transfer(get_platform_settings(db))


@router.put("/bank-transfer")
def update_bank_transfer_settings(
    body: BankTransferSettings,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """The platform's own receiving RIBs for the RIB CIB / RIB Edahabia
    manual-transfer payment methods (see app/routers/public.py's
    bank-transfer-info endpoint, which is what the student's booking flow
    actually reads)."""
    settings = db.get(PlatformSettings, True)
    if settings is None:
        settings = PlatformSettings(id=True)
        db.add(settings)
    settings.bank_beneficiary_name = body.bank_beneficiary_name
    settings.platform_rib_cib = body.platform_rib_cib
    settings.platform_rib_edahabia = body.platform_rib_edahabia
    settings.dzd_per_eur = body.dzd_per_eur
    settings.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(settings)
    cache_invalidate("public:bank-transfer-info")
    return _serialize_bank_transfer(settings)


def _serialize_homework(s: PlatformSettings) -> dict:
    return {
        "homework_kp_reward_max": s.homework_kp_reward_max,
        "homework_max_per_student_per_day": s.homework_max_per_student_per_day,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


@router.get("/homework")
def get_homework_settings(
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    return _serialize_homework(get_platform_settings(db))


@router.put("/homework")
def update_homework_settings(
    body: HomeworkSettings,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    settings = db.get(PlatformSettings, True)
    if settings is None:
        settings = PlatformSettings(id=True)
        db.add(settings)
    settings.homework_kp_reward_max = body.homework_kp_reward_max
    settings.homework_max_per_student_per_day = body.homework_max_per_student_per_day
    settings.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(settings)
    cache_invalidate("public:homework-policy")
    return _serialize_homework(settings)


def _serialize_competitive(s: PlatformSettings) -> dict:
    return {
        "competitive_mmr_initial_rating": s.competitive_mmr_initial_rating,
        "competitive_mmr_k_factor": s.competitive_mmr_k_factor,
        "competitive_mmr_streak_bonus_per_win": s.competitive_mmr_streak_bonus_per_win,
        "competitive_mmr_streak_bonus_max": s.competitive_mmr_streak_bonus_max,
        "competitive_mmr_floor": s.competitive_mmr_floor,
        "competitive_mmr_inflation_dampening_threshold": s.competitive_mmr_inflation_dampening_threshold,
        "competitive_mmr_inflation_dampening_factor": s.competitive_mmr_inflation_dampening_factor,
        "competitive_season_default_reset_strategy": s.competitive_season_default_reset_strategy,
        "competitive_season_default_reset_percentage": s.competitive_season_default_reset_percentage,
        "competitive_spectator_max_default": s.competitive_spectator_max_default,
        "competitive_prediction_lock_before_position": s.competitive_prediction_lock_before_position,
        "competitive_prediction_reward_arena_xp": s.competitive_prediction_reward_arena_xp,
        "competitive_prediction_reward_spectator_xp": s.competitive_prediction_reward_spectator_xp,
        "competitive_reaction_rate_limit_per_10s": s.competitive_reaction_rate_limit_per_10s,
        "competitive_chat_rate_limit_per_10s": s.competitive_chat_rate_limit_per_10s,
        "competitive_tournament_match_start_delay_seconds": s.competitive_tournament_match_start_delay_seconds,
        "competitive_tournament_round_advance_grace_minutes": s.competitive_tournament_round_advance_grace_minutes,
        "competitive_club_creation_cost_ep": s.competitive_club_creation_cost_ep,
        "competitive_club_min_level": s.competitive_club_min_level,
        "competitive_club_min_rating": s.competitive_club_min_rating,
        "competitive_club_min_account_age_days": s.competitive_club_min_account_age_days,
        "competitive_club_default_max_members": s.competitive_club_default_max_members,
        "competitive_club_name_min_length": s.competitive_club_name_min_length,
        "competitive_club_name_max_length": s.competitive_club_name_max_length,
        "competitive_question_count_options": s.competitive_question_count_options,
        "competitive_invitation_expiry_minutes": s.competitive_invitation_expiry_minutes,
        "competitive_max_scheduling_days": s.competitive_max_scheduling_days,
        "competitive_disconnect_grace_minutes": s.competitive_disconnect_grace_minutes,
        "competitive_disconnect_policy": s.competitive_disconnect_policy,
        "competitive_reminder_minutes_before": s.competitive_reminder_minutes_before,
        "competitive_max_invitations_per_day": s.competitive_max_invitations_per_day,
        "competitive_max_pending_invitations": s.competitive_max_pending_invitations,
        "competitive_invitation_cooldown_seconds": s.competitive_invitation_cooldown_seconds,
        "competitive_match_countdown_seconds": s.competitive_match_countdown_seconds,
        "competitive_reading_time_seconds": s.competitive_reading_time_seconds,
        "competitive_transition_time_seconds": s.competitive_transition_time_seconds,
        "competitive_points_per_correct": s.competitive_points_per_correct,
        "competitive_speed_bonus_enabled": s.competitive_speed_bonus_enabled,
        "competitive_speed_bonus_max_points": s.competitive_speed_bonus_max_points,
        "competitive_ingame_disconnect_grace_seconds": s.competitive_ingame_disconnect_grace_seconds,
        "competitive_heartbeat_timeout_seconds": s.competitive_heartbeat_timeout_seconds,
        "competitive_mmr_expansion_seconds": s.competitive_mmr_expansion_seconds,
        "competitive_mmr_expansion_radius": s.competitive_mmr_expansion_radius,
        "competitive_matchmaking_accept_seconds": s.competitive_matchmaking_accept_seconds,
        "competitive_min_match_quality_score": s.competitive_min_match_quality_score,
        "competitive_language_fallback_seconds": s.competitive_language_fallback_seconds,
        "competitive_queue_default_wait_estimate_sec": s.competitive_queue_default_wait_estimate_sec,
        "competitive_season_ending_soon_hours": s.competitive_season_ending_soon_hours,
        "competitive_club_reputation_battle_win": s.competitive_club_reputation_battle_win,
        "competitive_club_reputation_activity_daily": s.competitive_club_reputation_activity_daily,
        "competitive_club_reputation_achievement": s.competitive_club_reputation_achievement,
        "competitive_club_reputation_abuse_report": s.competitive_club_reputation_abuse_report,
        "competitive_club_battle_team_size_default": s.competitive_club_battle_team_size_default,
        "competitive_club_battle_challenge_expiry_hours": s.competitive_club_battle_challenge_expiry_hours,
        "competitive_club_rating_victory_gain": s.competitive_club_rating_victory_gain,
        "competitive_club_rating_defeat_loss": s.competitive_club_rating_defeat_loss,
        "competitive_club_rating_protection_battles": s.competitive_club_rating_protection_battles,
        "competitive_club_rating_floor": s.competitive_club_rating_floor,
        "competitive_club_battle_ep_reward_winner": s.competitive_club_battle_ep_reward_winner,
        "competitive_club_battle_ep_reward_participation": s.competitive_club_battle_ep_reward_participation,
        "competitive_club_battle_xp_reward_winner": s.competitive_club_battle_xp_reward_winner,
        "competitive_club_battle_xp_reward_participation": s.competitive_club_battle_xp_reward_participation,
        "competitive_grade_a_plus_min_accuracy": s.competitive_grade_a_plus_min_accuracy,
        "competitive_grade_a_min_accuracy": s.competitive_grade_a_min_accuracy,
        "competitive_grade_b_plus_min_accuracy": s.competitive_grade_b_plus_min_accuracy,
        "competitive_grade_b_min_accuracy": s.competitive_grade_b_min_accuracy,
        "competitive_grade_c_min_accuracy": s.competitive_grade_c_min_accuracy,
        "competitive_grade_d_min_accuracy": s.competitive_grade_d_min_accuracy,
        "competitive_replay_default_visibility": s.competitive_replay_default_visibility,
        "competitive_placement_matches_required": s.competitive_placement_matches_required,
        "competitive_placement_k_factor_multiplier": s.competitive_placement_k_factor_multiplier,
        "competitive_ranked_match_types": s.competitive_ranked_match_types,
        "competitive_casual_ep_reward_winner": s.competitive_casual_ep_reward_winner,
        "competitive_casual_ep_reward_participation": s.competitive_casual_ep_reward_participation,
        "competitive_fair_play_disconnect_penalty": s.competitive_fair_play_disconnect_penalty,
        "competitive_fair_play_report_penalty": s.competitive_fair_play_report_penalty,
        "competitive_fair_play_afk_penalty": s.competitive_fair_play_afk_penalty,
        "competitive_fair_play_clean_match_bonus": s.competitive_fair_play_clean_match_bonus,
        "competitive_fair_play_min_for_ranked": s.competitive_fair_play_min_for_ranked,
        "competitive_inactivity_decay_enabled": s.competitive_inactivity_decay_enabled,
        "competitive_inactivity_decay_after_days": s.competitive_inactivity_decay_after_days,
        "competitive_inactivity_decay_amount": s.competitive_inactivity_decay_amount,
        "competitive_inactivity_decay_floor": s.competitive_inactivity_decay_floor,
        "competitive_ranked_min_account_age_days": s.competitive_ranked_min_account_age_days,
        "competitive_ranked_require_onboarding": s.competitive_ranked_require_onboarding,
        "competitive_ranked_min_ep_balance": s.competitive_ranked_min_ep_balance,
        "competitive_ranked_require_phone_verified": s.competitive_ranked_require_phone_verified,
        "competitive_ranked_require_email_verified": s.competitive_ranked_require_email_verified,
        "competitive_badge_showcase_max": s.competitive_badge_showcase_max,
        "competitive_sticker_showcase_max": s.competitive_sticker_showcase_max,
        "competitive_achievement_showcase_max": s.competitive_achievement_showcase_max,
        "competitive_daily_missions_count": s.competitive_daily_missions_count,
        "competitive_weekly_missions_count": s.competitive_weekly_missions_count,
        "competitive_monthly_missions_count": s.competitive_monthly_missions_count,
        "competitive_mission_free_rerolls_daily": s.competitive_mission_free_rerolls_daily,
        "competitive_mission_free_rerolls_weekly": s.competitive_mission_free_rerolls_weekly,
        "competitive_login_streak_grace_hours": s.competitive_login_streak_grace_hours,
        "competitive_login_calendar_length": s.competitive_login_calendar_length,
        "competitive_event_ending_soon_hours": s.competitive_event_ending_soon_hours,
        "competitive_happy_hour_starting_soon_minutes": s.competitive_happy_hour_starting_soon_minutes,
        "competitive_mission_almost_done_pct": s.competitive_mission_almost_done_pct,
        "feature_battle_royale_enabled": s.feature_battle_royale_enabled,
        "feature_tournament_enabled": s.feature_tournament_enabled,
        "feature_replay_enabled": s.feature_replay_enabled,
        "feature_ai_analysis_enabled": s.feature_ai_analysis_enabled,
        "feature_ranked_enabled": s.feature_ranked_enabled,
        "feature_liveops_enabled": s.feature_liveops_enabled,
        "feature_clubs_enabled": s.feature_clubs_enabled,
        "feature_spectator_enabled": s.feature_spectator_enabled,
        "rate_limit_match_creation_per_10s": s.rate_limit_match_creation_per_10s,
        "rate_limit_answer_submit_per_10s": s.rate_limit_answer_submit_per_10s,
        "rate_limit_replay_request_per_10s": s.rate_limit_replay_request_per_10s,
        "rate_limit_leaderboard_refresh_per_10s": s.rate_limit_leaderboard_refresh_per_10s,
        "rate_limit_report_submit_per_10s": s.rate_limit_report_submit_per_10s,
        "rate_limit_invitation_create_per_10s": s.rate_limit_invitation_create_per_10s,
        "moderation_default_mute_hours": s.moderation_default_mute_hours,
        "moderation_default_suspension_days": s.moderation_default_suspension_days,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


@router.get("/competitive")
def get_competitive_settings(
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Phase 7's MMR Calculator knobs (migration 082 — shipped without an
    admin endpoint until now, only league/season CRUD existed) folded
    together with Phase 8's spectator/prediction/reaction/chat knobs
    (migration 083) into one section — see CompetitiveArenaSettings'
    docstring for why they're combined rather than split into a separate
    /competitive-mmr section."""
    return _serialize_competitive(get_platform_settings(db))


@router.put("/competitive")
def update_competitive_settings(
    body: CompetitiveArenaSettings,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    settings = db.get(PlatformSettings, True)
    if settings is None:
        settings = PlatformSettings(id=True)
        db.add(settings)
    for field, value in body.model_dump().items():
        setattr(settings, field, value)
    settings.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(settings)
    return _serialize_competitive(settings)
