from __future__ import annotations

import math
from typing import Any, Dict, List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.kp import KpAccount, KpTransaction
from app.models.user import User
from app.schemas.kp import (
    KpBalanceResponse,
    KpTransactionResponse,
    LeaderboardEntry,
)
from app.services.kp import get_or_create_kp_account, LEVEL_THRESHOLDS

router = APIRouter(tags=["kp"])

# NOTE (business/EP audit, 2026-09-08): this router used to also expose
# POST /spend, GET /badges and POST /badges/{id}/unlock, backed by two
# hardcoded Python lists (BADGES, STORE_REWARDS) that predate — and are
# unrelated to — the real reward systems (app/services/store.py's
# StoreItem-backed redeem_item, and app/routers/student_badges.py's
# DB-backed Badge/UserBadge). Confirmed via grep that no frontend code
# called any of the three (src/lib/api/*.ts has zero references), yet they
# were live, reachable-with-any-auth-token endpoints. /spend in particular
# took `amount` straight from the request body and handed it to spend_kp()
# with no server-side derivation from a real cost — exactly the "a user
# sends amount=1000000 and gets it" failure mode. Removed rather than
# patched: they were dead weight duplicating a system that already exists
# and works correctly.


@router.get("/balance", response_model=KpBalanceResponse)
def get_kp_balance(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get the current user's KP balance and level info."""
    stmt = select(User).where(User.id == UUID(current_user["id"]))
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    account = get_or_create_kp_account(user.id, db)
    return KpBalanceResponse.model_validate(account)


@router.get("/transactions", response_model=Dict)
def get_kp_transactions(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List KP transactions for the current user."""
    stmt = select(User).where(User.id == UUID(current_user["id"]))
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    # Paginated at the SQL level — this used to load the user's ENTIRE
    # transaction history on every call (Python-side slicing), which only
    # gets slower the longer someone has been using the platform (every
    # session/challenge/reward adds a row).
    total = db.exec(
        select(func.count()).select_from(
            select(KpTransaction.id).where(KpTransaction.user_id == user.id).subquery()
        )
    ).one()
    offset = (page - 1) * size
    paginated = db.exec(
        select(KpTransaction)
        .where(KpTransaction.user_id == user.id)
        .order_by(KpTransaction.created_at.desc())
        .offset(offset)
        .limit(size)
    ).all()

    return {
        "items": [KpTransactionResponse.model_validate(t) for t in paginated],
        "total": total,
        "page": page,
        "size": size,
        "pages": math.ceil(total / size) if total else 0,
    }


@router.get("/levels")
def get_levels():
    """Get all KP level definitions."""
    levels = []
    for i, threshold in enumerate(LEVEL_THRESHOLDS):
        levels.append({
            "level": i + 1,
            "xp_required": threshold,
            "title": _get_level_title(i + 1),
        })
    return {"levels": levels}


def _get_level_title(level: int) -> str:
    titles = {
        1: "Débutant",
        2: "Apprenant",
        3: "Studieux",
        4: "Avancé",
        5: "Expert",
        6: "Maître",
        7: "Légende",
    }
    return titles.get(level, f"Niveau {level}")


@router.get("/leaderboard", response_model=List[LeaderboardEntry])
def get_leaderboard(
    period: str = Query("week", pattern="^(week|month|all)$"),
    size: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
):
    """Get the KP leaderboard."""
    if period == "week":
        accounts = db.exec(
            select(KpAccount).order_by(KpAccount.week_earned.desc()).limit(size)
        ).all()
        score_field = "week_earned"
    else:
        accounts = db.exec(
            select(KpAccount).order_by(KpAccount.total_earned.desc()).limit(size)
        ).all()
        score_field = "total_earned"

    user_ids = [account.user_id for account in accounts]
    users_by_id = {
        u.id: u for u in db.exec(select(User).where(User.id.in_(user_ids))).all()
    }

    result = []
    for rank, account in enumerate(accounts, 1):
        user = users_by_id.get(account.user_id)
        if user:
            score = account.week_earned if period == "week" else account.total_earned
            result.append(LeaderboardEntry(
                rank=rank,
                user_id=account.user_id,
                full_name=user.full_name,
                avatar_url=user.avatar_url,
                level=account.level,
                xp=account.xp,
                week_earned=account.week_earned,
            ))
    return result
