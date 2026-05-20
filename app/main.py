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
            "Formulas: `single`, `pack5`, `monthly`. Modes: `online`, `presentiel`, `hybrid`."
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
    r"^https?://localhost(:\d+)?$"          # local dev (any port)
    r"|^https://e-novar\.com$"              # production apex
    r"|^https://[a-z0-9-]+\.e-novar\.com$"  # preprod, recette, any branch
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,   # explicit extras from env var
    allow_origin_regex=_CORS_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_exception_handlers(app)

# ── Routers ───────────────────────────────────────────────────────────────────
from app.routers.auth import router as auth_router
from app.routers.profile import router as profile_router
from app.routers.onboarding import router as onboarding_router
from app.routers.teachers import router as teachers_router
from app.routers.bookings import router as bookings_router
from app.routers.payments import router as payments_router
from app.routers.sessions import router as sessions_router
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
from app.routers.admin.questions import router as admin_questions_router

app.include_router(auth_router,          prefix="/api/auth",           tags=["Auth"])
app.include_router(profile_router,       prefix="/api/profile",        tags=["Profile"])
app.include_router(onboarding_router,    prefix="/api/onboarding",     tags=["Onboarding"])
app.include_router(teachers_router,      prefix="/api/teachers",       tags=["Teachers"])
app.include_router(bookings_router,      prefix="/api/bookings",       tags=["Bookings"])
app.include_router(payments_router,      prefix="/api/payments",       tags=["Payments"])
app.include_router(sessions_router,      prefix="/api/sessions",       tags=["Sessions"])
app.include_router(homework_router,      prefix="/api/homework",       tags=["Homework"])
app.include_router(messages_router,      prefix="/api/messages",       tags=["Messages"])
app.include_router(notifications_router, prefix="/api/notifications",  tags=["Notifications"])
app.include_router(kp_router,            prefix="/api/kp",             tags=["E-NOVAR Points"])
app.include_router(challenges_router,    prefix="/api/challenges",     tags=["Challenges"])
app.include_router(ai_router,            prefix="/api/ai",             tags=["AI Tutor"])
app.include_router(favorites_router,     prefix="/api/favorites",      tags=["Favorites"])
app.include_router(referrals_router,     prefix="/api/referrals",      tags=["Referrals"])
app.include_router(catalogs_router,      prefix="/api/catalogs",       tags=["Catalogs"])
app.include_router(parent_router,        prefix="/api/parent",         tags=["Parent"])
app.include_router(files_router,         prefix="/api/files",          tags=["Files"])
app.include_router(student_dashboard_router, prefix="/api/student",    tags=["Student"])
app.include_router(student_homework_router,  prefix="/api/student",    tags=["Student"])
app.include_router(student_teachers_router,  prefix="/api/student",    tags=["Student"])
app.include_router(student_practice_router,  prefix="/api/student",    tags=["Student"])

app.include_router(admin_users_router,      prefix="/api/admin/users",      tags=["Admin — Users"])
app.include_router(admin_teachers_router,   prefix="/api/admin/teachers",   tags=["Admin — Teachers"])
app.include_router(admin_reviews_router,    prefix="/api/admin/reviews",    tags=["Admin — Reviews"])
app.include_router(admin_challenges_router, prefix="/api/admin/challenges", tags=["Admin — Challenges"])
app.include_router(admin_promos_router,     prefix="/api/admin/promos",     tags=["Admin — Promos"])
app.include_router(admin_content_router,    prefix="/api/admin/content",    tags=["Admin — Content"])
app.include_router(admin_stats_router,      prefix="/api/admin/stats",      tags=["Admin — Stats"])
app.include_router(admin_questions_router,  prefix="/api/admin/questions",  tags=["Admin — Questions"])


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


@app.websocket("/ws/messages")
async def websocket_messages(websocket: WebSocket, token: str = ""):
    """Real-time messaging channel. Connect with `?token=<jwt>`."""
    from app.core.security import decode_supabase_jwt

    claims = decode_supabase_jwt(token) if token else None
    if not claims:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    user_id = claims.get("sub", "")
    if not user_id:
        await websocket.close(code=4001, reason="Invalid token")
        return

    await message_manager.connect(user_id, websocket)
    logger.info("WS /messages connected: user=%s", user_id)

    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                msg_type = msg.get("type")
                if msg_type == "ping":
                    await websocket.send_json({"type": "pong"})
                elif msg_type == "typing":
                    await websocket.send_json({
                        "type": "typing_ack",
                        "conversation_id": msg.get("conversation_id"),
                    })
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "Invalid JSON"})
    except WebSocketDisconnect:
        message_manager.disconnect(user_id, websocket)
        logger.info("WS /messages disconnected: user=%s", user_id)


@app.websocket("/ws/notifications")
async def websocket_notifications(websocket: WebSocket, token: str = ""):
    """Real-time notification channel. Connect with `?token=<jwt>`."""
    from app.core.security import decode_supabase_jwt

    claims = decode_supabase_jwt(token) if token else None
    if not claims:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    user_id = claims.get("sub", "")
    if not user_id:
        await websocket.close(code=4001, reason="Invalid token")
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
