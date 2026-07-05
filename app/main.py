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
    r"|^https://[a-z0-9-]+\.nacimmessi1010\.workers\.dev$"  # Cloudflare Workers dev
    r"|^https://[a-z0-9-]+\.workers\.dev$"              # any Cloudflare Workers
    r"|^https://[a-z0-9-]+\.pages\.dev$"                # any Cloudflare Pages
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
from app.routers.student_progress import router as student_progress_router
from app.routers.student_badges import router as student_badges_router
from app.routers.student_leaderboard import router as student_leaderboard_router
from app.routers.admin.questions import router as admin_questions_router
from app.routers.admin.auth import router as admin_auth_router
from app.routers.admin.messages import router as admin_messages_router
from app.routers.store import router as store_router
from app.routers.admin.store import router as admin_store_router

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
app.include_router(student_progress_router,  prefix="/api/student",    tags=["Student"])
app.include_router(student_badges_router,      prefix="/api/student",    tags=["Student"])
app.include_router(student_leaderboard_router, prefix="/api/student",    tags=["Student"])

app.include_router(admin_auth_router,       prefix="/api/admin/auth",       tags=["Admin — Auth"])
app.include_router(admin_users_router,      prefix="/api/admin/users",      tags=["Admin — Users"])
app.include_router(admin_teachers_router,   prefix="/api/admin/teachers",   tags=["Admin — Teachers"])
app.include_router(admin_reviews_router,    prefix="/api/admin/reviews",    tags=["Admin — Reviews"])
app.include_router(admin_challenges_router, prefix="/api/admin/challenges", tags=["Admin — Challenges"])
app.include_router(admin_promos_router,     prefix="/api/admin/promos",     tags=["Admin — Promos"])
app.include_router(admin_content_router,    prefix="/api/admin/content",    tags=["Admin — Content"])
app.include_router(admin_stats_router,      prefix="/api/admin/stats",      tags=["Admin — Stats"])
app.include_router(admin_questions_router,  prefix="/api/admin/questions",  tags=["Admin — Questions"])
app.include_router(admin_messages_router,   prefix="/api/admin/messages",   tags=["Admin — Messages"])
app.include_router(store_router,            prefix="/api/store",            tags=["Store"])
app.include_router(admin_store_router,      prefix="/api/admin/store",      tags=["Admin — Store"])


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


@app.websocket("/ws/messages")
async def websocket_messages(websocket: WebSocket, token: str = ""):
    """Real-time messaging channel. Connect with `?token=<jwt>`."""
    import asyncio
    from app.core.security import decode_supabase_jwt
    from app.core.connections import chat_connections

    claims = decode_supabase_jwt(token) if token else None
    if not claims:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    user_id = claims.get("sub", "")
    if not user_id:
        await websocket.close(code=4001, reason="Invalid token")
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
        # unregister puts None sentinel into send_queue → sender task exits cleanly
        chat_connections.unregister(user_id)
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
