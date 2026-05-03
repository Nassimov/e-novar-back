from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from app.config import get_settings
from app.dependencies import get_current_user, get_db
from app.schemas.auth import (
    ForgotPasswordRequest,
    LoginRequest,
    OtpVerifyRequest,
    RefreshTokenRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenResponse,
    UserBrief,
)
from app.services import auth as auth_service

router = APIRouter(tags=["auth"])
settings = get_settings()


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, db: Session = Depends(get_db)):
    """Register a new user via Supabase Auth and create local profile."""
    try:
        result = auth_service.register_user_in_supabase(
            email=payload.email,
            password=payload.password,
            role=payload.role,
            full_name=payload.full_name,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    supabase_user = result.user
    if supabase_user is None:
        raise HTTPException(status_code=400, detail="Registration failed")

    parts = payload.full_name.strip().split(" ", 1) if payload.full_name else ["", ""]
    profile = auth_service.get_or_create_profile(
        supabase_id=supabase_user.id,
        email=payload.email,
        role=payload.role,
        first_name=parts[0],
        last_name=parts[1] if len(parts) > 1 else "",
        db=db,
    )

    try:
        from app.workers.email_tasks import send_welcome_email
        send_welcome_email.delay(payload.email, payload.full_name or "")
    except Exception:
        pass

    session = result.session
    return TokenResponse(
        access_token=session.access_token if session else "",
        refresh_token=session.refresh_token if session else None,
        user=UserBrief(
            id=str(profile.id),
            email=profile.email or payload.email,
            full_name=profile.full_name or payload.full_name or "",
            role=payload.role,
            avatar_url=profile.avatar_url,
            is_verified=False,
        ),
    )


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, db: Session = Depends(get_db)):
    """Sign in with email and password via Supabase Auth."""
    try:
        result = auth_service.login_with_supabase(payload.email, payload.password)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    supabase_user = result.user
    if supabase_user is None:
        raise HTTPException(status_code=401, detail="Login failed")

    role = (
        supabase_user.app_metadata.get("role")
        or supabase_user.user_metadata.get("role")
        or "student"
    )
    full_name = supabase_user.user_metadata.get("full_name", payload.email.split("@")[0])
    parts = full_name.strip().split(" ", 1)

    profile = auth_service.get_or_create_profile(
        supabase_id=supabase_user.id,
        email=supabase_user.email or payload.email,
        role=role,
        first_name=parts[0],
        last_name=parts[1] if len(parts) > 1 else "",
        db=db,
    )

    session = result.session
    return TokenResponse(
        access_token=session.access_token if session else "",
        refresh_token=session.refresh_token if session else None,
        user=UserBrief(
            id=str(profile.id),
            email=profile.email or payload.email,
            full_name=profile.full_name or full_name,
            role=role,
            avatar_url=profile.avatar_url,
            is_verified=False,
        ),
    )


@router.post("/logout")
async def logout(current_user: Dict[str, Any] = Depends(get_current_user)):
    """Sign out the current user."""
    auth_service.logout_from_supabase(
        current_user["claims"].get("access_token", "")
    )
    return {"message": "Logged out successfully"}


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(payload: RefreshTokenRequest, db: Session = Depends(get_db)):
    """Refresh access token using a refresh token."""
    try:
        result = auth_service.refresh_supabase_token(payload.refresh_token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    supabase_user = result.user
    if supabase_user is None:
        raise HTTPException(status_code=401, detail="Refresh failed")

    role = (
        supabase_user.app_metadata.get("role")
        or supabase_user.user_metadata.get("role")
        or "student"
    )
    full_name = supabase_user.user_metadata.get("full_name", "")
    parts = full_name.strip().split(" ", 1)

    profile = auth_service.get_or_create_profile(
        supabase_id=supabase_user.id,
        email=supabase_user.email or "",
        role=role,
        first_name=parts[0],
        last_name=parts[1] if len(parts) > 1 else "",
        db=db,
    )

    session = result.session
    return TokenResponse(
        access_token=session.access_token if session else "",
        refresh_token=session.refresh_token if session else None,
        user=UserBrief(
            id=str(profile.id),
            email=profile.email or "",
            full_name=profile.full_name or full_name,
            role=role,
            avatar_url=profile.avatar_url,
            is_verified=False,
        ),
    )


@router.post("/forgot-password")
async def forgot_password(payload: ForgotPasswordRequest):
    """Send a password reset email."""
    auth_service.send_password_reset(payload.email)
    return {"message": "If that email is registered, you will receive a reset link"}


@router.post("/verify-otp")
async def verify_otp(payload: OtpVerifyRequest):
    """Verify an OTP code sent to an email."""
    success = auth_service.verify_otp(payload.email, payload.token)
    if not success:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP code")
    return {"message": "OTP verified successfully", "verified": True}


@router.post("/reset-password")
async def reset_password(payload: ResetPasswordRequest):
    """Reset password using token from email."""
    try:
        auth_service.reset_password_with_token(payload.token, payload.new_password)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"message": "Password reset successfully"}


@router.get("/google")
async def google_oauth():
    """Initiate Google OAuth via Supabase."""
    from app.database import get_supabase_anon

    client = get_supabase_anon()
    result = client.auth.sign_in_with_oauth(
        {
            "provider": "google",
            "options": {"redirect_to": f"{settings.app_url}/api/auth/google/callback"},
        }
    )
    return RedirectResponse(url=result.url)


@router.get("/google/callback")
async def google_callback(request: Request, db: Session = Depends(get_db)):
    """Handle Google OAuth callback."""
    code = request.query_params.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    # Supabase handles the PKCE flow; the frontend should complete this
    return RedirectResponse(url=f"{settings.frontend_url}/auth/callback?code={code}")


@router.get("/me", response_model=UserBrief)
async def get_me(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get the current authenticated user's basic info."""
    from uuid import UUID
    from sqlmodel import select
    from app.models.profile import Profile

    profile = db.exec(
        select(Profile).where(Profile.id == UUID(current_user["id"]))
    ).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")

    return UserBrief(
        id=str(profile.id),
        email=profile.email or "",
        full_name=profile.full_name or "",
        role=current_user["role"],
        avatar_url=profile.avatar_url,
        is_verified=False,
    )
