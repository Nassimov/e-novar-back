from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.teacher import TeacherProfile
from app.models.user import User

router = APIRouter(tags=["onboarding"])


class OnboardingCompleteRequest(BaseModel):
    full_name: Optional[str] = None
    wilaya: Optional[str] = None
    phone: Optional[str] = None
    bio: Optional[str] = None
    # Teacher-specific
    subjects: Optional[List[str]] = None
    levels: Optional[List[str]] = None
    price_per_session: Optional[int] = None
    modes: Optional[List[str]] = None
    experience_years: Optional[int] = None
    # Parent-specific
    child_name: Optional[str] = None
    child_level: Optional[str] = None


class OnboardingStatusResponse(BaseModel):
    completed: bool
    role: str
    missing_fields: List[str]


@router.post("/complete")
async def complete_onboarding(
    payload: OnboardingCompleteRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Complete the onboarding process for a new user."""
    from datetime import datetime

    statement = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(statement).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    # Update common fields
    if payload.full_name:
        user.full_name = payload.full_name
    if payload.wilaya:
        user.wilaya = payload.wilaya
    if payload.phone:
        user.phone = payload.phone
    if payload.bio:
        user.bio = payload.bio

    user.onboarding_completed = True
    user.updated_at = datetime.utcnow()
    db.add(user)

    # Create teacher profile if role is teacher
    if user.role.value == "teacher" and any([
        payload.subjects, payload.levels, payload.price_per_session
    ]):
        stmt = select(TeacherProfile).where(TeacherProfile.user_id == user.id)
        profile = db.exec(stmt).first()
        if profile is None:
            profile = TeacherProfile(user_id=user.id)

        if payload.subjects:
            profile.subjects = json.dumps(payload.subjects)
        if payload.levels:
            profile.levels = json.dumps(payload.levels)
        if payload.price_per_session is not None:
            profile.price_per_session = payload.price_per_session
        if payload.modes:
            profile.modes = json.dumps(payload.modes)
        if payload.experience_years is not None:
            profile.experience_years = payload.experience_years
        profile.bio = payload.bio or profile.bio
        profile.updated_at = datetime.utcnow()
        db.add(profile)

    db.commit()
    return {"message": "Onboarding completed", "role": user.role.value}


@router.get("/status", response_model=OnboardingStatusResponse)
async def get_onboarding_status(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get the onboarding status for the current user."""
    statement = select(User).where(User.supabase_id == current_user["id"])
    user = db.exec(statement).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    missing = []
    if not user.full_name:
        missing.append("full_name")
    if not user.wilaya:
        missing.append("wilaya")
    if not user.phone:
        missing.append("phone")

    if user.role.value == "teacher":
        stmt = select(TeacherProfile).where(TeacherProfile.user_id == user.id)
        profile = db.exec(stmt).first()
        if profile is None or not profile.subjects or profile.subjects == "[]":
            missing.append("subjects")
        if profile is None or not profile.price_per_session:
            missing.append("price_per_session")

    return OnboardingStatusResponse(
        completed=user.onboarding_completed,
        role=user.role.value,
        missing_fields=missing,
    )
