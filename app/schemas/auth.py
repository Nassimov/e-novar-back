from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator


class RegisterRequest(BaseModel):
    full_name: str = Field(min_length=2, max_length=100)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    role: str = Field(default="student")
    accepted_terms: bool

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        allowed = {"student", "teacher", "parent"}
        if v not in allowed:
            raise ValueError(f"Role must be one of: {allowed}")
        return v

    @field_validator("accepted_terms")
    @classmethod
    def must_accept_terms(cls, v: bool) -> bool:
        if not v:
            raise ValueError("You must accept the terms and conditions")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserBrief(BaseModel):
    id: str
    email: str
    full_name: str
    role: str
    avatar_url: Optional[str] = None
    is_verified: bool = False
    onboarding_completed: bool = False


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: Optional[str] = None
    token_type: str = "bearer"
    user: UserBrief


class OtpVerifyRequest(BaseModel):
    email: EmailStr
    token: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit")
        return v


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class GoogleCallbackParams(BaseModel):
    code: str
    state: Optional[str] = None


class GoogleExchangeRequest(BaseModel):
    """
    Exchange a Google OAuth code for a Supabase session.

    Send EITHER:
    - `code` + optional `code_verifier` — when the frontend has the raw OAuth code
      (PKCE flow: also pass the code_verifier generated at OAuth start)
    - `access_token` + optional `refresh_token` — when the Supabase SDK on the
      frontend already exchanged the code and returned the session tokens
    """
    code: Optional[str] = None
    code_verifier: Optional[str] = None
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    role: str = Field(default="student")

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        allowed = {"student", "teacher", "parent"}
        if v not in allowed:
            raise ValueError(f"Role must be one of: {allowed}")
        return v


class SignInRequest(BaseModel):
    """
    Unified sign-in — choose one method:
    - Email / password : fill `email` + `password`
    - Google (Supabase) : fill `provider="google"` + `access_token`
      (the token returned by Supabase after the OAuth flow)
    """
    # ── email / password ──────────────────────────────────────────────────────
    email: Optional[EmailStr] = None
    password: Optional[str] = None
    # ── google ────────────────────────────────────────────────────────────────
    provider: Optional[str] = None          # "google"
    access_token: Optional[str] = None      # Supabase JWT from Google OAuth
    refresh_token: Optional[str] = None
    # ── common ────────────────────────────────────────────────────────────────
    role: str = Field(default="student")    # used only for first-time Google users

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v != "google":
            raise ValueError("Only 'google' is supported as provider")
        return v

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        allowed = {"student", "teacher", "parent"}
        if v not in allowed:
            raise ValueError(f"Role must be one of: {allowed}")
        return v
