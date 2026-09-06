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
    PlatformPricingSettings,
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
