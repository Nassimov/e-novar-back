from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Tuple
from uuid import UUID

from sqlalchemy import func
from sqlmodel import Session, select

from app.models.kp import KpAccount, KpSource, KpTransaction, KpTransactionType

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


def _get_account_locked(uid: UUID, db: Session) -> KpAccount:
    """Row-locks the balance for the remainder of this DB transaction —
    without this, two concurrent award_kp/spend_kp calls for the same user
    (double-click, retry, multi-device) can each read the same stale
    balance, both pass their own check, and both write, producing an
    incoherent balance (in spend_kp's case, one nothing in the DB used to
    stop from going negative — see migration 109). A second concurrent
    caller blocks here until this one commits, then sees the up-to-date
    balance and (for spend_kp) the already-inserted idempotent transaction
    if there is one. Mirrors the same pattern already used by
    app/services/store.py's redeem_item()."""
    account = db.exec(
        select(KpAccount).where(KpAccount.user_id == uid).with_for_update()
    ).first()
    if account is None:
        account = get_or_create_kp_account(uid, db)
    return account


def _remaining_daily_allowance(db: Session, uid: UUID, source: KpSource) -> Optional[int]:
    """None = uncapped (no admin-configured cap for this source — today's
    default). Otherwise, how much of this source's per-user daily cap is
    left, computed from actual 'earn' transactions since UTC midnight —
    never trusts a client-declared count, always re-derived from the
    ledger. Admin sets caps via PlatformSettings.kp_source_daily_caps
    (Point 5.4 — farming limits); this only enforces whatever value the
    admin chose, never invents one."""
    from app.services.pricing import get_platform_settings

    caps = get_platform_settings(db).kp_source_daily_caps or {}
    source_key = source.value if hasattr(source, "value") else str(source)
    cap = caps.get(source_key)
    if not cap:
        return None

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    earned_today = db.exec(
        select(func.sum(KpTransaction.amount)).where(
            KpTransaction.user_id == uid,
            KpTransaction.source == source_key,
            KpTransaction.transaction_type == KpTransactionType.earn.value,
            KpTransaction.created_at >= today_start,
        )
    ).one() or 0
    return max(0, int(cap) - int(earned_today))


def _find_existing(
    db: Session,
    *,
    user_id: UUID,
    ref_type: Optional[str],
    ref_id,
    transaction_type: str,
    idempotency_key: Optional[str],
) -> Optional[KpTransaction]:
    """Looks for a transaction that already satisfies this exact request —
    called only while the KpBalance row lock above is held, so this check
    and the insert that follows it are atomic together. Two independent
    dedup keys are supported: (ref_type, ref_id, transaction_type) for
    event-driven awards (a homework grade, a referral validation...), and
    (user_id, idempotency_key) for a caller with no natural ref_id."""
    if ref_type and ref_id is not None:
        existing = db.exec(
            select(KpTransaction).where(
                KpTransaction.ref_type == ref_type,
                KpTransaction.ref_id == ref_id,
                KpTransaction.transaction_type == transaction_type,
            )
        ).first()
        if existing is not None:
            return existing
    if idempotency_key:
        existing = db.exec(
            select(KpTransaction).where(
                KpTransaction.user_id == user_id,
                KpTransaction.idempotency_key == idempotency_key,
            )
        ).first()
        if existing is not None:
            return existing
    return None


def award_kp(
    user_id,
    amount: int,
    source: KpSource,
    label: str,
    db: Session,
    *,
    ref_type: Optional[str] = None,
    ref_id=None,
    idempotency_key: Optional[str] = None,
    transaction_type: str = KpTransactionType.earn.value,
) -> Tuple[KpAccount, bool]:
    """
    Award KP to a user.
    Applies active ep_boost multiplier transparently before crediting.
    Returns (updated_account, level_up_occurred) — a no-op replay (same
    ref_type+ref_id+transaction_type, or same idempotency_key, seen before)
    returns the account UNCHANGED and level_up_occurred=False rather than
    granting a second time.
    Balance/XP are maintained by the Supabase trigger apply_kp_transaction();
    Python must NOT modify them directly — only insert the KpTransaction.
    """
    from uuid import UUID as _UUID

    uid = _UUID(str(user_id)) if not isinstance(user_id, _UUID) else user_id

    account = _get_account_locked(uid, db)

    if _find_existing(
        db, user_id=uid, ref_type=ref_type, ref_id=ref_id,
        transaction_type=transaction_type, idempotency_key=idempotency_key,
    ) is not None:
        return account, False

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

    # Admin-configured per-source daily cap (Point 5.4 — farming limits).
    # Clamped, never rejected outright: a capped user still gets whatever
    # allowance remains today, down to a silent 0 — grading a homework or
    # finishing a match should never error out just because the student
    # farmed that same source earlier today.
    if amount > 0 and transaction_type == KpTransactionType.earn.value:
        remaining = _remaining_daily_allowance(db, uid, source)
        if remaining is not None:
            amount = min(amount, remaining)
            if amount <= 0:
                return account, False

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
        transaction_type=transaction_type,
        ref_type=ref_type,
        ref_id=ref_id,
        idempotency_key=idempotency_key,
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
    *,
    ref_type: Optional[str] = None,
    ref_id=None,
    idempotency_key: Optional[str] = None,
) -> Tuple[KpAccount, bool]:
    """Deduct KP from a user's balance. Raises ValueError if insufficient.
    A replayed request (same ref_type+ref_id, or same idempotency_key) is a
    no-op returning the account unchanged, not a second deduction — the
    second element of the tuple tells the caller which happened (True = a
    real deduction just occurred, False = this was a deduped replay), since
    some callers need to know before doing their own side effect (e.g.
    app/services/boost.py only extends boost_expires_at on a real spend —
    otherwise a retried request would extend it twice for one payment)."""
    uid = UUID(str(user_id)) if not isinstance(user_id, UUID) else user_id
    account = _get_account_locked(uid, db)

    if _find_existing(
        db, user_id=uid, ref_type=ref_type, ref_id=ref_id,
        transaction_type=KpTransactionType.spend.value, idempotency_key=idempotency_key,
    ) is not None:
        return account, False

    if account.balance < amount:
        raise ValueError(f"Insufficient KP balance: {account.balance} < {amount}")

    # Balance is maintained by the apply_kp_transaction() trigger — insert only.
    db.add(KpTransaction(
        user_id=account.user_id,
        label=label,
        source=KpSource.reward,
        amount=-amount,
        transaction_type=KpTransactionType.spend.value,
        ref_type=ref_type,
        ref_id=ref_id,
        idempotency_key=idempotency_key,
    ))
    db.commit()
    db.refresh(account)

    return account, True


def reverse_kp_transaction(
    *,
    ref_type: str,
    ref_id,
    reason: str,
    db: Session,
    actor_id=None,
) -> Optional[KpAccount]:
    """Undoes a prior award for the given (ref_type, ref_id) — e.g. a
    homework grade gets un-graded, a referral is found fraudulent, a
    session credited with EP gets disputed/cancelled after the fact.
    Writes a NEW 'reversal' transaction with the opposite amount rather
    than editing/deleting the original row (immutable history — Point
    5.8). Idempotent: calling twice for the same event only reverses once.
    Returns None if there was nothing to reverse (unknown ref, or already
    reversed)."""
    original = db.exec(
        select(KpTransaction).where(
            KpTransaction.ref_type == ref_type,
            KpTransaction.ref_id == ref_id,
            KpTransaction.transaction_type.in_([
                KpTransactionType.earn.value, KpTransactionType.bonus.value,
            ]),
        )
    ).first()
    if original is None or original.amount == 0:
        return None

    account = _get_account_locked(original.user_id, db)

    if _find_existing(
        db, user_id=original.user_id, ref_type=ref_type, ref_id=ref_id,
        transaction_type=KpTransactionType.reversal.value, idempotency_key=None,
    ) is not None:
        return account  # already reversed — no-op

    db.add(KpTransaction(
        user_id=original.user_id,
        label=reason,
        source=original.source,
        amount=-original.amount,
        transaction_type=KpTransactionType.reversal.value,
        actor_id=actor_id,
        ref_type=ref_type,
        ref_id=ref_id,
    ))
    db.commit()
    db.refresh(account)
    return account


def adjust_kp_balance(
    *,
    user_id,
    delta: int,
    reason: str,
    actor_id,
    db: Session,
) -> KpAccount:
    """Manual admin correction to a user's EP balance — the only place
    outside award_kp/spend_kp allowed to move a balance, and the only one
    that requires a reason + actor. Always journaled to audit_logs in
    addition to kp_transactions (Point 5.8). Never allowed to push the
    balance negative, same floor as an ordinary spend."""
    if delta == 0:
        raise ValueError("delta must be non-zero")

    uid = UUID(str(user_id)) if not isinstance(user_id, UUID) else user_id
    account = _get_account_locked(uid, db)

    if delta < 0 and account.balance < -delta:
        raise ValueError(f"Insufficient KP balance: {account.balance} < {-delta}")

    balance_before = account.balance

    db.add(KpTransaction(
        user_id=uid,
        label=reason,
        source=KpSource.reward,
        amount=delta,
        transaction_type=KpTransactionType.adjustment.value,
        actor_id=actor_id,
    ))

    from app.models.admin import AuditLog
    db.add(AuditLog(
        actor_id=actor_id,
        action="kp_balance_adjustment",
        target_type="user",
        target_id=uid,
        meta={
            "delta": delta,
            "reason": reason,
            "balance_before": balance_before,
        },
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
