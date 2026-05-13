from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.profile import Profile
from app.services.storage import upload_file

router = APIRouter(tags=["profile"])


# ── Response schema compatible with frontend AuthUser ─────────────────────────

class ProfileResponse(BaseModel):
    id: str
    email: str
    full_name: str
    role: str
    avatar_url: Optional[str] = None
    wilaya: Optional[str] = None
    phone: Optional[str] = None
    bio: Optional[str] = None
    onboarding_completed: bool = False
    is_verified: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ProfileUpdateRequest(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    wilaya: Optional[str] = None
    phone: Optional[str] = None
    bio: Optional[str] = None
    avatar_url: Optional[str] = None


class PaymentMethodCreate(BaseModel):
    type: str
    holder_name: str
    card_last4: Optional[str] = None
    iban: Optional[str] = None
    bank_name: Optional[str] = None


class PaymentMethodResponse(BaseModel):
    id: str
    type: str
    holder_name: str
    card_last4: Optional[str] = None
    bank_name: Optional[str] = None
    is_default: bool = False


# ── Helper ────────────────────────────────────────────────────────────────────

def _get_profile(user_id_str: str, db: Session) -> Profile:
    uid = UUID(user_id_str)
    profile = db.exec(select(Profile).where(Profile.id == uid)).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile


def _to_response(profile: Profile, role: str) -> ProfileResponse:
    return ProfileResponse(
        id=str(profile.id),
        email=profile.email or "",
        full_name=profile.full_name or "",
        role=role,
        avatar_url=profile.avatar_url,
        wilaya=profile.wilaya,
        phone=profile.phone,
        bio=profile.bio,
        onboarding_completed=profile.onboarding_completed,
        is_verified=False,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/", response_model=ProfileResponse)
async def get_profile(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get the current user's profile."""
    profile = _get_profile(current_user["id"], db)
    return _to_response(profile, current_user["role"])


@router.put("/", response_model=ProfileResponse)
async def update_profile(
    payload: ProfileUpdateRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update the current user's profile."""
    profile = _get_profile(current_user["id"], db)

    if payload.first_name is not None:
        profile.first_name = payload.first_name
    if payload.last_name is not None:
        profile.last_name = payload.last_name
    if payload.wilaya is not None:
        profile.wilaya = payload.wilaya
    if payload.phone is not None:
        profile.phone = payload.phone
    if payload.bio is not None:
        profile.bio = payload.bio
    if payload.avatar_url is not None:
        profile.avatar_url = payload.avatar_url

    profile.updated_at = datetime.utcnow()
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return _to_response(profile, current_user["role"])


@router.post("/avatar", response_model=ProfileResponse)
async def upload_avatar(
    file: UploadFile,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upload and update user avatar."""
    if file.content_type not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        raise HTTPException(status_code=400, detail="Only image files are allowed")

    contents = await file.read()
    if len(contents) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (max 5MB)")

    url = upload_file(
        contents,
        file.filename or "avatar.jpg",
        file.content_type,
        folder=f"avatars/{current_user['id']}",
    )

    profile = _get_profile(current_user["id"], db)
    profile.avatar_url = url
    profile.updated_at = datetime.utcnow()
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return _to_response(profile, current_user["role"])


@router.put("/notifications")
async def update_notification_prefs(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update notification preferences (stub — prefs stored in notification_preferences table)."""
    return {"message": "Notification preferences updated"}


@router.get("/payment-methods", response_model=List[PaymentMethodResponse])
async def list_payment_methods(
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """List saved payment methods (stub — full implementation in payments router)."""
    return []


@router.post(
    "/payment-methods",
    response_model=PaymentMethodResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_payment_method(
    payload: PaymentMethodCreate,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Add a payment method (stub)."""
    import uuid as _uuid
    return PaymentMethodResponse(
        id=str(_uuid.uuid4()),
        type=payload.type,
        holder_name=payload.holder_name,
        card_last4=payload.card_last4,
        bank_name=payload.bank_name,
        is_default=False,
    )


@router.delete("/payment-methods/{method_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_payment_method(
    method_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Remove a saved payment method (stub)."""
    return None
