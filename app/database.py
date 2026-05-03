from __future__ import annotations

from typing import Generator, Optional

from sqlmodel import Session, SQLModel, create_engine
from supabase import Client, create_client

from app.config import get_settings

settings = get_settings()

# Engine is created lazily so missing DATABASE_URL doesn't crash on import
_engine = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(
            settings.database_url,
            echo=settings.app_env == "development",
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=20,
        )
    return _engine


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(get_engine())


def get_session() -> Generator[Session, None, None]:
    with Session(get_engine()) as session:
        yield session


# Supabase clients (lazy)
_supabase_anon: Optional[Client] = None
_supabase_service: Optional[Client] = None


def get_supabase_anon() -> Client:
    global _supabase_anon
    if _supabase_anon is None:
        _supabase_anon = create_client(settings.supabase_url, settings.supabase_anon_key)
    return _supabase_anon


def get_supabase_service() -> Client:
    global _supabase_service
    if _supabase_service is None:
        _supabase_service = create_client(
            settings.supabase_url, settings.supabase_service_role_key
        )
    return _supabase_service
