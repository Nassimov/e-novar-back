from __future__ import annotations

"""
Configuration — reads EXCLUSIVELY from environment variables.

On Railway: set values in the service Variables tab.
            pydantic-settings picks them up automatically.
Locally:    create a .env file (copied from .env.example).
            The .env file is optional and ignored if absent.

Priority: environment variables > .env file > default values.
All fields have safe defaults so the app starts even with no vars set
(features that need a key will be skipped at runtime, not at startup).
"""

import os
from functools import lru_cache
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # .env is optional — only used locally. On Railway, env vars are injected directly.
        env_file=".env" if os.path.exists(".env") else None,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Supabase ──────────────────────────────────────────────────────────────
    # Set these in Railway → Variables (copy from Supabase dashboard → Settings → API)
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""
    supabase_jwt_secret: str = ""   # Settings → API → JWT Settings → JWT Secret
    database_url: str = ""          # Settings → Database → URI connection string

    # ── Supabase Storage ──────────────────────────────────────────────────────
    supabase_storage_bucket: str = "enovar-files"

    # ── Redis ─────────────────────────────────────────────────────────────────
    # Railway Redis add-on: copy REDIS_URL from the Redis service Variables tab
    redis_url: str = "redis://localhost:6379/0"

    # ── Anthropic (Claude AI) ─────────────────────────────────────────────────
    anthropic_api_key: str = ""

    # ── Stripe ────────────────────────────────────────────────────────────────
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""

    # ── Chargily Pay (Edahabia/CIB via a real Algeria-native gateway) ──────────
    # One secret key doubles as the API bearer token AND the webhook HMAC key —
    # no separate webhook secret, unlike Stripe. Base URL differs by mode (the
    # "test" segment is part of the path, not just the key prefix); default is
    # Chargily's test/sandbox endpoint — override with the live URL in
    # production once the account is verified for Live Mode.
    chargily_secret_key: str = ""
    chargily_base_url: str = "https://pay.chargily.net/test/api/v2"

    # ── LiveKit (video classroom — WebRTC rooms + access tokens) ──────────────
    # Every join requires a fresh, short-lived, per-user JWT access token
    # (grants are scoped per-room, per-identity, server-signed) — never a raw
    # room name/URL. See app/services/livekit_video.py.
    livekit_url: str = ""       # wss://<project>.livekit.cloud (or self-hosted wss://)
    livekit_api_key: str = ""
    livekit_api_secret: str = ""

    # ── OneSignal (push + email) ──────────────────────────────────────────────
    onesignal_app_id: str = ""
    onesignal_rest_api_key: str = ""

    # ── Resend (kept for medium-term migration, not used currently) ───────────
    resend_api_key: str = ""
    email_from: str = "noreply@enovar.dz"

    # ── Twilio (SMS) ──────────────────────────────────────────────────────────
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""

    # ── App ───────────────────────────────────────────────────────────────────
    secret_key: str = "change-this-in-production"
    app_env: str = "production"     # set to "production" in Railway Variables
    app_url: str = ""               # your Railway domain, e.g. https://xxx.up.railway.app
    frontend_url: str = ""          # your Vercel/frontend domain

    @field_validator("app_url", "frontend_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        # Every call site does f"{settings.frontend_url}/some/path" — a trailing
        # slash in the env var (easy to paste by mistake) silently produces a
        # double slash in every redirect URL and email link built from it.
        return v.rstrip("/")

    # ── Admin (privileged account — credentials stored in Railway env vars only) ─
    # ADMIN_EMAIL=admin@e-novar.com
    # ADMIN_PASSWORD_HASH=$(python -c "from passlib.context import CryptContext; print(CryptContext(schemes=['bcrypt']).hash('YOUR_PASSWORD'))")
    # ADMIN_2FA_SECRET=$(python -c "import pyotp; print(pyotp.random_base32())")
    # ADMIN_JWT_EXPIRE_MINUTES=60
    # ADMIN_JWT_SECRET=<random-256-bit-hex>
    # ── AI Quota ──────────────────────────────────────────────────────────────
    # Free interactive AI questions per user per calendar day.
    # Set AI_FREE_DAILY_QUOTA=0 to disable quota (unlimited for all).
    ai_free_daily_quota: int = 10

    admin_email: str = ""
    admin_password_hash: str = ""   # bcrypt hash of the admin password
    admin_2fa_secret: str = ""      # base32-encoded TOTP secret (standard TOTP, RFC 6238)
    admin_jwt_expire_minutes: int = 60
    admin_jwt_secret: str = ""      # dedicated signing secret for admin JWTs
    # Comma-separated explicit origins (e.g. "https://e-novar.com,http://localhost:5173").
    # The wildcard "*" is no longer used here — *.e-novar.com is handled by regex in main.py.
    allowed_origins: str = ""

    @property
    def allowed_origins_list(self) -> List[str]:
        origins = [o.strip() for o in self.allowed_origins.split(",") if o.strip() and o.strip() != "*"]
        return origins

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
