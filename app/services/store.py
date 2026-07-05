"""
Store redemption service.

Generic effect type system:
  ep_boost      — config: {multiplier: N, duration_hours: H}     auto-approved
  ai_boost      — config: {questions: N}                          auto-approved
  streak_shield — config: {duration_hours: H}                     auto-approved
  premium       — config: {duration_days: N}                      auto-approved
  pdf_pack      — config: {pack_id: str}                          auto-approved
  stickers_pack — config: {count: N, pack_id: str}               auto-approved
  coaching_credit     — config: {duration_minutes: N}             pending → admin
  psychometric_credit — config: {type: str}                       pending → admin
  voucher_carrefour   — config: {amount_dzd: N, brand: str}       pending → admin
  travel_booking      — config: {destination: str, type: str}     pending → admin
  (none/null)   — physical items: pending → admin
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from uuid import UUID

from sqlmodel import Session, select

from app.models.enums import KpSource
from app.models.kp import KpBalance, KpTransaction
from app.models.store import RewardClaim, StoreItem, UserEntitlement
from app.services.kp import get_or_create_kp_account

logger = logging.getLogger(__name__)

# ── Effect registry ───────────────────────────────────────────────────────────

EFFECT_DESCRIPTIONS: dict[str, str] = {
    "ep_boost":             "Boost EP ×{multiplier} actif pendant {duration_hours}h",
    "ai_boost":             "{questions} questions IA créditées",
    "streak_shield":        "Streak protégé pour {duration_hours}h",
    "premium":              "Accès E-NOVAR Premium pendant {duration_days} jours",
    "pdf_pack":             "Pack PDF déverrouillé — téléchargez vos documents dans la boutique",
    "stickers_pack":        "Pack de {count} stickers E-NOVAR activé",
    "coaching_credit":      "Crédit coaching enregistré — l'équipe vous contactera sous 48h",
    "psychometric_credit":  "Bilan psychométrique enregistré — l'équipe vous contactera sous 48h",
    "voucher_carrefour":    "Bon Carrefour enregistré — votre code vous sera transmis sous 48h",
    "travel_booking":       "Demande de voyage enregistrée — l'équipe vous contactera sous 72h",
}

EFFECT_AUTO_APPROVE = {
    "ep_boost",
    "ai_boost",
    "streak_shield",
    "premium",
    "pdf_pack",
    "stickers_pack",
}

# Manual effects whose claims stay pending and trigger a notification email
EFFECT_MANUAL_NOTIFY = {
    "coaching_credit",
    "psychometric_credit",
    "voucher_carrefour",
    "travel_booking",
}

# Effects where only ONE active entitlement is allowed at a time.
# Serialised via the KpBalance SELECT FOR UPDATE lock — the check happens
# after acquiring that lock, so concurrent requests are correctly serialised.
EFFECT_EXCLUSIVE = {"ep_boost", "premium", "streak_shield"}

# Labels used in user-facing error messages for EFFECT_EXCLUSIVE types
_EXCLUSIVE_LABELS: dict[str, str] = {
    "ep_boost":      "Boost EP",
    "premium":       "accès Premium",
    "streak_shield": "protection Streak",
}

# Manual-review effects that block a new purchase while any claim on the
# SAME item is still pending (prevents admin queue flooding).
EFFECT_PENDING_EXCLUSIVE = {
    "coaching_credit",
    "psychometric_credit",
    "voucher_carrefour",
    "travel_booking",
}


def format_effect_description(effect_type: str, config: dict) -> str:
    """Return a human-readable description with config values interpolated."""
    template = EFFECT_DESCRIPTIONS.get(effect_type, "Échange enregistré !")
    try:
        return template.format(**{k: v for k, v in config.items()})
    except KeyError:
        return template


def _calc_expiry(effect_type: str, config: dict) -> Optional[datetime]:
    now = datetime.now(timezone.utc)
    hours = config.get("duration_hours")
    days = config.get("duration_days")
    if hours:
        return now + timedelta(hours=int(hours))
    if days:
        return now + timedelta(days=int(days))
    # Type-based defaults when config doesn't specify duration
    defaults: dict[str, timedelta] = {
        "ep_boost":      timedelta(hours=24),
        "streak_shield": timedelta(hours=24),
        "premium":       timedelta(days=30),
    }
    return now + defaults[effect_type] if effect_type in defaults else None


def _build_entitlement_config(effect_type: str, item_config: dict) -> dict:
    """Add derived fields to the entitlement config at creation time."""
    config = dict(item_config)
    # ai_boost: initialise remaining question pool
    if effect_type == "ai_boost":
        config.setdefault("remaining", int(config.get("questions", 5)))
    return config


# ── Redeem ────────────────────────────────────────────────────────────────────

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
      5. Create UserEntitlement for effects that have an effect_type
    Single db.commit() — fully atomic.
    Raises ValueError for business-rule violations (mapped to HTTP 400 by caller).
    """
    now = datetime.now(timezone.utc)

    # Lock item row to prevent concurrent stock races
    item = db.exec(
        select(StoreItem)
        .where(StoreItem.id == item_id, StoreItem.active == True)  # noqa: E712
        .with_for_update()
    ).first()
    if not item:
        raise ValueError("Article introuvable ou inactif.")

    if item.stock is not None and item.stock <= 0:
        raise ValueError("Stock épuisé.")

    # Lock KP account to prevent concurrent balance races
    account = db.exec(
        select(KpBalance).where(KpBalance.user_id == user_id).with_for_update()
    ).first()
    if account is None:
        account = get_or_create_kp_account(user_id, db)

    if account.level < item.level_required:
        raise ValueError(
            f"Niveau insuffisant : niveau {item.level_required} requis "
            f"(votre niveau actuel : {account.level})."
        )

    if account.balance < item.cost:
        raise ValueError(
            f"Solde EP insuffisant : {account.balance} EP disponibles, "
            f"{item.cost} EP requis."
        )

    # ── Exclusivity guards (run inside the KpBalance lock — concurrent requests
    #    on the same user are serialised here, so no TOCTOU race is possible) ──

    if item.effect_type in EFFECT_EXCLUSIVE:
        # Block if ANY active entitlement of the same type already exists
        existing = db.exec(
            select(UserEntitlement).where(
                UserEntitlement.user_id == user_id,
                UserEntitlement.effect_type == item.effect_type,
                UserEntitlement.status == "active",
            )
        ).first()
        if existing and (existing.expires_at is None or existing.expires_at > now):
            label = _EXCLUSIVE_LABELS.get(item.effect_type, item.effect_type)
            raise ValueError(
                f"Vous avez déjà un {label} actif. "
                "Attendez qu'il expire avant d'en acheter un autre."
            )

    if item.effect_type in ("pdf_pack", "stickers_pack"):
        # Block same pack_id — buying it twice is a pure EP waste with no benefit
        new_pack_id = (item.effect_config or {}).get("pack_id")
        if new_pack_id:
            existing_packs = db.exec(
                select(UserEntitlement).where(
                    UserEntitlement.user_id == user_id,
                    UserEntitlement.effect_type == item.effect_type,
                    UserEntitlement.status == "active",
                )
            ).all()
            for ep in existing_packs:
                if (ep.effect_config or {}).get("pack_id") == new_pack_id:
                    raise ValueError(
                        "Vous avez déjà accès à ce pack. "
                        "Inutile de l'acheter à nouveau."
                    )

    if item.effect_type in EFFECT_PENDING_EXCLUSIVE:
        # Block while a pending claim for this EXACT item already exists
        pending_claim = db.exec(
            select(RewardClaim).where(
                RewardClaim.user_id == user_id,
                RewardClaim.item_id == item_id,
                RewardClaim.status == "pending",
            )
        ).first()
        if pending_claim:
            raise ValueError(
                "Votre demande précédente pour cet article est encore en cours de traitement. "
                "L'équipe E-NOVAR reviendra vers vous prochainement."
            )

    # Balance is maintained by the apply_kp_transaction() trigger — insert only.
    db.add(KpTransaction(
        user_id=user_id,
        amount=-item.cost,
        source=KpSource.reward,
        label=f"Échange marketplace : {item.name}",
    ))

    if item.stock is not None:
        item.stock -= 1
        db.add(item)

    auto_approve = bool(item.effect_type) and item.effect_type in EFFECT_AUTO_APPROVE
    claim = RewardClaim(
        user_id=user_id,
        item_id=item_id,
        cost=item.cost,
        status="approved" if auto_approve else "pending",
        processed_at=now if auto_approve else None,
    )
    db.add(claim)
    db.flush()  # get claim.id before creating entitlement

    entitlement: Optional[UserEntitlement] = None
    if item.effect_type:
        raw_config = item.effect_config or {}
        ent_config = _build_entitlement_config(item.effect_type, raw_config)
        entitlement = UserEntitlement(
            user_id=user_id,
            claim_id=claim.id,
            effect_type=item.effect_type,
            effect_config=ent_config,
            status="active",
            expires_at=_calc_expiry(item.effect_type, ent_config),
        )
        db.add(entitlement)

    db.commit()
    db.refresh(account)
    db.refresh(claim)
    if entitlement:
        db.refresh(entitlement)

    msg = format_effect_description(item.effect_type or "", item.effect_config or {}) if item.effect_type else "Échange enregistré !"

    # Send notification email (fire-and-forget, non-blocking)
    _notify_redeem(user_id, item, claim, db)

    return claim, entitlement, account, msg


def _notify_redeem(user_id: UUID, item: StoreItem, claim: RewardClaim, db: Session) -> None:
    """Send a confirmation email for the redemption. Silently swallows failures."""
    try:
        from app.models.profile import Profile
        profile = db.exec(select(Profile).where(Profile.id == user_id)).first()
        if not profile or not profile.email:
            return
        from app.workers.email_tasks import send_store_redeem_email
        send_store_redeem_email.delay(
            to=profile.email,
            name=profile.first_name or profile.full_name or "Utilisateur",
            item_name=item.name,
            item_category=item.category,
            effect_type=item.effect_type or "",
            is_auto=claim.status == "approved",
            cost=item.cost,
        )
    except Exception as exc:
        logger.warning("store redeem email skipped: %s", exc)
