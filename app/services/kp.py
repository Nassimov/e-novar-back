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
    Balance/XP are maintained by the Supabase trigger apply_kp_transaction();
    Python must NOT modify them directly — only insert the KpTransaction.
    """
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

    # Level-up check against expected XP (trigger will update actual xp in DB)
    expected_xp = account.xp + max(0, amount)
    leveled_up, new_level = _level_for_xp(expected_xp, account.level)
    if leveled_up:
        account.level = new_level
        next_idx = new_level
        account.next_level_at = (
            LEVEL_THRESHOLDS[next_idx] if next_idx < len(LEVEL_THRESHOLDS)
            else expected_xp + 99999
        )
        db.add(account)

    txn = KpTransaction(
        user_id=account.user_id,
        label=label,
        source=source,
        amount=amount,
    )
    db.add(txn)
    db.commit()
    db.refresh(account)
    db.refresh(txn)

    # 'challenge' source is skipped — app/routers/admin/challenges.py's
    # approve_submission already sends a richer "challenge_approved"
    # notification that mentions the EP amount inline; a second generic one
    # would just be noise for that specific flow.
    if amount > 0 and source != KpSource.challenge:
        from app.services.notification_engine import emit
        emit(
            db, event_type="ep_rewarded", user_id=account.user_id,
            context={"amount": amount, "label": label},
            data={"amount": amount, "source": source.value},
            dedup_key=f"ep_rewarded:{txn.id}",
        )

    return account, leveled_up


def spend_kp(
    user_id,
    amount: int,
    label: str,
    db: Session,
) -> KpAccount:
    """Deduct KP from a user's balance. Raises ValueError if insufficient."""
    account = get_or_create_kp_account(user_id, db)

    if account.balance < amount:
        raise ValueError(f"Insufficient KP balance: {account.balance} < {amount}")

    # Balance is maintained by the apply_kp_transaction() trigger — insert only.
    db.add(KpTransaction(
        user_id=account.user_id,
        label=label,
        source=KpSource.reward,
        amount=-amount,
    ))
    db.commit()
    db.refresh(account)

    return account


def _level_for_xp(xp: int, current_level: int) -> Tuple[bool, int]:
    new_level = current_level
    for i, threshold in enumerate(LEVEL_THRESHOLDS):
        if xp >= threshold:
            new_level = i + 1
        else:
            break
    return new_level > current_level, new_level


def check_level_up(kp_account: KpAccount) -> Tuple[bool, int]:
    return _level_for_xp(kp_account.xp, kp_account.level)
