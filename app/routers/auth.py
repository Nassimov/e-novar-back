from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from app.config import get_settings
from app.dependencies import get_current_user, get_db
from app.schemas.auth import (
    ForgotPasswordRequest,
    GoogleExchangeRequest,
    LoginRequest,
    OtpVerifyRequest,
    RefreshTokenRequest,
    RegisterRequest,
    ResetPasswordRequest,
    SignInRequest,
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
            phone=payload.phone,
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
        phone=payload.phone,
        db=db,
    )

    try:
        from app.workers.email_tasks import send_welcome_email
        send_welcome_email.delay(payload.email, payload.full_name or "")
    except Exception:
        pass

    try:
        from app.services.onesignal import register_user
        register_user(str(profile.id), email=payload.email, role=payload.role)
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
            onboarding_completed=profile.onboarding_completed,
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

    try:
        from app.services.onesignal import register_user
        register_user(str(profile.id), email=profile.email or payload.email, role=role)
    except Exception:
        pass

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
            onboarding_completed=profile.onboarding_completed,
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
            onboarding_completed=profile.onboarding_completed,
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


@router.post("/google/exchange", response_model=TokenResponse)
async def google_exchange(payload: GoogleExchangeRequest, db: Session = Depends(get_db)):
    """
    Exchange a Google OAuth code **or** existing Supabase tokens for a session.

    **Mode 1 — raw OAuth code (PKCE):**
    Send `code` + `code_verifier` (the verifier your app generated before opening
    the OAuth browser). Supabase exchanges the code and returns a fresh session.

    **Mode 2 — already-exchanged tokens:**
    If the Supabase SDK on your frontend already called `exchangeCodeForSession()`
    and gave you `access_token` / `refresh_token`, send those directly.

    In both cases the user is **auto-registered** on first login (no separate
    registration step needed for Google users).
    """
    supabase_user = None
    session_access = None
    session_refresh = None

    if payload.code:
        # ── Mode 1: exchange the OAuth code ───────────────────────────────────
        try:
            result = auth_service.exchange_google_code(
                payload.code, payload.code_verifier
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Code exchange failed: {exc}")

        if not result.session or not result.user:
            raise HTTPException(status_code=400, detail="Code exchange returned no session")

        supabase_user = result.user
        session_access = result.session.access_token
        session_refresh = result.session.refresh_token

    elif payload.access_token:
        # ── Mode 2: validate already-obtained Supabase tokens ─────────────────
        try:
            user_resp = auth_service.get_supabase_user_from_token(payload.access_token)
        except Exception as exc:
            raise HTTPException(status_code=401, detail=f"Invalid access token: {exc}")

        if not user_resp.user:
            raise HTTPException(status_code=401, detail="Token validation returned no user")

        supabase_user = user_resp.user
        session_access = payload.access_token
        session_refresh = payload.refresh_token

    else:
        raise HTTPException(
            status_code=422,
            detail="Provide either 'code' (OAuth code) or 'access_token' (Supabase token)",
        )

    # ── resolve role & name from Supabase metadata ────────────────────────────
    role = (
        supabase_user.app_metadata.get("role")
        or supabase_user.user_metadata.get("role")
        or payload.role
    )
    full_name = (
        supabase_user.user_metadata.get("full_name")
        or supabase_user.user_metadata.get("name")
        or (supabase_user.email or "").split("@")[0]
    )
    parts = full_name.strip().split(" ", 1)
    avatar_url = supabase_user.user_metadata.get("avatar_url") or supabase_user.user_metadata.get("picture")

    # ── get or create local profile (auto-registration) ───────────────────────
    try:
        profile = auth_service.get_or_create_profile(
            supabase_id=supabase_user.id,
            email=supabase_user.email or "",
            role=role,
            first_name=parts[0],
            last_name=parts[1] if len(parts) > 1 else "",
            db=db,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Profile creation failed: {exc}")

    if avatar_url and not profile.avatar_url:
        try:
            profile.avatar_url = avatar_url
            db.add(profile)
            db.commit()
            db.refresh(profile)
        except Exception:
            db.rollback()

    try:
        from app.services.onesignal import register_user
        register_user(str(profile.id), email=profile.email or "", role=role)
    except Exception:
        pass

    return TokenResponse(
        access_token=session_access,
        refresh_token=session_refresh,
        user=UserBrief(
            id=str(profile.id),
            email=profile.email or supabase_user.email or "",
            full_name=profile.full_name or full_name,
            role=role,
            avatar_url=profile.avatar_url,
            is_verified=True,
            onboarding_completed=profile.onboarding_completed,
        ),
    )


@router.post("/signin", response_model=TokenResponse)
async def signin(payload: SignInRequest, db: Session = Depends(get_db)):
    """
    Unified sign-in endpoint — choose one method:

    **Email / password:**
    ```json
    { "email": "user@example.com", "password": "secret123" }
    ```

    **Google (Supabase token):**
    ```json
    { "provider": "google", "access_token": "<supabase_jwt>", "refresh_token": "<optional>" }
    ```
    The `access_token` is the one returned by Supabase after the Google OAuth flow
    (call `POST /api/auth/google/exchange` first if you only have the raw OAuth code).
    """
    # ── Google path ───────────────────────────────────────────────────────────
    if payload.provider == "google":
        if not payload.access_token:
            raise HTTPException(
                status_code=422,
                detail="'access_token' is required when provider is 'google'",
            )
        try:
            user_resp = auth_service.get_supabase_user_from_token(payload.access_token)
        except Exception as exc:
            raise HTTPException(status_code=401, detail=f"Invalid Google token: {exc}")

        if not user_resp.user:
            raise HTTPException(status_code=401, detail="Token validation failed")

        supabase_user = user_resp.user
        role = (
            supabase_user.app_metadata.get("role")
            or supabase_user.user_metadata.get("role")
            or payload.role
        )
        full_name = (
            supabase_user.user_metadata.get("full_name")
            or supabase_user.user_metadata.get("name")
            or (supabase_user.email or "").split("@")[0]
        )
        parts = full_name.strip().split(" ", 1)
        try:
            profile = auth_service.get_or_create_profile(
                supabase_id=supabase_user.id,
                email=supabase_user.email or "",
                role=role,
                first_name=parts[0],
                last_name=parts[1] if len(parts) > 1 else "",
                db=db,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Profile creation failed: {exc}")

        try:
            from app.services.onesignal import register_user
            register_user(str(profile.id), email=profile.email or "", role=role)
        except Exception:
            pass

        return TokenResponse(
            access_token=payload.access_token,
            refresh_token=payload.refresh_token,
            user=UserBrief(
                id=str(profile.id),
                email=profile.email or supabase_user.email or "",
                full_name=profile.full_name or full_name,
                role=role,
                avatar_url=profile.avatar_url,
                is_verified=True,
                onboarding_completed=profile.onboarding_completed,
            ),
        )

    # ── Email / password path ─────────────────────────────────────────────────
    if not payload.email or not payload.password:
        raise HTTPException(
            status_code=422,
            detail="Provide 'email' + 'password', or 'provider' + 'access_token'",
        )
    try:
        result = auth_service.login_with_supabase(payload.email, payload.password)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid email or password")

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
        email=supabase_user.email or str(payload.email),
        role=role,
        first_name=parts[0],
        last_name=parts[1] if len(parts) > 1 else "",
        db=db,
    )
    try:
        from app.services.onesignal import register_user
        register_user(str(profile.id), email=profile.email or str(payload.email), role=role)
    except Exception:
        pass

    session = result.session
    return TokenResponse(
        access_token=session.access_token if session else "",
        refresh_token=session.refresh_token if session else None,
        user=UserBrief(
            id=str(profile.id),
            email=profile.email or str(payload.email),
            full_name=profile.full_name or full_name,
            role=role,
            avatar_url=profile.avatar_url,
            is_verified=False,
            onboarding_completed=profile.onboarding_completed,
        ),
    )


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
    """Get the current authenticated user's basic info.

    Role is read from user_roles table (authoritative) — not from the JWT.
    This ensures the correct role is returned immediately after onboarding
    completes, even before the Supabase JWT is refreshed.
    """
    from uuid import UUID
    from sqlmodel import select
    from app.models.profile import Profile, UserRole

    uid = UUID(current_user["id"])
    profile = db.exec(select(Profile).where(Profile.id == uid)).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")

    # Role priority: admin > teacher > parent > student
    _priority = {"admin": 4, "teacher": 3, "parent": 2, "student": 1}
    role_rows = db.exec(select(UserRole).where(UserRole.user_id == uid)).all()
    if role_rows:
        role = max(role_rows, key=lambda r: _priority.get(r.role, 0)).role
    else:
        role = current_user["role"]  # fallback to JWT if no row yet

    return UserBrief(
        id=str(profile.id),
        email=profile.email or "",
        full_name=profile.full_name or "",
        role=role,
        avatar_url=profile.avatar_url,
        phone=profile.phone,
        is_verified=False,
        onboarding_completed=profile.onboarding_completed,
    )
