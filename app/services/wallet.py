from __future__ import annotations

from typing import Optional, Tuple
from uuid import UUID

from sqlmodel import Session, select

from app.models.profile import TeacherProfile
from app.models.wallet import TeacherWalletTransaction
from app.models.enums import WalletTransactionType


def _get_teacher_locked(teacher_id: UUID, db: Session) -> TeacherProfile:
    """Row-locks teacher_profiles for the remainder of this DB transaction —
    the same pattern app/services/kp.py uses for kp_balances, and the one
    app/routers/teachers.py's request_dzd_withdrawal already used ad hoc
    before this ledger existed. A second concurrent caller blocks here
    until this one commits, then sees the up-to-date balance and (if
    idempotency inputs are given) the already-inserted transaction."""
    tp = db.exec(
        select(TeacherProfile).where(TeacherProfile.user_id == teacher_id).with_for_update()
    ).first()
    if tp is None:
        raise ValueError("Teacher profile not found")
    return tp


def _find_existing(
    db: Session, *, teacher_id: UUID, ref_type: Optional[str], ref_id, transaction_type: str,
) -> Optional[TeacherWalletTransaction]:
    if not (ref_type and ref_id is not None):
        return None
    return db.exec(
        select(TeacherWalletTransaction).where(
            TeacherWalletTransaction.ref_type == ref_type,
            TeacherWalletTransaction.ref_id == ref_id,
            TeacherWalletTransaction.transaction_type == transaction_type,
        )
    ).first()


def credit_wallet(
    teacher_id,
    amount: int,
    source: str,
    label: str,
    db: Session,
    *,
    ref_type: Optional[str] = None,
    ref_id=None,
    actor_id=None,
    transaction_type: str = WalletTransactionType.credit.value,
) -> Tuple[TeacherProfile, bool]:
    """Credit a teacher's DZD wallet. Returns (profile, was_credited) — a
    replayed request (same ref_type+ref_id+transaction_type) is a no-op
    (was_credited=False), not a second credit."""
    tid = UUID(str(teacher_id)) if not isinstance(teacher_id, UUID) else teacher_id
    tp = _get_teacher_locked(tid, db)

    if _find_existing(db, teacher_id=tid, ref_type=ref_type, ref_id=ref_id, transaction_type=transaction_type) is not None:
        return tp, False

    tp.wallet_balance_dzd += amount
    db.add(tp)
    db.add(TeacherWalletTransaction(
        teacher_id=tid, amount=amount, transaction_type=transaction_type, source=source,
        label=label, ref_type=ref_type, ref_id=ref_id, actor_id=actor_id,
    ))
    db.commit()
    db.refresh(tp)
    return tp, True


def debit_wallet(
    teacher_id,
    amount: int,
    source: str,
    label: str,
    db: Session,
    *,
    ref_type: Optional[str] = None,
    ref_id=None,
    actor_id=None,
    clamp: bool = False,
) -> Tuple[TeacherProfile, int]:
    """Debit a teacher's DZD wallet. Raises ValueError if insufficient,
    UNLESS clamp=True (used for the admin clawback on a rejected session
    validation — takes whatever is left rather than failing outright,
    matching the pre-ledger behavior of that one call site). Returns
    (profile, amount_actually_debited) — a replayed request (same
    ref_type+ref_id) is a no-op (0 debited), not a second debit."""
    tid = UUID(str(teacher_id)) if not isinstance(teacher_id, UUID) else teacher_id
    tp = _get_teacher_locked(tid, db)

    if _find_existing(db, teacher_id=tid, ref_type=ref_type, ref_id=ref_id, transaction_type=WalletTransactionType.debit.value) is not None:
        return tp, 0

    actual = amount
    if tp.wallet_balance_dzd < amount:
        if not clamp:
            raise ValueError(f"Insufficient wallet balance: {tp.wallet_balance_dzd} < {amount}")
        actual = tp.wallet_balance_dzd

    if actual <= 0:
        return tp, 0

    tp.wallet_balance_dzd -= actual
    db.add(tp)
    db.add(TeacherWalletTransaction(
        teacher_id=tid, amount=-actual, transaction_type=WalletTransactionType.debit.value, source=source,
        label=label, ref_type=ref_type, ref_id=ref_id, actor_id=actor_id,
    ))
    db.commit()
    db.refresh(tp)
    return tp, actual


def adjust_wallet_balance(
    *,
    teacher_id,
    delta: int,
    reason: str,
    actor_id,
    db: Session,
) -> TeacherProfile:
    """Manual admin correction to a teacher's DZD wallet — mirrors
    app/services/kp.py's adjust_kp_balance. Always journaled to audit_logs
    in addition to teacher_wallet_transactions. Never allowed to push the
    balance negative."""
    if delta == 0:
        raise ValueError("delta must be non-zero")

    tid = UUID(str(teacher_id)) if not isinstance(teacher_id, UUID) else teacher_id
    tp = _get_teacher_locked(tid, db)

    if delta < 0 and tp.wallet_balance_dzd < -delta:
        raise ValueError(f"Insufficient wallet balance: {tp.wallet_balance_dzd} < {-delta}")

    balance_before = tp.wallet_balance_dzd
    tp.wallet_balance_dzd += delta
    db.add(tp)
    db.add(TeacherWalletTransaction(
        teacher_id=tid, amount=delta, transaction_type=WalletTransactionType.adjustment.value,
        source="admin_adjustment", label=reason, actor_id=actor_id,
    ))

    from app.models.admin import AuditLog
    db.add(AuditLog(
        actor_id=actor_id, action="wallet_balance_adjustment", target_type="teacher", target_id=tid,
        meta={"delta": delta, "reason": reason, "balance_before": balance_before},
    ))

    db.commit()
    db.refresh(tp)
    return tp
