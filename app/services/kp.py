from __future__ import annotations

from typing import Optional, Tuple
from uuid import UUID

from sqlmodel import Session, select

from app.models.kp import KpAccount, KpSource, KpTransaction

LEVEL_THRESHOLDS = [0, 500, 1500, 3500, 7000, 12000, 20000]


def get_or_create_kp_account(user_id, db: Session) -> KpAccount:
    uid = UUID(str(user_id)) if not isinstance(user_id, UUID) else user_id
    account = db.exec(select(KpAccount).where(KpAccount.user_id == uid)).first()
    if account is None:
        account = KpAccount(
            user_id=uid,
            balance=0,
            total_earned=0,
            week_earned=0,
            level=1,
            xp=0,
            next_level_at=LEVEL_THRESHOLDS[1],
        )
        db.add(account)
        db.flush()
        db.refresh(account)
    return account


def award_kp(
    user_id,
    amount: int,
    source: KpSource,
    label: str,
    db: Session,
) -> Tuple[KpAccount, bool]:
    """
    Award KP to a user.
    Applies active ep_boost multiplier transparently before crediting.
    Returns (updated_account, level_up_occurred).
    """
    from datetime import datetime
    from uuid import UUID as _UUID

    uid = _UUID(str(user_id)) if not isinstance(user_id, _UUID) else user_id

    # Apply ep_boost multiplier (positive awards only)
    if amount > 0:
        try:
            from app.services.effects import get_active_ep_boost
            boost = get_active_ep_boost(uid, db)
            if boost:
                multiplier = float((boost.effect_config or {}).get("multiplier", 2.0))
                amount = max(1, int(amount * multiplier))
        except Exception:
            pass  # never block KP award on effect lookup failure

    account = get_or_create_kp_account(uid, db)

    account.balance += amount
    account.total_earned += max(0, amount)
    account.week_earned += max(0, amount)
    account.xp += max(0, amount)

    leveled_up, new_level = check_level_up(account)
    if leveled_up:
        account.level = new_level
        next_idx = new_level
        if next_idx < len(LEVEL_THRESHOLDS):
            account.next_level_at = LEVEL_THRESHOLDS[next_idx]
        else:
            account.next_level_at = account.xp + 99999

    account.updated_at = datetime.utcnow()
    db.add(account)

    transaction = KpTransaction(
        user_id=account.user_id,
        label=label,
        source=source,
        amount=amount,
    )
    db.add(transaction)
    db.commit()
    db.refresh(account)

    return account, leveled_up


def spend_kp(
    user_id,
    amount: int,
    label: str,
    db: Session,
) -> KpAccount:
    """Deduct KP from a user's balance. Raises ValueError if insufficient."""
    from datetime import datetime

    account = get_or_create_kp_account(user_id, db)

    if account.balance < amount:
        raise ValueError(f"Insufficient KP balance: {account.balance} < {amount}")

    account.balance -= amount
    account.updated_at = datetime.utcnow()
    db.add(account)

    transaction = KpTransaction(
        user_id=account.user_id,
        label=label,
        source=KpSource.reward,
        amount=-amount,
    )
    db.add(transaction)
    db.commit()
    db.refresh(account)

    return account


def check_level_up(kp_account: KpAccount) -> Tuple[bool, int]:
    current_level = kp_account.level
    xp = kp_account.xp
    new_level = current_level
    for i, threshold in enumerate(LEVEL_THRESHOLDS):
        if xp >= threshold:
            new_level = i + 1
        else:
            break
    return new_level > current_level, new_level
