from __future__ import annotations

import math
from typing import Any, Dict, List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.kp import KpAccount, KpTransaction
from app.models.user import User
from app.schemas.kp import (
    BadgeResponse,
    KpBalanceResponse,
    KpSpendRequest,
    KpTransactionResponse,
    LeaderboardEntry,
)
from app.services.kp import award_kp, get_or_create_kp_account, spend_kp, LEVEL_THRESHOLDS

router = APIRouter(tags=["kp"])

BADGES = [
    BadgeResponse(id="first_booking", name="Première réservation", description="Première session réservée", required_level=1, kp_cost=0),
    BadgeResponse(id="week_streak", name="Semaine de feu", description="7 jours consécutifs d'activité", required_level=2, kp_cost=0),
    BadgeResponse(id="top_student", name="Top Étudiant", description="Dans le top 10 hebdomadaire", required_level=3, kp_cost=200),
    BadgeResponse(id="homework_master", name="Maître des devoirs", description="10 devoirs complétés", required_level=2, kp_cost=100),
    BadgeResponse(id="challenger", name="Challenger", description="5 challenges complétés", required_level=3, kp_cost=300),
    BadgeResponse(id="referral_king", name="Ambassadeur", description="5 amis référencés", required_level=2, kp_cost=200),
]

STORE_REWARDS = [
    {"id": "discount_10", "name": "Réduction 10%", "description": "10% de réduction sur la prochaine session", "kp_cost": 500, "type": "discount"},
    {"id": "discount_20", "name": "Réduction 20%", "description": "20% de réduction sur la prochaine session", "kp_cost": 900, "type": "discount"},
    {"id": "free_session", "name": "Session gratuite", "description": "Une session offerte avec un tuteur", "kp_cost": 2000, "type": "session"},
    {"id": "priority_support", "name": "Support prioritaire", "description": "Accès prioritaire au support pendant 1 mois", "kp_cost": 300, "type": "perk"},
]


@router.get("/balance", response_model=KpBalanceResponse)
async def get_kp_balance(
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
async def get_kp_transactions(
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

    transactions = db.exec(
        select(KpTransaction)
        .where(KpTransaction.user_id == user.id)
        .order_by(KpTransaction.created_at.desc())
    ).all()

    total = len(transactions)
    offset = (page - 1) * size
    paginated = transactions[offset: offset + size]

    return {
        "items": [KpTransactionResponse.model_validate(t) for t in paginated],
        "total": total,
        "page": page,
        "size": size,
        "pages": math.ceil(total / size) if total else 0,
    }


@router.post("/spend")
async def spend_kp_endpoint(
    payload: KpSpendRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Spend KP points on a reward."""
    stmt = select(User).where(User.id == UUID(current_user["id"]))
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    try:
        account = spend_kp(user.id, payload.amount, payload.label, db)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "message": f"Spent {payload.amount} KP",
        "new_balance": account.balance,
        "reward_id": payload.reward_id,
    }


@router.get("/levels")
async def get_levels():
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


@router.get("/badges", response_model=List[BadgeResponse])
async def list_badges(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List all available badges and unlock status."""
    stmt = select(User).where(User.id == UUID(current_user["id"]))
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    account = get_or_create_kp_account(user.id, db)
    result = []
    for badge in BADGES:
        b = badge.model_copy()
        b.is_unlocked = account.level >= badge.required_level
        result.append(b)
    return result


@router.post("/badges/{badge_id}/unlock")
async def unlock_badge(
    badge_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Unlock a badge using KP."""
    stmt = select(User).where(User.id == UUID(current_user["id"]))
    user = db.exec(stmt).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    badge = next((b for b in BADGES if b.id == badge_id), None)
    if badge is None:
        raise HTTPException(status_code=404, detail="Badge not found")

    if badge.kp_cost > 0:
        try:
            spend_kp(user.id, badge.kp_cost, f"Badge unlocked: {badge.name}", db)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    return {"message": f"Badge '{badge.name}' unlocked!", "badge_id": badge_id}


@router.get("/leaderboard", response_model=List[LeaderboardEntry])
async def get_leaderboard(
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
