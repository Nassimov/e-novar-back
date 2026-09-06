from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session

from app.config import get_settings
from app.dependencies import get_current_user, get_db
from app.services import ai as ai_service

settings = get_settings()
router = APIRouter(tags=["ai"])

_QUOTA_TTL = 86400 * 2   # Redis key TTL: 2 days


class ExplainRequest(BaseModel):
    subject: str
    question: str
    level: str = "secondaire"
    stream: bool = False


class HomeworkHintRequest(BaseModel):
    homework_id: Optional[UUID] = None
    subject: str
    title: str
    statement: str
    level: str = "secondaire"


class HomeworkSolveRequest(BaseModel):
    homework_id: Optional[UUID] = None
    subject: str
    title: str
    statement: str
    level: str = "secondaire"


class QuizGenerateRequest(BaseModel):
    subject: str
    level: str
    num_questions: int = 5


class QuizEvaluateRequest(BaseModel):
    question: str
    user_answer: str
    correct_answer: str


# ── Quota helpers ─────────────────────────────────────────────────────────────

def _redis_quota_key(user_id: str) -> str:
    today = datetime.utcnow().date().isoformat()
    return f"ai:daily:{user_id}:{today}"


def _check_and_consume_quota(user_id: str, db: Session) -> None:
    """
    Enforce per-user daily AI quota.
    Order of checks:
      1. settings.ai_free_daily_quota == 0 → unlimited (quota disabled globally)
      2. User has active 'premium' entitlement → unlimited
      3. Daily Redis counter < free quota → allow + increment
      4. User has active 'ai_boost' entitlement with remaining questions → consume + allow
      5. All exhausted → HTTP 429
    Never raises on Redis failures — fails open to avoid blocking users.
    """
    if settings.ai_free_daily_quota <= 0:
        return  # quota globally disabled

    uid = UUID(user_id)

    # Premium check (time-expensive but done first to short-circuit Redis)
    try:
        from app.services.effects import expire_stale_entitlements, get_active_premium
        expire_stale_entitlements(uid, db)
        if get_active_premium(uid, db):
            return
    except Exception:
        pass

    # Redis daily counter
    try:
        from app.core.redis import get_redis_client
        r = get_redis_client()
        key = _redis_quota_key(user_id)
        count = int(r.get(key) or 0)

        if count < settings.ai_free_daily_quota:
            r.incr(key)
            r.expire(key, _QUOTA_TTL)
            return

        # Free quota exhausted — try ai_boost
        from app.services.effects import get_active_ai_boost, consume_ai_boost_question
        boost = get_active_ai_boost(uid, db)
        if boost:
            consume_ai_boost_question(boost, db)
            db.commit()
            r.incr(key)
            r.expire(key, _QUOTA_TTL)
            return

        raise HTTPException(
            status_code=429,
            detail=(
                f"Quota IA journalier atteint ({settings.ai_free_daily_quota} questions/jour). "
                "Échangez un 'Coup de pouce IA' dans la Boutique EP pour continuer."
            ),
        )
    except HTTPException:
        raise
    except Exception:
        pass  # Redis unavailable — fail open


def _current_quota_status(user_id: str, db: Session) -> dict:
    """Return quota info for the current user (used by GET /quota)."""
    uid = UUID(user_id)
    try:
        from app.services.effects import get_active_premium, get_active_ai_boost, expire_stale_entitlements
        expire_stale_entitlements(uid, db)

        if settings.ai_free_daily_quota <= 0:
            return {"unlimited": True, "reason": "global", "remaining_free": -1, "boost_remaining": 0}

        if get_active_premium(uid, db):
            return {"unlimited": True, "reason": "premium", "remaining_free": -1, "boost_remaining": 0}

        from app.core.redis import get_redis_client
        r = get_redis_client()
        used = int(r.get(_redis_quota_key(user_id)) or 0)
        remaining_free = max(0, settings.ai_free_daily_quota - used)

        boost = get_active_ai_boost(uid, db)
        boost_remaining = int((boost.effect_config or {}).get("remaining", 0)) if boost else 0

        return {
            "unlimited": False,
            "reason": None,
            "remaining_free": remaining_free,
            "boost_remaining": boost_remaining,
            "daily_quota": settings.ai_free_daily_quota,
        }
    except Exception:
        return {"unlimited": True, "reason": "error", "remaining_free": -1, "boost_remaining": 0}


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/quota")
def get_ai_quota(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the current user's AI quota status."""
    return _current_quota_status(current_user["id"], db)


@router.post("/explain")
def explain_concept(
    payload: ExplainRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _check_and_consume_quota(current_user["id"], db)

    if payload.stream:
        def generate():
            for chunk in ai_service.explain_concept_stream(payload.subject, payload.question, payload.level):
                yield chunk
        return StreamingResponse(generate(), media_type="text/plain")

    explanation = ai_service.explain_concept(payload.subject, payload.question, payload.level)
    return {"explanation": explanation, "subject": payload.subject, "level": payload.level}


@router.post("/homework/hint")
def get_homework_hint(
    payload: HomeworkHintRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _check_and_consume_quota(current_user["id"], db)
    hint = ai_service.generate_homework_hint({
        "subject": payload.subject,
        "title": payload.title,
        "statement": payload.statement,
        "level": payload.level,
    })
    return {"hint": hint}


@router.post("/homework/solve")
def solve_homework(
    payload: HomeworkSolveRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _check_and_consume_quota(current_user["id"], db)
    solution = ai_service.solve_homework({
        "subject": payload.subject,
        "title": payload.title,
        "statement": payload.statement,
        "level": payload.level,
    })
    return {"solution": solution}


@router.post("/practice/generate")
def generate_practice(
    payload: QuizGenerateRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _check_and_consume_quota(current_user["id"], db)
    try:
        questions = ai_service.generate_quiz(payload.subject, payload.level, payload.num_questions)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to generate quiz: {exc}")
    return {"questions": questions, "subject": payload.subject, "level": payload.level}


@router.post("/practice/evaluate")
def evaluate_practice(
    payload: QuizEvaluateRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    # Evaluation is automatic feedback — not quota-limited
    try:
        result = ai_service.evaluate_quiz_answer(payload.question, payload.user_answer, payload.correct_answer)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to evaluate answer: {exc}")
    return result


# NOTE: a `/progress-report` and a `/session-summary/{id}` endpoint used to live
# here, but both referenced fields that don't exist on `TutoringSession`
# (`.subject`, `.rating`, `.duration_minutes`, `.ai_summary`, etc. — the model
# has `.subject_id`, `.duration_min`, `.summary`, no session-level rating at
# all, ratings live on the separate `Review` model). Neither had any frontend
# caller, so they were removed rather than fixed. The equivalent, correct,
# already-wired session-summary flow is `GET /api/sessions/{id}/summary`
# (`app/routers/sessions.py`), which uses the real fields.
