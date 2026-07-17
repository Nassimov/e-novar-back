from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.dependencies import get_admin_user, get_db
from app.models.admin import PlatformSettings
from app.schemas.admin import BookingPolicySettings, PlatformPricingSettings
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
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


@router.get("/pricing")
async def get_pricing_settings(
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    return _serialize(get_platform_settings(db))


@router.put("/pricing")
async def update_pricing_settings(
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
    return _serialize(settings)


@router.get("/booking-policy")
async def get_booking_policy_settings(
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    return _serialize_booking_policy(get_platform_settings(db))


@router.put("/booking-policy")
async def update_booking_policy_settings(
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
    settings.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(settings)
    return _serialize_booking_policy(settings)
