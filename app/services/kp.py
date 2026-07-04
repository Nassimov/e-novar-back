from __future__ import annotations

from typing import Optional, Tuple

from sqlmodel import Session, select

from app.models.kp import KpAccount, KpSource, KpTransaction

LEVEL_THRESHOLDS = [0, 500, 1500, 3500, 7000, 12000, 20000]


def get_or_create_kp_account(user_id, db: Session) -> KpAccount:
    """Get or create a KP account for a user."""
    from uuid import UUID
    uid = UUID(str(user_id)) if not isinstance(user_id, UUID) else user_id
    statement = select(KpAccount).where(KpAccount.user_id == uid)
    account = db.exec(statement).first()
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
        db.flush()  # flush within the current transaction rather than committing early
        db.refresh(account)
    return account


def award_kp(
    user_id,
    amount: int,
    source: KpSource,
    label: str,
    db: Session,
) -> Tuple[KpAccount, bool]:
    """Award KP to a user. Returns (updated_account, level_up_occurred)."""
    from datetime import datetime

    account = get_or_create_kp_account(user_id, db)

    account.balance += amount
    account.total_earned += amount
    account.week_earned += amount
    account.xp += amount

    leveled_up, new_level = check_level_up(account)
    if leveled_up:
        account.level = new_level
        next_idx = new_level  # level 1 = index 0, so next threshold is at index `level`
        if next_idx < len(LEVEL_THRESHOLDS):
            account.next_level_at = LEVEL_THRESHOLDS[next_idx]
        else:
            account.next_level_at = account.xp + 99999  # max level

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
    """Check if the user qualifies for a level up based on current XP.

    Returns (leveled_up: bool, new_level: int).
    """
    current_level = kp_account.level
    xp = kp_account.xp

    new_level = current_level
    for i, threshold in enumerate(LEVEL_THRESHOLDS):
        if xp >= threshold:
            new_level = i + 1  # levels are 1-indexed
        else:
            break

    leveled_up = new_level > current_level
    return leveled_up, new_level
