from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from uuid import UUID

from sqlmodel import Session, select

from app.models.enums import KpSource
from app.models.kp import KpBalance, KpTransaction
from app.models.store import RewardClaim, StoreItem, UserEntitlement
from app.services.kp import get_or_create_kp_account


EFFECT_DESCRIPTIONS: dict[str, str] = {
    "ep_boost_2x_24h":     "Boost ×2 EP actif pendant 24h",
    "ai_boost_5q":         "5 questions IA créditées",
    "streak_shield_1d":    "Streak protégé pour 1 jour",
    "premium_30d":         "Accès E-NOVAR Premium pendant 30 jours",
    "pdf_pack_bac":        "Pack annales BAC PDF déverrouillé",
    "stickers_pack":       "Pack stickers E-NOVAR activé",
    "coaching_credit":     "Crédit coaching enregistré — l'équipe vous contactera sous 48h",
    "psychometric_credit": "Bilan psychométrique enregistré — l'équipe vous contactera sous 48h",
    "voucher_carrefour":   "Bon Carrefour enregistré — l'équipe vous transmettra votre code sous 48h",
    "travel_booking":      "Demande de voyage enregistrée — l'équipe vous contactera sous 72h",
}

EFFECT_AUTO_APPROVE = {
    "ep_boost_2x_24h",
    "ai_boost_5q",
    "streak_shield_1d",
    "premium_30d",
    "pdf_pack_bac",
    "stickers_pack",
}


def _calc_expiry(effect_type: str, config: dict) -> Optional[datetime]:
    now = datetime.now(timezone.utc)
    hours = config.get("duration_hours")
    days = config.get("duration_days")
    if hours:
        return now + timedelta(hours=int(hours))
    if days:
        return now + timedelta(days=int(days))
    # Defaults per effect type
    if effect_type == "ep_boost_2x_24h":
        return now + timedelta(hours=24)
    if effect_type == "streak_shield_1d":
        return now + timedelta(days=1)
    if effect_type == "premium_30d":
        return now + timedelta(days=30)
    if effect_type in ("ai_boost_5q", "pdf_pack_bac", "stickers_pack",
                       "coaching_credit", "psychometric_credit",
                       "voucher_carrefour", "travel_booking"):
        return None  # no expiry / admin-set
    return None


def redeem_item(
    user_id: UUID,
    item_id: str,
    db: Session,
) -> Tuple[RewardClaim, Optional[UserEntitlement], KpBalance, str]:
    """
    Atomically:
      1. Validate item (active, in stock, level ok, balance ok)
      2. Deduct KP (balance update + KpTransaction)
      3. Decrement stock if limited
      4. Create RewardClaim
      5. Create UserEntitlement for auto-processed effects
    Returns (claim, entitlement_or_None, updated_account, human_message).
    Raises ValueError for business-rule violations (mapped to HTTP 400 by caller).
    """
    now = datetime.now(timezone.utc)

    # Lock item row to prevent concurrent stock races
    stmt = (
        select(StoreItem)
        .where(StoreItem.id == item_id, StoreItem.active == True)
        .with_for_update()
    )
    item = db.exec(stmt).first()
    if not item:
        raise ValueError("Article introuvable ou inactif.")

    # Stock check
    if item.stock is not None and item.stock <= 0:
        raise ValueError("Stock épuisé.")

    # KP account (lock for update — prevents concurrent balance races)
    account = db.exec(
        select(KpBalance).where(KpBalance.user_id == user_id).with_for_update()
    ).first()
    if account is None:
        # Create on the fly (new user with no KP yet)
        account = get_or_create_kp_account(user_id, db)

    # Level check
    if account.level < item.level_required:
        raise ValueError(
            f"Niveau insuffisant : niveau {item.level_required} requis "
            f"(votre niveau : {account.level})."
        )

    # Balance check
    if account.balance < item.cost:
        raise ValueError(
            f"Solde EP insuffisant : {account.balance} EP disponibles, "
            f"{item.cost} EP requis."
        )

    # Deduct balance
    account.balance -= item.cost
    account.updated_at = now
    db.add(account)

    # KP deduction transaction
    kp_tx = KpTransaction(
        user_id=user_id,
        amount=-item.cost,
        source=KpSource.reward,
        label=f"Échange marketplace : {item.name}",
    )
    db.add(kp_tx)

    # Decrement stock
    if item.stock is not None:
        item.stock -= 1
        db.add(item)

    # Determine claim status
    auto_approve = bool(item.effect_type) and item.effect_type in EFFECT_AUTO_APPROVE
    claim_status = "approved" if auto_approve else "pending"

    # Create claim
    claim = RewardClaim(
        user_id=user_id,
        item_id=item_id,
        cost=item.cost,
        status=claim_status,
        processed_at=now if auto_approve else None,
    )
    db.add(claim)
    db.flush()  # get claim.id before creating entitlement

    # Create entitlement for auto-processed effects
    entitlement: Optional[UserEntitlement] = None
    if item.effect_type:
        config = item.effect_config or {}
        expires_at = _calc_expiry(item.effect_type, config)
        entitlement = UserEntitlement(
            user_id=user_id,
            claim_id=claim.id,
            effect_type=item.effect_type,
            effect_config=config,
            status="active",
            expires_at=expires_at,
        )
        db.add(entitlement)

    db.commit()
    db.refresh(account)
    db.refresh(claim)
    if entitlement:
        db.refresh(entitlement)

    msg = EFFECT_DESCRIPTIONS.get(item.effect_type or "", "Échange enregistré !")
    return claim, entitlement, account, msg
