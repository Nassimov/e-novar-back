from __future__ import annotations

import os
from typing import Any, Dict, Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

# Set test environment variables before importing app modules
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "test-anon-key")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-testing-only")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_dummy")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_test_dummy")
os.environ.setdefault("R2_ACCOUNT_ID", "test-account")
os.environ.setdefault("R2_ACCESS_KEY_ID", "test-access-key")
os.environ.setdefault("R2_SECRET_ACCESS_KEY", "test-secret")
os.environ.setdefault("R2_BUCKET_NAME", "test-bucket")
os.environ.setdefault("R2_PUBLIC_URL", "https://test.r2.dev")
os.environ.setdefault("ONESIGNAL_APP_ID", "test-app-id")
os.environ.setdefault("ONESIGNAL_REST_API_KEY", "test-rest-key")
os.environ.setdefault("RESEND_API_KEY", "test-resend-key")
os.environ.setdefault("ALLOWED_ORIGINS", "http://localhost:3000")

# Import app.main (transitively every router, every model module) BEFORE any
# fixture runs SQLModel.metadata.create_all() — otherwise the metadata is
# still empty and create_all() silently creates zero tables.
import app.main  # noqa: E402,F401


def _register_sqlite_compile_shims() -> None:
    """Phase 16 — the full SQLModel.metadata spans every model in the app
    (not just the ones a given test touches), and several existing models
    (predating this phase) use PostgreSQL-only column types (JSONB, ARRAY)
    that SQLite's DDL compiler has no rendering for at all — create_all()
    fails immediately without this, for ANY test, before Phase 16. Test-
    infrastructure-only: registers a DDL-compile-time fallback so SQLite
    can create the tables; never touches production model files or the
    real Postgres schema."""
    from sqlalchemy.dialects.postgresql import ARRAY, JSONB
    from sqlalchemy.ext.compiler import compiles

    @compiles(JSONB, "sqlite")
    def _compile_jsonb_sqlite(element, compiler, **kw):
        return "JSON"

    @compiles(ARRAY, "sqlite")
    def _compile_array_sqlite(element, compiler, **kw):
        return "JSON"


_register_sqlite_compile_shims()


@pytest.fixture(scope="session")
def engine():
    """Create in-memory SQLite engine for testing."""
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(test_engine)
    return test_engine


@pytest.fixture
def db_session(engine) -> Generator[Session, None, None]:
    """Provide a transactional database session for each test.

    expire_on_commit=False: SQLite (unlike the real Postgres/timestamptz
    backend) does not round-trip timezone-aware datetimes — a value
    written as tz-aware comes back naive on the next SELECT. Without this
    flag, SQLAlchemy's default post-commit expiry forces exactly that
    reload on the next attribute access, so any code under test comparing
    a freshly-committed row's datetime against datetime.now(timezone.utc)
    would hit "can't compare offset-naive and offset-aware datetimes" —
    a test-DB artifact, not a real bug. With it, a still-in-session object
    keeps the Python values it was given until truly re-queried.
    """
    with Session(engine, expire_on_commit=False) as session:
        yield session
        session.rollback()


@pytest.fixture
def mock_redis():
    """Mock Redis client."""
    mock = MagicMock()
    mock.get.return_value = None
    mock.setex.return_value = True
    mock.delete.return_value = 1
    mock.publish.return_value = 0
    return mock


@pytest.fixture
def mock_settings():
    """Mock application settings."""
    settings = MagicMock()
    settings.supabase_url = "https://test.supabase.co"
    settings.supabase_anon_key = "test-anon-key"
    settings.supabase_service_role_key = "test-service-role-key"
    settings.database_url = "sqlite:///:memory:"
    settings.redis_url = "redis://localhost:6379/0"
    settings.secret_key = "test-secret-key"
    settings.app_env = "testing"
    settings.frontend_url = "http://localhost:3000"
    settings.allowed_origins_list = ["http://localhost:3000"]
    return settings


@pytest.fixture
def mock_current_user() -> Dict[str, Any]:
    """Mock authenticated user for testing."""
    return {
        "id": "test-supabase-user-id",
        "email": "test@example.com",
        "role": "student",
        "claims": {
            "sub": "test-supabase-user-id",
            "email": "test@example.com",
        },
    }


@pytest.fixture
def mock_admin_user() -> Dict[str, Any]:
    """Mock admin user for testing."""
    return {
        "id": "admin-supabase-user-id",
        "email": "admin@enovar.dz",
        "role": "admin",
        "claims": {
            "sub": "admin-supabase-user-id",
            "email": "admin@enovar.dz",
        },
    }


@pytest.fixture
def client(db_session: Session, mock_redis, mock_current_user) -> TestClient:
    """Create test client with mocked dependencies."""
    from app.dependencies import get_current_user, get_db, get_redis
    from app.main import app

    def override_get_db():
        yield db_session

    def override_get_current_user():
        return mock_current_user

    def override_get_redis():
        return mock_redis

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    app.dependency_overrides[get_redis] = override_get_redis

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


@pytest.fixture
def admin_client(db_session: Session, mock_redis, mock_admin_user) -> TestClient:
    """Create test client with admin user."""
    from app.dependencies import get_current_user, get_db, get_redis
    from app.main import app

    def override_get_db():
        yield db_session

    def override_get_admin_user():
        return mock_admin_user

    def override_get_redis():
        return mock_redis

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_admin_user
    app.dependency_overrides[get_redis] = override_get_redis

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


@pytest.fixture
def sample_user(db_session: Session):
    """Create a sample user in the test database."""
    from app.models.user import User, UserRole

    user = User(
        supabase_id="test-supabase-user-id",
        email="test@example.com",
        full_name="Test User",
        role=UserRole.student,
        is_active=True,
        is_verified=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def sample_teacher(db_session: Session):
    """Create a sample teacher user and profile."""
    import json
    from app.models.teacher import TeacherProfile
    from app.models.user import User, UserRole

    user = User(
        supabase_id="test-teacher-supabase-id",
        email="teacher@example.com",
        full_name="Test Teacher",
        role=UserRole.teacher,
        is_active=True,
        is_verified=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    profile = TeacherProfile(
        user_id=user.id,
        subjects=json.dumps(["Mathématiques", "Physique"]),
        levels=json.dumps(["3ème AS", "2ème AS"]),
        price_per_session=2000,
        modes=json.dumps(["online"]),
        is_approved=True,
        is_verified=True,
        rating=4.5,
        reviews_count=10,
    )
    db_session.add(profile)
    db_session.commit()
    db_session.refresh(profile)

    return {"user": user, "profile": profile}
