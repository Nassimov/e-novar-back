from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi

from app.config import get_settings
from app.core.exceptions import register_exception_handlers

logger = logging.getLogger(__name__)
settings = get_settings()

# ── OpenAPI tag groups ────────────────────────────────────────────────────────
TAGS_METADATA = [
    {
        "name": "Auth",
        "description": (
            "Register, login, logout, OAuth, OTP, and token refresh. "
            "All protected endpoints require a **Bearer** JWT issued by Supabase Auth. "
            "Click **Authorize** at the top of this page and paste your access token."
        ),
    },
    {
        "name": "Profile",
        "description": "Read and update the authenticated user's profile (name, avatar, wilaya, preferences).",
    },
    {
        "name": "Onboarding",
        "description": (
            "Multi-step onboarding wizard for students, teachers, and parents. "
            "Submit each step individually; the final step marks onboarding as complete."
        ),
    },
    {
        "name": "Teachers",
        "description": (
            "Public teacher catalogue with filtering (subject, wilaya, price, rating, mode). "
            "Also covers teacher-specific actions: slot management, wallet, withdrawals, statistics."
        ),
    },
    {
        "name": "Bookings",
        "description": (
            "Full booking lifecycle: create → confirm → complete → cancel. "
            "Formulas: `single`, `pack5`, `pack10`. Modes: `online`, `at_student`, `at_home`."
        ),
    },
    {
        "name": "Payments",
        "description": (
            "Payment processing (CIB, Edahabia, BaridiMob, Visa, transfer, cash). "
            "Invoice download, payment history, and promo-code application."
        ),
    },
    {
        "name": "Sessions",
        "description": (
            "Session management and live-classroom lifecycle. "
            "Statuses: `scheduled` → `waiting` → `live` → `completed` | `no_show` | `cancelled`."
        ),
    },
    {
        "name": "Homework",
        "description": (
            "Teacher assigns homework; student submits; teacher grades. "
            "Includes hint system and automatic KP award via Supabase trigger."
        ),
    },
    {
        "name": "Evaluations",
        "description": "Post-session formal evaluations sent by teachers to students (score 0–20, skill breakdown).",
    },
    {
        "name": "Messages",
        "description": (
            "Persistent chat between users (student ↔ teacher, parent ↔ teacher). "
            "Real-time delivery via WebSocket `wss://<host>/ws/messages?token=<jwt>`."
        ),
    },
    {
        "name": "Notifications",
        "description": (
            "In-app notification centre. "
            "Real-time push via WebSocket `wss://<host>/ws/notifications?token=<jwt>`. "
            "User preferences control push/email/SMS channels."
        ),
    },
    {
        "name": "E-NOVAR Points",
        "description": (
            "Gamification engine: KP balance, transaction history, level progression (7 levels). "
            "Points are awarded automatically via Supabase triggers on booking completion, "
            "homework grades, challenge approvals, and referral validation."
        ),
    },
    {
        "name": "Badges",
        "description": "Badge catalogue and per-user progress tracking. Badges unlock automatically when conditions are met.",
    },
    {
        "name": "Challenges",
        "description": (
            "Time-limited challenges for students and teachers. "
            "Submit proof (image/PDF), admin reviews, KP awarded on approval."
        ),
    },
    {
        "name": "Store",
        "description": (
            "EP Marketplace — spend KP on powerups, digital rewards, physical prizes, services, and travel. "
            "Categories: `powerups`, `digital`, `physical`, `services`, `travel`."
        ),
    },
    {
        "name": "Referrals",
        "description": "Referral programme — share a unique code, earn KP when the invited user completes their first booking.",
    },
    {
        "name": "Favorites",
        "description": "Students can save/remove favourite teachers.",
    },
    {
        "name": "Parent",
        "description": (
            "Parent-monitoring features: link to a child account via invite code, "
            "view child sessions, homework, KP progress, and payment history."
        ),
    },
    {
        "name": "AI Tutor",
        "description": (
            "AI-powered tutoring assistant (Claude). Maintains conversation history per subject. "
            "Also exposes AI progress tracking and practice attempt recording."
        ),
    },
    {
        "name": "Catalogs",
        "description": "Public read-only catalogues: subjects, education levels, Algerian wilayas.",
    },
    {
        "name": "Files",
        "description": "Supabase Storage upload/download proxy for avatars, diplomas, homework attachments, and invoices.",
    },
    {
        "name": "Admin — Users",
        "description": "Admin: list, suspend, delete user accounts. View activity and analytics.",
    },
    {
        "name": "Admin — Teachers",
        "description": "Admin: validate teacher applications, approve/suspend, review uploaded diplomas.",
    },
    {
        "name": "Admin — Reviews",
        "description": "Admin: moderation queue for flagged reviews. Approve, hide, or remove.",
    },
    {
        "name": "Admin — Challenges",
        "description": "Admin: review challenge proof submissions, approve/decline, trigger KP award.",
    },
    {
        "name": "Admin — Promos",
        "description": "Admin: create and manage promotional discount codes.",
    },
    {
        "name": "Admin — Content",
        "description": "Admin: manage the learning catalogue (subjects, levels, goals) and CMS pages (terms, privacy, help).",
    },
    {
        "name": "Admin — Stats",
        "description": "Admin: platform KPIs — total users, revenue, sessions completed, average rating, activity feed.",
    },
    {
        "name": "Admin — Messages",
        "description": "Admin: surveillance des messages — liste des messages signalés (hors-plateforme, contacts externes), avertissements aux expéditeurs.",
    },
    {
        "name": "Admin — Questions",
        "description": (
            "Admin: full question-bank management. "
            "Create, edit, soft-delete, restore, and bulk-import questions (CSV/JSON). "
            "Validation workflow: draft → pending_review → approved | rejected. "
            "Analytics endpoint for usage, error-rate, and subject coverage."
        ),
    },
    {
        "name": "Health",
        "description": "Service health check — used by Railway's health probe.",
    },
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Enovar API v1.0.0 ...")
    from app.core import domain_events_bootstrap
    domain_events_bootstrap.register()
    yield
    logger.info("Shutting down Enovar API ...")
    from app.core.redis import close_redis
    close_redis()


app = FastAPI(
    title="E-NOVAR API",
    version="1.0.0",
    description="""
## Algeria's premier tutoring platform

Connect students with qualified teachers for online and in-person tutoring sessions.

### Authentication
Every protected endpoint requires a **Supabase JWT** in the `Authorization` header:
```
Authorization: Bearer <access_token>
```
1. Call `POST /api/auth/login` to get your token.
2. Click the **Authorize** button at the top of this page.
3. Enter `Bearer <your_token>` and click **Authorize**.

### User roles
| Role | Access |
|------|--------|
| `student` | Search teachers, book sessions, homework, gamification |
| `teacher` | Manage availability, sessions, homework, wallet |
| `parent` | Monitor child's sessions, progress, payments |
| `admin` | Full platform management |

### Real-time WebSockets
| Endpoint | Purpose |
|----------|---------|
| `wss://<host>/ws/messages?token=<jwt>` | Live chat |
| `wss://<host>/ws/notifications?token=<jwt>` | Push notifications |

### Database
PostgreSQL hosted on **Supabase**. Schema is defined in `database-schema.sql`.
Business logic (KP triggers, rating recompute, homework KP) runs as PostgreSQL functions inside Supabase.
""",
    openapi_tags=TAGS_METADATA,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    swagger_ui_parameters={
        "persistAuthorization": True,         # token survives page refresh
        "displayRequestDuration": True,       # shows ms per request
        "filter": True,                       # search bar above the endpoint list
        "syntaxHighlight.theme": "monokai",
        "tryItOutEnabled": True,              # "Try it out" open by default
        "docExpansion": "list",               # show all tags collapsed but listed
        "defaultModelsExpandDepth": 2,
        "deepLinking": True,                  # shareable anchor links per endpoint
    },
    contact={
        "name": "E-NOVAR Support",
        "email": "nacimmessi1010@gmail.com",
    },
    license_info={
        "name": "Proprietary",
    },
    lifespan=lifespan,
)

# ── Middleware ─────────────────────────────────────────────────────────────────
# allow_credentials=True + allow_origins=["*"] is rejected by browsers.
# Instead we use a regex that matches every *.e-novar.com subdomain and localhost
# so prod / preprod / recette / local all work without manually listing each origin.
_CORS_ORIGIN_REGEX = (
    r"^https?://localhost(:\d+)?$"                      # local dev (any port)
    r"|^https://e-novar\.com$"                          # production apex
    r"|^https://[a-z0-9-]+\.e-novar\.com$"              # preprod, recette, any branch
    r"|^https://[a-z0-9-]+\.nacimmessi1010\.workers\.dev$"  # this project's Workers dev subdomain
    # NOTE: intentionally NOT `[a-z0-9-]+\.workers\.dev` / `[a-z0-9-]+\.pages\.dev` —
    # those are public suffixes anyone can register a free subdomain under, so with
    # allow_credentials=True they'd let any attacker-owned *.pages.dev/*.workers.dev
    # site be treated as a trusted CORS origin. Scoped to this project's own
    # deployments only: e-novar.pages.dev (prod) and <preview-id>.e-novar.pages.dev.
    r"|^https://(?:[a-z0-9-]+\.)?e-novar\.pages\.dev$"  # this project's Cloudflare Pages deployments
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,   # explicit extras from env var
    allow_origin_regex=_CORS_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from app.core.security_headers import SecurityHeadersMiddleware  # noqa: E402
app.add_middleware(SecurityHeadersMiddleware)

register_exception_handlers(app)

# ── Routers ───────────────────────────────────────────────────────────────────
from app.routers.auth import router as auth_router
from app.routers.profile import router as profile_router
from app.routers.onboarding import router as onboarding_router
from app.routers.teachers import router as teachers_router
from app.routers.payments import router as payments_router
from app.routers.chargily_webhook import router as chargily_webhook_router
from app.routers.sessions import router as sessions_router
from app.routers.session_validation import router as session_validation_router
from app.routers.classroom import router as classroom_router
from app.routers.messages import router as messages_router
from app.routers.notifications import router as notifications_router
from app.routers.kp import router as kp_router
from app.routers.challenges import router as challenges_router
from app.routers.homework import router as homework_router
from app.routers.ai import router as ai_router
from app.routers.favorites import router as favorites_router
from app.routers.referrals import router as referrals_router
from app.routers.catalogs import router as catalogs_router
from app.routers.parent import router as parent_router
from app.routers.files import router as files_router
from app.routers.admin.users import router as admin_users_router
from app.routers.admin.admin_accounts import router as admin_accounts_router
from app.routers.admin.teachers import router as admin_teachers_router
from app.routers.admin.reviews import router as admin_reviews_router
from app.routers.admin.challenges import router as admin_challenges_router
from app.routers.admin.promos import router as admin_promos_router
from app.routers.admin.content import router as admin_content_router
from app.routers.admin.stats import router as admin_stats_router
from app.routers.student_dashboard import router as student_dashboard_router
from app.routers.student_homework import router as student_homework_router
from app.routers.student_teachers import router as student_teachers_router
from app.routers.student_practice import router as student_practice_router
from app.routers.student_progress import router as student_progress_router
from app.routers.student_badges import router as student_badges_router
from app.routers.student_leaderboard import router as student_leaderboard_router
from app.routers.teacher_leaderboard import router as teacher_leaderboard_router
from app.routers.teacher_badges import router as teacher_badges_router
from app.routers.admin.questions import router as admin_questions_router
from app.routers.admin.auth import router as admin_auth_router
from app.routers.admin.messages import router as admin_messages_router
from app.routers.store import router as store_router
from app.routers.admin.store import router as admin_store_router
from app.routers.admin.referrals import router as admin_referrals_router
from app.routers.admin.bookings import router as admin_bookings_router
from app.routers.admin.settings import router as admin_settings_router
from app.routers.admin.session_validation import router as admin_session_validation_router
from app.routers.admin.notification_campaigns import router as admin_campaigns_router
from app.routers.admin.competitive import router as admin_competitive_router
from app.routers.promos import router as promos_router
from app.routers.public import router as public_router
from app.routers.competitive.matches import router as competitive_matches_router
from app.routers.competitive.lobby import router as competitive_lobby_router
from app.routers.competitive.invitations import router as competitive_invitations_router
from app.routers.competitive.stats import router as competitive_stats_router
from app.routers.competitive.ranking import router as competitive_ranking_router
from app.routers.competitive.opponents import router as competitive_opponents_router
from app.routers.competitive.schedule import router as competitive_schedule_router
from app.routers.competitive.gameplay import router as competitive_gameplay_router
from app.routers.competitive.analysis import router as competitive_analysis_router
from app.routers.competitive.matchmaking import router as competitive_matchmaking_router
from app.routers.competitive.spectate import router as competitive_spectate_router
from app.routers.competitive.tournaments import router as competitive_tournaments_router
from app.routers.competitive.battle_royale import router as competitive_battle_royale_router
from app.routers.competitive.achievements import router as competitive_achievements_router
from app.routers.competitive.liveops import router as competitive_liveops_router
from app.routers.competitive.moderation import router as competitive_moderation_router
from app.routers.club.clubs import router as clubs_router
from app.routers.club.chat import router as clubs_chat_router
from app.routers.club.social import router as clubs_social_router
from app.routers.club.battles import router as clubs_battles_router
from app.routers.club.seasons import router as clubs_seasons_router
from app.routers.admin.club import router as admin_clubs_router

app.include_router(public_router,        prefix="/api/public",         tags=["Public"])
app.include_router(auth_router,          prefix="/api/auth",           tags=["Auth"])
app.include_router(profile_router,       prefix="/api/profile",        tags=["Profile"])
app.include_router(onboarding_router,    prefix="/api/onboarding",     tags=["Onboarding"])
app.include_router(teachers_router,      prefix="/api/teachers",       tags=["Teachers"])
app.include_router(teacher_leaderboard_router, prefix="/api/teachers", tags=["Teachers"])
app.include_router(teacher_badges_router,      prefix="/api/teacher",  tags=["Teacher"])
app.include_router(payments_router,      prefix="/api/payments",       tags=["Payments"])
app.include_router(chargily_webhook_router, prefix="/api/payments",    tags=["Payments"])
app.include_router(sessions_router,      prefix="/api/sessions",       tags=["Sessions"])
app.include_router(session_validation_router, prefix="/api/sessions",  tags=["Session Validation"])
app.include_router(classroom_router,     prefix="/api/classroom",     tags=["Classroom"])
app.include_router(homework_router,      prefix="/api/homework",       tags=["Homework"])
app.include_router(messages_router,      prefix="/api/messages",       tags=["Messages"])
app.include_router(notifications_router, prefix="/api/notifications",  tags=["Notifications"])
app.include_router(kp_router,            prefix="/api/kp",             tags=["E-NOVAR Points"])
app.include_router(challenges_router,    prefix="/api/challenges",     tags=["Challenges"])
app.include_router(ai_router,            prefix="/api/ai",             tags=["AI Tutor"])
app.include_router(favorites_router,     prefix="/api/favorites",      tags=["Favorites"])
app.include_router(referrals_router,     prefix="/api/referrals",      tags=["Referrals"])
app.include_router(promos_router,        prefix="/api/promos",          tags=["Promos"])
app.include_router(catalogs_router,      prefix="/api/catalogs",       tags=["Catalogs"])
app.include_router(parent_router,        prefix="/api/parent",         tags=["Parent"])
app.include_router(files_router,         prefix="/api/files",          tags=["Files"])
app.include_router(student_dashboard_router, prefix="/api/student",    tags=["Student"])
app.include_router(student_homework_router,  prefix="/api/student",    tags=["Student"])
app.include_router(student_teachers_router,  prefix="/api/student",    tags=["Student"])
app.include_router(student_practice_router,  prefix="/api/student",    tags=["Student"])
app.include_router(student_progress_router,  prefix="/api/student",    tags=["Student"])
app.include_router(student_badges_router,      prefix="/api/student",    tags=["Student"])
app.include_router(student_leaderboard_router, prefix="/api/student",    tags=["Student"])

app.include_router(admin_auth_router,       prefix="/api/admin/auth",       tags=["Admin — Auth"])
app.include_router(admin_users_router,      prefix="/api/admin/users",      tags=["Admin — Users"])
app.include_router(admin_accounts_router,   prefix="/api/admin/accounts",   tags=["Admin — Accounts"])
app.include_router(admin_teachers_router,   prefix="/api/admin/teachers",   tags=["Admin — Teachers"])
app.include_router(admin_reviews_router,    prefix="/api/admin/reviews",    tags=["Admin — Reviews"])
app.include_router(admin_challenges_router, prefix="/api/admin/challenges", tags=["Admin — Challenges"])
app.include_router(admin_promos_router,     prefix="/api/admin/promos",     tags=["Admin — Promos"])
app.include_router(admin_content_router,    prefix="/api/admin/content",    tags=["Admin — Content"])
app.include_router(admin_stats_router,      prefix="/api/admin/stats",      tags=["Admin — Stats"])
app.include_router(admin_questions_router,  prefix="/api/admin/questions",  tags=["Admin — Questions"])
app.include_router(admin_messages_router,   prefix="/api/admin/messages",   tags=["Admin — Messages"])
app.include_router(admin_referrals_router,  prefix="/api/admin/referrals",  tags=["Admin — Referrals"])
app.include_router(admin_bookings_router,   prefix="/api/admin/bookings",   tags=["Admin — Bookings"])
app.include_router(admin_settings_router,   prefix="/api/admin/settings",   tags=["Admin — Settings"])
app.include_router(admin_session_validation_router, prefix="/api/admin/session-validations", tags=["Admin — Session Validation"])
app.include_router(store_router,            prefix="/api/store",            tags=["Store"])
app.include_router(admin_store_router,      prefix="/api/admin/store",      tags=["Admin — Store"])
app.include_router(admin_campaigns_router,  prefix="/api/admin/campaigns",  tags=["Admin — Notification Campaigns"])
app.include_router(admin_competitive_router, prefix="/api/admin/competitive", tags=["Admin — Competitive"])
app.include_router(competitive_matches_router,     prefix="/api/competitive", tags=["Competitive"])
app.include_router(competitive_lobby_router,       prefix="/api/competitive", tags=["Competitive"])
app.include_router(competitive_invitations_router, prefix="/api/competitive", tags=["Competitive"])
app.include_router(competitive_stats_router,       prefix="/api/competitive", tags=["Competitive"])
app.include_router(competitive_ranking_router,     prefix="/api/competitive", tags=["Competitive"])
app.include_router(competitive_opponents_router,   prefix="/api/competitive", tags=["Competitive"])
app.include_router(competitive_schedule_router,    prefix="/api/competitive", tags=["Competitive"])
app.include_router(competitive_gameplay_router,    prefix="/api/competitive", tags=["Competitive"])
app.include_router(competitive_analysis_router,    prefix="/api/competitive", tags=["Competitive"])
app.include_router(competitive_matchmaking_router, prefix="/api/competitive", tags=["Competitive"])
app.include_router(competitive_spectate_router,    prefix="/api/competitive", tags=["Competitive"])
app.include_router(competitive_tournaments_router, prefix="/api/competitive", tags=["Competitive"])
app.include_router(competitive_battle_royale_router, prefix="/api/competitive", tags=["Competitive"])
app.include_router(competitive_achievements_router, prefix="/api/competitive", tags=["Competitive"])
app.include_router(competitive_liveops_router, prefix="/api/competitive", tags=["Competitive"])
app.include_router(competitive_moderation_router, prefix="/api/competitive", tags=["Competitive"])
app.include_router(clubs_router,             prefix="/api",                tags=["Clubs"])
app.include_router(clubs_chat_router,        prefix="/api",                tags=["Clubs"])
app.include_router(clubs_social_router,      prefix="/api",                tags=["Clubs"])
app.include_router(clubs_battles_router,     prefix="/api",                tags=["Clubs"])
app.include_router(clubs_seasons_router,     prefix="/api",                tags=["Clubs"])
app.include_router(admin_clubs_router,       prefix="/api/admin/clubs",    tags=["Admin — Clubs"])


# ── Custom OpenAPI schema (adds Bearer security globally) ─────────────────────
def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        contact=app.contact,
        license_info=app.license_info,
        tags=TAGS_METADATA,
        routes=app.routes,
    )

    # Inject Bearer JWT security scheme so the Authorize button appears globally
    schema.setdefault("components", {}).setdefault("securitySchemes", {})
    schema["components"]["securitySchemes"]["BearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": (
            "Supabase JWT. Get it from `POST /api/auth/login` → `access_token`. "
            "Format: `Bearer <token>`"
        ),
    }

    # Apply the security scheme to every operation globally
    for path_data in schema.get("paths", {}).values():
        for operation in path_data.values():
            if isinstance(operation, dict):
                operation.setdefault("security", [{"BearerAuth": []}])

    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi  # type: ignore[method-assign]


# ── Root redirect ─────────────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
async def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/docs")


# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/health", tags=["Health"], summary="Service health check", include_in_schema=False)
async def health_check():
    """Minimal probe for Railway — no DB, no Redis, no env vars required."""
    import os
    return {"status": "ok", "version": "1.0.0", "env": os.getenv("APP_ENV", "production")}


@app.get("/health/deps", include_in_schema=False)
async def health_deps():
    """Verify key dependencies — used to confirm deploys."""
    import supabase as _sb
    from app.config import get_settings as _gs
    cfg = _gs()
    result: dict = {
        "supabase_version": _sb.__version__,
        "service_key_set": bool(cfg.supabase_service_role_key),
        "jwt_secret_set": bool(cfg.supabase_jwt_secret),
        "supabase_url_set": bool(cfg.supabase_url),
    }
    try:
        client = _sb.create_client(cfg.supabase_url, cfg.supabase_service_role_key)
        result["service_client"] = "ok"
    except Exception as e:
        result["service_client"] = f"ERROR: {type(e).__name__}: {e}"

    # Phase 16 — Production Hardening: Redis + Celery worker liveness,
    # so this one endpoint covers every dependency the spec's "Health
    # Checks" section names (API/DB/Redis/Workers) except Realtime, which
    # is this same FastAPI process (no separate service to probe).
    try:
        from app.core.redis import get_redis_client
        get_redis_client().ping()
        result["redis"] = "ok"
    except Exception as e:
        result["redis"] = f"ERROR: {type(e).__name__}: {e}"

    try:
        from app.workers.celery_app import celery_app
        pings = celery_app.control.ping(timeout=1.0)
        result["celery_workers"] = f"ok ({len(pings)} responding)" if pings else "no workers responding"
    except Exception as e:
        result["celery_workers"] = f"ERROR: {type(e).__name__}: {e}"

    try:
        from sqlmodel import Session, select
        from app.database import get_engine
        with Session(get_engine()) as db:
            db.exec(select(1))
        result["database"] = "ok"
    except Exception as e:
        result["database"] = f"ERROR: {type(e).__name__}: {e}"

    return result


# ── WebSocket connection manager ──────────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, list[WebSocket]] = {}

    async def connect(self, user_id: str, websocket: WebSocket):
        await websocket.accept()
        if user_id not in self.active_connections:
            self.active_connections[user_id] = []
        self.active_connections[user_id].append(websocket)

    def disconnect(self, user_id: str, websocket: WebSocket):
        if user_id in self.active_connections:
            self.active_connections[user_id].remove(websocket)
            if not self.active_connections[user_id]:
                del self.active_connections[user_id]

    async def send_to_user(self, user_id: str, data: Dict[str, Any]):
        if user_id in self.active_connections:
            disconnected = []
            for ws in self.active_connections[user_id]:
                try:
                    await ws.send_json(data)
                except Exception:
                    disconnected.append(ws)
            for ws in disconnected:
                self.active_connections[user_id].remove(ws)

    async def broadcast(self, data: Dict[str, Any]):
        for user_id in list(self.active_connections.keys()):
            await self.send_to_user(user_id, data)


message_manager = ConnectionManager()
notification_manager = ConnectionManager()

# Reuses the generic queue-based delivery manager from the chat system, but
# keyed by classroom room_key (not user_id) — every participant connected to
# the same room shares one broadcast "channel".
from app.core.connections import ChatConnectionManager as _ChatConnectionManager  # noqa: E402
classroom_connections = _ChatConnectionManager()
competitive_connections = _ChatConnectionManager()
club_connections = _ChatConnectionManager()


async def _resolve_ws_user(token: str) -> tuple[str, str]:
    """Return (user_id, email) from a WS token, or raise ValueError on auth failure.

    Mirrors the two-path strategy in get_current_user():
      1. Fast local HMAC decode (requires SUPABASE_JWT_SECRET).
      2. Supabase Auth API fallback (always correct, one extra round-trip).
    """
    from app.core.security import decode_supabase_jwt

    if not token:
        raise ValueError("missing token")

    claims = decode_supabase_jwt(token)
    if claims:
        uid = claims.get("sub", "")
        if not uid:
            raise ValueError("missing sub")
        return uid, claims.get("email", "")

    # Fast decode failed — try Supabase Auth API (handles missing/wrong JWT secret)
    logger.warning("WS: local JWT decode failed — falling back to Supabase get_user()")
    try:
        from app.database import get_supabase_service
        resp = get_supabase_service().auth.get_user(token)
        if resp and resp.user:
            return str(resp.user.id), resp.user.email or ""
    except Exception as exc:
        logger.error("WS: Supabase get_user fallback failed: %s", exc)

    raise ValueError("invalid or expired token")


def _extract_ws_token(websocket: WebSocket, token: str) -> str:
    """Prefer the JWT carried as a WS subprotocol over the legacy `?token=`
    query param.

    The frontend now connects with `new WebSocket(url, ["access_token",
    <jwt>])` — the browser sends that as the `Sec-WebSocket-Protocol` request
    header during the handshake instead of putting the token in the URL.
    Query strings routinely end up in reverse-proxy / uvicorn access logs;
    handshake headers generally don't get logged the same way (though be
    aware some proxies can be configured to log arbitrary headers too — this
    reduces exposure, it doesn't guarantee zero exposure everywhere).

    Falls back to the `token` query param so a browser tab still running the
    previous frontend bundle (e.g. mid-deploy) keeps working, and so the
    `wss://…?token=<jwt>` form documented for external/API consumers in the
    OpenAPI docs keeps working too.
    """
    proto_header = websocket.headers.get("sec-websocket-protocol", "")
    if proto_header:
        parts = [p.strip() for p in proto_header.split(",") if p.strip()]
        if len(parts) >= 2 and parts[0] == "access_token" and parts[1]:
            return parts[1]
    return token


@app.websocket("/ws/messages")
async def websocket_messages(websocket: WebSocket, token: str = ""):
    """Real-time messaging channel. Connect with the JWT as a WS subprotocol
    (`new WebSocket(url, ["access_token", jwt])`), or legacy `?token=<jwt>`."""
    import asyncio
    from app.core.connections import chat_connections

    try:
        user_id, _email = await _resolve_ws_user(_extract_ws_token(websocket, token))
    except ValueError as exc:
        logger.info("WS /messages rejected: %s", exc)
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await message_manager.connect(user_id, websocket)
    # Register user in queue-based delivery manager; get the queue back
    send_queue = chat_connections.register(user_id)

    # Register online presence
    try:
        from app.core.redis import get_redis_client
        _r = get_redis_client()
        _r.sadd("chat:online_users", user_id)
    except Exception:
        pass

    async def _touch_presence():
        try:
            from uuid import UUID as _UUID
            from datetime import datetime, timezone
            from sqlmodel import select
            from app.database import get_session
            from app.models.profile import Profile
            with next(get_session()) as _db:
                _p = _db.exec(select(Profile).where(Profile.id == _UUID(user_id))).first()
                if _p:
                    _p.last_seen_at = datetime.now(timezone.utc)
                    _db.add(_p)
                    _db.commit()
        except Exception as _exc:
            logger.debug("presence touch failed: %s", _exc)

    asyncio.create_task(_touch_presence())
    asyncio.create_task(_broadcast_presence_event(user_id, {"type": "user_online", "user_id": user_id}))
    asyncio.create_task(_send_partner_online_statuses(user_id, send_queue))

    stop_evt = asyncio.Event()

    # Single sender task: the ONLY coroutine that calls ws.send_json().
    # All other paths (HTTP handlers, Redis subscriber) enqueue via send_queue.
    async def _sender():
        while True:
            data = await send_queue.get()
            if data is None:  # sentinel from unregister()
                break
            try:
                await websocket.send_json(data)
            except Exception as exc:
                logger.debug("WS send error user=%s: %s", user_id, exc)
                break

    sender_task = asyncio.create_task(_sender())
    sub_task = asyncio.create_task(_chat_redis_subscriber(user_id, send_queue, stop_evt))

    logger.info("WS /messages connected: user=%s", user_id)

    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                mtype = msg.get("type")
                if mtype == "ping":
                    send_queue.put_nowait({"type": "pong"})
                    try:
                        from app.core.redis import get_redis_client
                        get_redis_client().sadd("chat:online_users", user_id)
                    except Exception:
                        pass
                elif mtype == "heartbeat":
                    try:
                        from app.core.redis import get_redis_client
                        get_redis_client().sadd("chat:online_users", user_id)
                    except Exception:
                        pass
                elif mtype == "typing_start":
                    _handle_chat_typing(user_id, msg.get("conv_id", ""), True)
                elif mtype == "typing_stop":
                    _handle_chat_typing(user_id, msg.get("conv_id", ""), False)
            except json.JSONDecodeError:
                send_queue.put_nowait({"type": "error", "message": "Invalid JSON"})
    except WebSocketDisconnect:
        pass
    finally:
        stop_evt.set()
        sub_task.cancel()
        # unregister removes this specific queue and puts None sentinel → sender exits
        chat_connections.unregister(user_id, send_queue)
        sender_task.cancel()
        message_manager.disconnect(user_id, websocket)
        try:
            from app.core.redis import get_redis_client
            get_redis_client().srem("chat:online_users", user_id)
        except Exception:
            pass
        from datetime import datetime, timezone as _tz
        last_seen_at = datetime.now(_tz.utc).isoformat()
        asyncio.create_task(_touch_presence())
        asyncio.create_task(_broadcast_presence_event(user_id, {
            "type": "user_offline",
            "user_id": user_id,
            "last_seen_at": last_seen_at,
        }))
        logger.info("WS /messages disconnected: user=%s", user_id)


@app.websocket("/ws/classroom/{session_id}")
async def websocket_classroom(websocket: WebSocket, session_id: str, token: str = ""):
    """Live classroom channel — whiteboard strokes, screen-share annotation,
    hand-raise, and REST-triggered chapter/quiz/file events (published by
    app/routers/classroom.py via app.core.classroom_ws). Connect with the JWT
    as a WS subprotocol (preferred) or legacy `?token=<jwt>`. Group lessons:
    every student has their own session_id
    but all resolve to the same broadcast room (see
    app.services.livekit_video.room_key_for_session)."""
    import asyncio
    from uuid import UUID as _UUID

    from app.core.classroom_ws import classroom_channel
    from app.database import get_session as _get_db_session
    from app.models.booking import TutoringSession
    from app.services import livekit_video as _lk_video

    try:
        user_id, _email = await _resolve_ws_user(_extract_ws_token(websocket, token))
    except ValueError as exc:
        logger.info("WS /classroom rejected: %s", exc)
        await websocket.close(code=4001, reason="Unauthorized")
        return

    try:
        with next(_get_db_session()) as _db:
            session = _db.get(TutoringSession, _UUID(session_id))
            if session is None:
                await websocket.close(code=4004, reason="Session not found")
                return
            if not _lk_video.is_session_participant(_db, session, _UUID(user_id)):
                await websocket.close(code=4003, reason="Forbidden")
                return
            room_key = _lk_video.room_key_for_session(_db, session)
    except Exception:
        logger.exception("WS /classroom setup failed session_id=%s", session_id)
        await websocket.close(code=1011, reason="Server error")
        return

    await websocket.accept()
    send_queue = classroom_connections.register(room_key)
    stop_evt = asyncio.Event()

    async def _sender():
        while True:
            data = await send_queue.get()
            if data is None:
                break
            try:
                await websocket.send_json(data)
            except Exception:
                break

    async def _redis_subscriber():
        import redis.asyncio as aioredis

        url = get_settings().redis_url
        while not stop_evt.is_set():
            r = None
            try:
                r = aioredis.Redis.from_url(url, decode_responses=True)
                ps = r.pubsub()
                await ps.subscribe(classroom_channel(room_key))
                while not stop_evt.is_set():
                    raw = await ps.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if raw is None:
                        continue
                    if raw.get("type") == "message":
                        try:
                            await send_queue.put(json.loads(raw["data"]))
                        except Exception:
                            pass
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("classroom redis subscriber error room=%s: %s — reconnecting", room_key, exc)
                await asyncio.sleep(2.0)
            finally:
                if r:
                    try:
                        await r.aclose()
                    except Exception:
                        pass

    sender_task = asyncio.create_task(_sender())
    sub_task = asyncio.create_task(_redis_subscriber())

    from app.core.classroom_ws import publish_classroom_event
    publish_classroom_event(room_key, {"type": "participant_joined", "user_id": user_id})
    logger.info("WS /classroom connected: user=%s room=%s", user_id, room_key)

    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                mtype = msg.get("type")
                if mtype == "ping":
                    send_queue.put_nowait({"type": "pong"})
                elif mtype in (
                    "whiteboard_stroke", "whiteboard_clear",
                    "annotation_stroke", "annotation_clear",
                    "hand_raise",
                ):
                    msg["from"] = user_id
                    publish_classroom_event(room_key, msg)
            except json.JSONDecodeError:
                send_queue.put_nowait({"type": "error", "message": "Invalid JSON"})
    except WebSocketDisconnect:
        pass
    finally:
        stop_evt.set()
        sub_task.cancel()
        classroom_connections.unregister(room_key, send_queue)
        sender_task.cancel()
        publish_classroom_event(room_key, {"type": "participant_left", "user_id": user_id})
        logger.info("WS /classroom disconnected: user=%s room=%s", user_id, room_key)


@app.websocket("/ws/competitive/{match_id}")
async def websocket_competitive(websocket: WebSocket, match_id: str, token: str = ""):
    """Competitive Arena real-time channel (Phase 2 lobby + Phase 5 live
    gameplay push). Connect with the JWT as a WS subprotocol (preferred) or
    legacy `?token=<jwt>`. Mirrors /ws/classroom's
    per-match Redis-backed pub/sub room, one queue per connection.

    Phase 5: `gameplay_update` signals published via app.core.competitive_ws
    are no longer relayed raw — this handler computes THIS connection's own
    personalized MatchStateResponse (own answer visible, opponent's never
    leaked pre-reveal) and pushes it directly as `gameplay_state`, so the
    frontend never needs to poll GET .../gameplay/state on an interval.
    Other signal types (ready_changed, participant_connected/disconnected)
    are still relayed as-is (Phase 2 lobby behavior, unchanged)."""
    import asyncio
    from uuid import UUID as _UUID

    from app.core.competitive_ws import competitive_channel, publish_competitive_event
    from app.database import get_session as _get_db_session
    from app.models.admin import PlatformSettings
    from app.services.competitive import connection_service, gameplay_service, match_service, presence_service

    try:
        user_id, _email = await _resolve_ws_user(_extract_ws_token(websocket, token))
    except ValueError as exc:
        logger.info("WS /competitive rejected: %s", exc)
        await websocket.close(code=4001, reason="Unauthorized")
        return

    try:
        from app.models.competitive import CompetitiveMatch as _CompetitiveMatch

        was_disconnected = False
        with next(_get_db_session()) as _db:
            match = _db.get(_CompetitiveMatch, _UUID(match_id))
            if match is None:
                await websocket.close(code=4004, reason="Match not found")
                return
            participants = match_service.get_participants(_db, match.id)
            if not any(str(p.user_id) == user_id for p in participants):
                await websocket.close(code=4003, reason="Forbidden")
                return
            settings_row = _db.get(PlatformSettings, True) or PlatformSettings()
            grace_minutes = settings_row.competitive_disconnect_grace_minutes
            ingame_grace_seconds = settings_row.competitive_ingame_disconnect_grace_seconds
            prior_presence = presence_service.get_presence(match.id).get(user_id)
            was_disconnected = bool(prior_presence and prior_presence.get("status") == presence_service.STATUS_DISCONNECTED)
    except Exception:
        logger.exception("WS /competitive setup failed match_id=%s", match_id)
        await websocket.close(code=1011, reason="Server error")
        return

    await websocket.accept()
    send_queue = competitive_connections.register(match_id)
    stop_evt = asyncio.Event()

    async def _sender():
        while True:
            data = await send_queue.get()
            if data is None:
                break
            try:
                await websocket.send_json(data)
            except Exception:
                break

    async def _push_personal_gameplay_state():
        """Phase 5 true push — computed fresh per connection, never a raw
        relay, so each player only ever sees their own answer pre-reveal.

        Phase 10, Part B: a battle_royale match reuses this SAME connection/
        push mechanism (same audience — authenticated participants of this
        match_id) but needs an N-player-shaped payload, so it's computed via
        gameplay_service.compute_battle_royale_state and pushed under a
        distinct `battle_royale_state` type instead of `gameplay_state` —
        one cheap match_type check per push, no new WS endpoint."""
        try:
            with next(_get_db_session()) as _db:
                from app.models.competitive import CompetitiveMatch as _CM
                m = _db.get(_CM, _UUID(match_id))
                if m is None:
                    return
                if m.match_type == "battle_royale":
                    br_state = gameplay_service.compute_battle_royale_state(_db, m, viewer_id=_UUID(user_id))
                    await send_queue.put({"type": "battle_royale_state", "state": json.loads(br_state.model_dump_json())})
                else:
                    state = gameplay_service.compute_state(_db, m, viewer_id=_UUID(user_id))
                    await send_queue.put({"type": "gameplay_state", "state": json.loads(state.model_dump_json())})
        except Exception:
            logger.debug("failed to push personalized gameplay state match=%s user=%s", match_id, user_id, exc_info=True)

    async def _redis_subscriber():
        import redis.asyncio as aioredis

        url = get_settings().redis_url
        while not stop_evt.is_set():
            r = None
            try:
                r = aioredis.Redis.from_url(url, decode_responses=True)
                ps = r.pubsub()
                await ps.subscribe(competitive_channel(match_id))
                while not stop_evt.is_set():
                    raw = await ps.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if raw is None:
                        continue
                    if raw.get("type") == "message":
                        try:
                            payload = json.loads(raw["data"])
                            if payload.get("type") == "gameplay_update":
                                await _push_personal_gameplay_state()
                            else:
                                await send_queue.put(payload)
                        except Exception:
                            pass
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("competitive redis subscriber error match=%s: %s — reconnecting", match_id, exc)
                await asyncio.sleep(2.0)
            finally:
                if r:
                    try:
                        await r.aclose()
                    except Exception:
                        pass

    sender_task = asyncio.create_task(_sender())
    sub_task = asyncio.create_task(_redis_subscriber())

    presence_service.set_status(_UUID(match_id), _UUID(user_id), presence_service.STATUS_ONLINE)
    publish_competitive_event(match_id, {"type": "participant_connected", "user_id": user_id})
    try:
        with next(_get_db_session()) as _db:
            connection_service.log_connection_event(
                _db, match_id=_UUID(match_id), user_id=_UUID(user_id),
                event_type="reconnected" if was_disconnected else "connected",
            )
            from app.services.notification_engine import emit as _emit
            other = next((p for p in match_service.get_participants(_db, _UUID(match_id)) if str(p.user_id) != user_id), None)
            if other:
                _emit(
                    _db, event_type="competitive_opponent_connected", user_id=other.user_id,
                    dedup_key=f"competitive_opponent_connected:{match_id}:{user_id}",
                )
    except Exception:
        logger.debug("competitive opponent_connected notify failed", exc_info=True)
    logger.info("WS /competitive connected: user=%s match=%s reconnect=%s", user_id, match_id, was_disconnected)

    # If gameplay is already live, push this connection its own state right
    # away (covers reconnect-mid-question without waiting for the next tick).
    if match.status == "in_progress":
        await _push_personal_gameplay_state()

    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    # Heartbeat: refresh presence's last_seen so
                    # task_detect_stale_gameplay_connections never flags an
                    # actively-connected client as stale.
                    presence_service.set_status(_UUID(match_id), _UUID(user_id), presence_service.STATUS_ONLINE)
                    send_queue.put_nowait({"type": "pong"})
            except json.JSONDecodeError:
                send_queue.put_nowait({"type": "error", "message": "Invalid JSON"})
    except WebSocketDisconnect:
        pass
    finally:
        stop_evt.set()
        sub_task.cancel()
        competitive_connections.unregister(match_id, send_queue)
        sender_task.cancel()
        presence_service.set_status(_UUID(match_id), _UUID(user_id), presence_service.STATUS_DISCONNECTED)
        publish_competitive_event(match_id, {"type": "participant_disconnected", "user_id": user_id})
        try:
            with next(_get_db_session()) as _db:
                connection_service.log_connection_event(
                    _db, match_id=_UUID(match_id), user_id=_UUID(user_id), event_type="disconnected",
                )
                from app.models.competitive import CompetitiveMatch as _CM
                current_match = _db.get(_CM, _UUID(match_id))
                current_br = None
                if current_match is not None and current_match.match_type == "battle_royale":
                    from app.models.competitive import CompetitiveBattleRoyaleMatch as _BRM
                    current_br = _db.get(_BRM, _UUID(match_id))
            is_ingame = current_match is not None and current_match.status == "in_progress"
            if is_ingame and current_match.match_type == "battle_royale":
                # Phase 10, Part B — a BR-specific disconnect task: the
                # existing task_check_ingame_disconnect forfeits the ENTIRE
                # match to "the other player", which is the wrong shape for
                # an N-player Battle Royale (one disconnect must never end
                # the match for everyone else). See task_check_battle_
                # royale_disconnect's own docstring.
                from app.workers.competitive_tasks import task_check_battle_royale_disconnect
                grace = current_br.disconnect_grace_seconds if current_br else ingame_grace_seconds
                task_check_battle_royale_disconnect.apply_async(
                    args=[match_id, user_id], countdown=grace,
                )
            elif is_ingame:
                from app.workers.competitive_tasks import task_check_ingame_disconnect
                task_check_ingame_disconnect.apply_async(
                    args=[match_id, user_id], countdown=ingame_grace_seconds,
                )
            else:
                from app.workers.competitive_tasks import task_check_waiting_room_disconnect
                task_check_waiting_room_disconnect.apply_async(
                    args=[match_id, user_id], countdown=grace_minutes * 60,
                )
        except Exception:
            logger.debug("scheduling disconnect grace check failed", exc_info=True)
        logger.info("WS /competitive disconnected: user=%s match=%s", user_id, match_id)


@app.websocket("/ws/competitive/{match_id}/spectate")
async def websocket_competitive_spectate(websocket: WebSocket, match_id: str, token: str = ""):
    """Competitive Arena Spectator Mode real-time channel (Phase 8). Connect
    with the JWT as a WS subprotocol (preferred) or legacy `?token=<jwt>`.
    Structurally mirrors /ws/competitive/{match_id}
    (the player channel just above) — same auth, same per-connection queue
    via competitive_connections (the same _ChatConnectionManager instance
    the player channel and /ws/chat both already share, just keyed by a
    different match_id-derived channel), same Redis pub/sub subscriber
    pattern — but with three deliberate differences:

    1. NO participant check — any authenticated user may connect (this
       platform requires login everywhere already; spectating isn't
       restricted to invited players). Only PUBLIC matches are surfaced by
       GET /live-matches, so an uninvited spectator would need the private
       match's exact match_id to reach this at all.
    2. A CAPACITY check — rejects with 4029 if
       platform_settings.competitive_spectator_max_default is set and the
       live spectator count (Redis-backed, spectator_presence_service) is
       already at/above it.
    3. The pushed state is VIEWER-AGNOSTIC — gameplay_service.
       compute_spectator_state() is computed once and pushed identically to
       every connected spectator (unlike the player channel's per-viewer
       compute_state()), since spectators never see hidden per-player info
       before the reveal phase anyway.
    """
    import asyncio
    from uuid import UUID as _UUID

    from app.core.spectator_ws import publish_spectator_event, spectator_channel
    from app.database import get_session as _get_db_session
    from app.models.admin import PlatformSettings
    from app.services.competitive import gameplay_service, spectator_presence_service, spectator_service

    try:
        user_id, _email = await _resolve_ws_user(_extract_ws_token(websocket, token))
    except ValueError as exc:
        logger.info("WS /competitive/spectate rejected: %s", exc)
        await websocket.close(code=4001, reason="Unauthorized")
        return

    try:
        from app.models.competitive import CompetitiveMatch as _CompetitiveMatch

        with next(_get_db_session()) as _db:
            match = _db.get(_CompetitiveMatch, _UUID(match_id))
            if match is None:
                await websocket.close(code=4004, reason="Match not found")
                return
            settings_row = _db.get(PlatformSettings, True) or PlatformSettings()
            # Phase 9 — a per-match override (e.g. set by a tournament's
            # organizer) takes precedence over the platform-wide default.
            max_spectators = match.max_spectators if match.max_spectators is not None else settings_row.competitive_spectator_max_default
            if max_spectators is not None and spectator_presence_service.get_count(match.id) >= max_spectators:
                await websocket.close(code=4029, reason="Spectator capacity reached")
                return
    except Exception:
        logger.exception("WS /competitive/spectate setup failed match_id=%s", match_id)
        await websocket.close(code=1011, reason="Server error")
        return

    await websocket.accept()
    send_queue = competitive_connections.register(f"spectate:{match_id}")
    stop_evt = asyncio.Event()

    async def _sender():
        while True:
            data = await send_queue.get()
            if data is None:
                break
            try:
                await websocket.send_json(data)
            except Exception:
                break

    async def _push_spectator_state():
        try:
            with next(_get_db_session()) as _db:
                from app.models.competitive import CompetitiveMatch as _CM
                m = _db.get(_CM, _UUID(match_id))
                if m is None:
                    return
                count = spectator_presence_service.get_count(m.id)
                state = gameplay_service.compute_spectator_state(_db, m, spectator_count=count)
                await send_queue.put({"type": "spectator_state", "state": json.loads(state.model_dump_json())})
        except Exception:
            logger.debug("failed to push spectator state match=%s", match_id, exc_info=True)

    async def _redis_subscriber():
        import redis.asyncio as aioredis

        url = get_settings().redis_url
        while not stop_evt.is_set():
            r = None
            try:
                r = aioredis.Redis.from_url(url, decode_responses=True)
                ps = r.pubsub()
                await ps.subscribe(spectator_channel(match_id))
                while not stop_evt.is_set():
                    raw = await ps.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if raw is None:
                        continue
                    if raw.get("type") == "message":
                        try:
                            payload = json.loads(raw["data"])
                            if payload.get("type") == "spectator_update":
                                await _push_spectator_state()
                            else:
                                # chat_message/chat_deleted/reaction/
                                # prediction_summary/timeline_event/
                                # spectator_count relay as-is.
                                await send_queue.put(payload)
                        except Exception:
                            pass
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("spectator redis subscriber error match=%s: %s — reconnecting", match_id, exc)
                await asyncio.sleep(2.0)
            finally:
                if r:
                    try:
                        await r.aclose()
                    except Exception:
                        pass

    sender_task = asyncio.create_task(_sender())
    sub_task = asyncio.create_task(_redis_subscriber())

    try:
        with next(_get_db_session()) as _db:
            spectator_service.record_spectator_join(_db, match_id=_UUID(match_id), user_id=_UUID(user_id))
    except Exception:
        logger.debug("record_spectator_join failed match=%s user=%s", match_id, user_id, exc_info=True)
    new_count = spectator_presence_service.add_viewer(_UUID(match_id), _UUID(user_id))
    publish_spectator_event(match_id, {"type": "spectator_count", "count": new_count})
    logger.info("WS /competitive/spectate connected: user=%s match=%s count=%s", user_id, match_id, new_count)

    await _push_spectator_state()

    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    send_queue.put_nowait({"type": "pong"})
            except json.JSONDecodeError:
                send_queue.put_nowait({"type": "error", "message": "Invalid JSON"})
    except WebSocketDisconnect:
        pass
    finally:
        stop_evt.set()
        sub_task.cancel()
        competitive_connections.unregister(f"spectate:{match_id}", send_queue)
        sender_task.cancel()
        try:
            with next(_get_db_session()) as _db:
                spectator_service.record_spectator_leave(_db, match_id=_UUID(match_id), user_id=_UUID(user_id))
        except Exception:
            logger.debug("record_spectator_leave failed match=%s user=%s", match_id, user_id, exc_info=True)
        left_count = spectator_presence_service.remove_viewer(_UUID(match_id), _UUID(user_id))
        publish_spectator_event(match_id, {"type": "spectator_count", "count": left_count})
        logger.info("WS /competitive/spectate disconnected: user=%s match=%s count=%s", user_id, match_id, left_count)


@app.websocket("/ws/competitive/tournaments/{tournament_id}")
async def websocket_competitive_tournament(websocket: WebSocket, tournament_id: str, token: str = ""):
    """Tournament System real-time channel (Phase 9 Part B). Connect with
    the JWT as a WS subprotocol (preferred) or legacy `?token=<jwt>`. Any
    authenticated user may connect — no participant/
    capacity check, unlike the player/spectator gameplay channels: a
    tournament bracket is fully public data with no hidden-information
    concern.

    Unlike those other two channels, this one is SIGNAL-ONLY — it never
    computes/pushes a full bracket state (that payload is comparatively
    large/complex); it just relays the small set of typed events published
    by tournament_service (bracket_updated/round_started/round_completed/
    match_created/participant_advanced/tournament_completed) as-is, and the
    frontend refetches GET /api/competitive/tournaments/{id}/bracket
    whenever one arrives."""
    import asyncio
    from uuid import UUID as _UUID

    from app.core.tournament_ws import tournament_channel

    try:
        user_id, _email = await _resolve_ws_user(_extract_ws_token(websocket, token))
    except ValueError as exc:
        logger.info("WS /competitive/tournaments rejected: %s", exc)
        await websocket.close(code=4001, reason="Unauthorized")
        return

    try:
        _UUID(tournament_id)
    except ValueError:
        await websocket.close(code=4004, reason="Invalid tournament id")
        return

    await websocket.accept()
    send_queue = competitive_connections.register(f"tournament:{tournament_id}")
    stop_evt = asyncio.Event()

    async def _sender():
        while True:
            data = await send_queue.get()
            if data is None:
                break
            try:
                await websocket.send_json(data)
            except Exception:
                break

    async def _redis_subscriber():
        import redis.asyncio as aioredis

        url = get_settings().redis_url
        while not stop_evt.is_set():
            r = None
            try:
                r = aioredis.Redis.from_url(url, decode_responses=True)
                ps = r.pubsub()
                await ps.subscribe(tournament_channel(tournament_id))
                while not stop_evt.is_set():
                    raw = await ps.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if raw is None:
                        continue
                    if raw.get("type") == "message":
                        try:
                            await send_queue.put(json.loads(raw["data"]))
                        except Exception:
                            pass
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("tournament redis subscriber error tournament=%s: %s — reconnecting", tournament_id, exc)
                await asyncio.sleep(2.0)
            finally:
                if r:
                    try:
                        await r.aclose()
                    except Exception:
                        pass

    sender_task = asyncio.create_task(_sender())
    sub_task = asyncio.create_task(_redis_subscriber())
    logger.info("WS /competitive/tournaments connected: user=%s tournament=%s", user_id, tournament_id)

    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    send_queue.put_nowait({"type": "pong"})
            except json.JSONDecodeError:
                send_queue.put_nowait({"type": "error", "message": "Invalid JSON"})
    except WebSocketDisconnect:
        pass
    finally:
        stop_evt.set()
        sub_task.cancel()
        competitive_connections.unregister(f"tournament:{tournament_id}", send_queue)
        sender_task.cancel()
        logger.info("WS /competitive/tournaments disconnected: user=%s tournament=%s", user_id, tournament_id)


@app.websocket("/ws/club/{club_id}")
async def websocket_club(websocket: WebSocket, club_id: str, token: str = ""):
    """Clash Club real-time channel (Phase 11, Part B) — chat send/pin/
    unpin/delete. Connect with the JWT as a WS subprotocol (preferred) or
    legacy `?token=<jwt>`. Members-only (unlike the
    public tournament bracket channel this otherwise mirrors) — a club's
    chat is private to its roster, same gating club_chat_service already
    applies on every REST mutation. Signal-only, same shape as
    /ws/competitive/tournaments/{id}: REST endpoints
    (app/routers/club/chat.py) call publish_club_event() directly, this
    handler just relays it to every connected member."""
    import asyncio
    from uuid import UUID as _UUID

    from app.core.club_ws import club_channel
    from app.database import get_session as _get_db_session
    from app.models.club import ClubMember as _ClubMember

    try:
        user_id, _email = await _resolve_ws_user(_extract_ws_token(websocket, token))
    except ValueError as exc:
        logger.info("WS /club rejected: %s", exc)
        await websocket.close(code=4001, reason="Unauthorized")
        return

    try:
        with next(_get_db_session()) as _db:
            from sqlmodel import select as _select
            membership = _db.exec(
                _select(_ClubMember).where(_ClubMember.club_id == _UUID(club_id))
                .where(_ClubMember.user_id == _UUID(user_id)).where(_ClubMember.status == "active")
            ).first()
            if membership is None:
                await websocket.close(code=4003, reason="Forbidden")
                return
    except Exception:
        logger.exception("WS /club setup failed club_id=%s", club_id)
        await websocket.close(code=1011, reason="Server error")
        return

    await websocket.accept()
    send_queue = club_connections.register(club_id)
    stop_evt = asyncio.Event()

    async def _sender():
        while True:
            data = await send_queue.get()
            if data is None:
                break
            try:
                await websocket.send_json(data)
            except Exception:
                break

    async def _redis_subscriber():
        import redis.asyncio as aioredis

        url = get_settings().redis_url
        while not stop_evt.is_set():
            r = None
            try:
                r = aioredis.Redis.from_url(url, decode_responses=True)
                ps = r.pubsub()
                await ps.subscribe(club_channel(club_id))
                while not stop_evt.is_set():
                    raw = await ps.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if raw is None:
                        continue
                    if raw.get("type") == "message":
                        try:
                            await send_queue.put(json.loads(raw["data"]))
                        except Exception:
                            pass
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("club redis subscriber error club=%s: %s — reconnecting", club_id, exc)
                await asyncio.sleep(2.0)
            finally:
                if r:
                    try:
                        await r.aclose()
                    except Exception:
                        pass

    sender_task = asyncio.create_task(_sender())
    sub_task = asyncio.create_task(_redis_subscriber())
    logger.info("WS /club connected: user=%s club=%s", user_id, club_id)

    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    send_queue.put_nowait({"type": "pong"})
            except json.JSONDecodeError:
                send_queue.put_nowait({"type": "error", "message": "Invalid JSON"})
    except WebSocketDisconnect:
        pass
    finally:
        stop_evt.set()
        sub_task.cancel()
        club_connections.unregister(club_id, send_queue)
        sender_task.cancel()
        logger.info("WS /club disconnected: user=%s club=%s", user_id, club_id)


async def _chat_redis_subscriber(user_id: str, send_queue: "asyncio.Queue", stop: "asyncio.Event"):
    """Subscribe to Redis pub/sub and enqueue messages for the sender task (cross-worker delivery)."""
    import asyncio
    import redis.asyncio as aioredis
    from app.config import get_settings

    url = get_settings().redis_url

    while not stop.is_set():
        r = None
        try:
            r = aioredis.Redis.from_url(url, decode_responses=True)
            ps = r.pubsub()
            await ps.subscribe(f"chat:user:{user_id}")
            logger.debug("Redis subscriber ready: user=%s", user_id)

            while not stop.is_set():
                # get_message with timeout=1.0 blocks up to 1 s then returns None.
                # Messages arrive instantly (no need to wait the full second).
                raw = await ps.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if raw is None:
                    continue
                if raw.get("type") == "message":
                    try:
                        await send_queue.put(json.loads(raw["data"]))
                    except Exception as exc:
                        logger.debug("Redis msg parse error: %s", exc)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("Redis subscriber error user=%s: %s — reconnecting", user_id, exc)
            await asyncio.sleep(2.0)
        finally:
            if r:
                try:
                    await r.aclose()
                except Exception:
                    pass


async def _broadcast_presence_event(user_id: str, event: dict) -> None:
    """Notify all conversation partners of a presence change."""
    try:
        from uuid import UUID as _UUID
        from sqlmodel import select as _select
        from app.database import get_session
        from app.models.conversation import ConversationParticipant
        from app.core.connections import chat_connections as _cc

        with next(get_session()) as _db:
            my_parts = _db.exec(
                _select(ConversationParticipant).where(
                    ConversationParticipant.user_id == _UUID(user_id)
                )
            ).all()
            conv_ids = [p.conv_id for p in my_parts]
            if not conv_ids:
                return
            partner_parts = _db.exec(
                _select(ConversationParticipant).where(
                    ConversationParticipant.conv_id.in_(conv_ids),
                    ConversationParticipant.user_id != _UUID(user_id),
                )
            ).all()
            partner_ids = list({str(p.user_id) for p in partner_parts})

        for pid in partner_ids:
            delivered = await _cc.send(pid, event)
            if not delivered:
                try:
                    from app.core.redis import get_redis_client
                    get_redis_client().publish(f"chat:user:{pid}", json.dumps(event))
                except Exception:
                    pass
    except Exception as exc:
        logger.debug("presence broadcast error: %s", exc)


async def _send_partner_online_statuses(user_id: str, send_queue: "asyncio.Queue") -> None:
    """Push current online status of all conversation partners to a newly connected user.

    Fixes the race condition where both users are already connected when the page
    loads — neither receives a user_online event because both connected before the
    other registered their WS.  Calling this on connect ensures the freshly arrived
    user learns which partners are already online without waiting for those partners
    to reconnect.
    """
    try:
        from uuid import UUID as _UUID
        from sqlmodel import select as _select
        from app.database import get_session
        from app.models.conversation import ConversationParticipant
        from app.routers.messages import _is_online

        with next(get_session()) as _db:
            my_parts = _db.exec(
                _select(ConversationParticipant).where(
                    ConversationParticipant.user_id == _UUID(user_id)
                )
            ).all()
            conv_ids = [p.conv_id for p in my_parts]
            if not conv_ids:
                return
            partner_parts = _db.exec(
                _select(ConversationParticipant).where(
                    ConversationParticipant.conv_id.in_(conv_ids),
                    ConversationParticipant.user_id != _UUID(user_id),
                )
            ).all()
            partner_ids = list({str(p.user_id) for p in partner_parts})

        for pid in partner_ids:
            if _is_online(pid):
                send_queue.put_nowait({"type": "user_online", "user_id": pid})
    except Exception as exc:
        logger.debug("partner status push failed: %s", exc)


def _handle_chat_typing(user_id: str, conv_id: str, is_typing: bool):
    """Set/clear the typing Redis key and notify other conversation participants."""
    if not conv_id:
        return
    try:
        from uuid import UUID as _UUID
        from app.core.redis import get_redis_client
        from app.database import get_session
        from app.models.conversation import ConversationParticipant
        from sqlmodel import select

        r = get_redis_client()
        key = f"chat:typing:{conv_id}:{user_id}"
        if is_typing:
            r.setex(key, 3, "1")
        else:
            r.delete(key)

        with next(get_session()) as _db:
            parts = _db.exec(
                select(ConversationParticipant).where(
                    ConversationParticipant.conv_id == _UUID(conv_id),
                    ConversationParticipant.user_id != _UUID(user_id),
                )
            ).all()
            event = json.dumps({
                "type": "typing_start" if is_typing else "typing_stop",
                "conv_id": conv_id,
                "user_id": user_id,
            })
            for p in parts:
                r.publish(f"chat:user:{p.user_id}", event)
    except Exception as exc:
        logger.debug("typing handler error: %s", exc)


@app.websocket("/ws/notifications")
async def websocket_notifications(websocket: WebSocket, token: str = ""):
    """Real-time notification channel. Connect with the JWT as a WS
    subprotocol (preferred) or legacy `?token=<jwt>`."""
    try:
        user_id, _email = await _resolve_ws_user(_extract_ws_token(websocket, token))
    except ValueError as exc:
        logger.info("WS /notifications rejected: %s", exc)
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await notification_manager.connect(user_id, websocket)
    logger.info("WS /notifications connected: user=%s", user_id)

    try:
        from uuid import UUID as _UUID
        from sqlmodel import select
        from app.database import get_session
        from app.models.notification import Notification

        profile_uuid = _UUID(user_id)
        with next(get_session()) as db:
            unread_count = len(
                db.exec(
                    select(Notification).where(
                        Notification.user_id == profile_uuid,
                        Notification.read_at.is_(None),
                    )
                ).all()
            )
            await websocket.send_json({"type": "unread_count", "count": unread_count})
    except Exception as exc:
        logger.warning("Could not fetch unread count: %s", exc)

    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        notification_manager.disconnect(user_id, websocket)
        logger.info("WS /notifications disconnected: user=%s", user_id)
