from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Supabase
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""
    # JWT secret from Supabase dashboard → Settings → API → JWT Secret
    supabase_jwt_secret: str = ""
    database_url: str = ""

    # Supabase Storage
    supabase_storage_bucket: str = "enovar-files"

    # Redis (Railway Redis add-on or external)
    redis_url: str = "redis://localhost:6379/0"

    # Anthropic (Claude)
    anthropic_api_key: str = ""

    # Stripe
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""

    # OneSignal (push notifications)
    onesignal_app_id: str = ""
    onesignal_rest_api_key: str = ""

    # Resend (transactional email)
    resend_api_key: str = ""
    email_from: str = "noreply@enovar.dz"

    # Twilio (SMS)
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""

    # App
    secret_key: str = "super-secret-key-change-in-production"
    app_env: str = "development"
    app_url: str = "http://localhost:8000"
    frontend_url: str = "http://localhost:3000"
    allowed_origins: str = "http://localhost:3000,https://yourdomain.vercel.app"

    @property
    def allowed_origins_list(self) -> List[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
