from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.core.exceptions import register_exception_handlers

logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    logger.info("Starting Enovar API v1.0.0 ...")
    yield
    logger.info("Shutting down Enovar API ...")
    from app.core.redis import close_redis
    close_redis()


app = FastAPI(
    title="Enovar API",
    version="1.0.0",
    description="Backend API for Enovar - Algeria's premier tutoring platform",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register custom exception handlers
register_exception_handlers(app)

# Import and register all routers
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

# Admin routers
from app.routers.admin.users import router as admin_users_router
from app.routers.admin.teachers import router as admin_teachers_router
from app.routers.admin.reviews import router as admin_reviews_router
from app.routers.admin.challenges import router as admin_challenges_router
from app.routers.admin.promos import router as admin_promos_router
from app.routers.admin.content import router as admin_content_router
from app.routers.admin.stats import router as admin_stats_router

app.include_router(auth_router, prefix="/api/auth")
app.include_router(profile_router, prefix="/api/profile")
app.include_router(onboarding_router, prefix="/api/onboarding")
app.include_router(teachers_router, prefix="/api/teachers")
app.include_router(bookings_router, prefix="/api/bookings")
app.include_router(payments_router, prefix="/api/payments")
app.include_router(sessions_router, prefix="/api/sessions")
app.include_router(messages_router, prefix="/api/messages")
app.include_router(notifications_router, prefix="/api/notifications")
app.include_router(kp_router, prefix="/api/kp")
app.include_router(challenges_router, prefix="/api/challenges")
app.include_router(homework_router, prefix="/api/homework")
app.include_router(ai_router, prefix="/api/ai")
app.include_router(favorites_router, prefix="/api/favorites")
app.include_router(referrals_router, prefix="/api/referrals")
app.include_router(catalogs_router, prefix="/api/catalogs")
app.include_router(parent_router, prefix="/api/parent")
app.include_router(files_router, prefix="/api/files")

app.include_router(admin_users_router, prefix="/api/admin/users")
app.include_router(admin_teachers_router, prefix="/api/admin/teachers")
app.include_router(admin_reviews_router, prefix="/api/admin/reviews")
app.include_router(admin_challenges_router, prefix="/api/admin/challenges")
app.include_router(admin_promos_router, prefix="/api/admin/promos")
app.include_router(admin_content_router, prefix="/api/admin/content")
app.include_router(admin_stats_router, prefix="/api/admin/stats")


# Health check
@app.get("/health", tags=["health"])
async def health_check():
    """Check if the API is running."""
    return {
        "status": "ok",
        "version": "1.0.0",
        "app": "Enovar API",
        "environment": settings.app_env,
    }


# WebSocket connection manager
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
    """WebSocket endpoint for real-time messaging."""
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
    logger.info("WebSocket /messages connected: user=%s", user_id)

    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                msg_type = msg.get("type")

                if msg_type == "ping":
                    await websocket.send_json({"type": "pong"})
                elif msg_type == "typing":
                    conversation_id = msg.get("conversation_id")
                    # Broadcast typing indicator to other participants
                    await websocket.send_json({"type": "typing_ack", "conversation_id": conversation_id})

            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "Invalid JSON"})

    except WebSocketDisconnect:
        message_manager.disconnect(user_id, websocket)
        logger.info("WebSocket /messages disconnected: user=%s", user_id)


@app.websocket("/ws/notifications")
async def websocket_notifications(websocket: WebSocket, token: str = ""):
    """WebSocket endpoint for real-time notifications."""
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
    logger.info("WebSocket /notifications connected: user=%s", user_id)

    # Send unread notification count on connect
    try:
        from uuid import UUID as _UUID

        from app.database import get_session
        from app.models.notification import Notification
        from sqlmodel import select

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
        logger.info("WebSocket /notifications disconnected: user=%s", user_id)
