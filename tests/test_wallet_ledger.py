from __future__ import annotations

"""Teacher DZD wallet ledger (business audit, 2026-09-08).

wallet_balance_dzd used to be a plain mutable column with no transaction
history, credited/debited directly by application code at 4 different
call sites, each reimplementing its own locking (or not locking at all).
Covers the new ledger (app/services/wallet.py) and its rewiring into:
  - credit_session_payout (session payout)
  - request_dzd_withdrawal (withdrawal request)
  - process_withdrawal's reject branch (withdrawal refund)
  - reject_validation's clawback (session dispute)
"""

import datetime as dt
from uuid import uuid4

import pytest

from app.models.admin import AuditLog
from app.models.enums import WalletTransactionType
from app.models.profile import Profile, TeacherProfile
from app.models.wallet import TeacherWalletTransaction
from app.services.wallet import adjust_wallet_balance, credit_wallet, debit_wallet


def _make_profile(db_session, **overrides):
    fields = {"id": uuid4(), "email": f"{uuid4()}@test.local", "first_name": "Test", "last_name": "User"}
    fields.update(overrides)
    profile = Profile(**fields)
    db_session.add(profile)
    db_session.commit()
    return profile


def _make_teacher(db_session, **overrides):
    profile = _make_profile(db_session)
    tp = TeacherProfile(user_id=profile.id, **overrides)
    db_session.add(tp)
    db_session.commit()
    return tp


def _tx_count(db_session, teacher_id, **where) -> int:
    from sqlmodel import select

    stmt = select(TeacherWalletTransaction).where(TeacherWalletTransaction.teacher_id == teacher_id)
    for k, v in where.items():
        stmt = stmt.where(getattr(TeacherWalletTransaction, k) == v)
    return len(db_session.exec(stmt).all())


# ── credit / debit basics ───────────────────────────────────────────────────

def test_credit_wallet_increases_balance_and_journals(db_session):
    tp = _make_teacher(db_session)
    tp, was_credited = credit_wallet(tp.user_id, 500, "session_payout", "Séance validée", db_session)
    assert tp.wallet_balance_dzd == 500
    assert was_credited is True
    assert _tx_count(db_session, tp.user_id, transaction_type=WalletTransactionType.credit.value) == 1


def test_debit_wallet_decreases_balance_and_journals(db_session):
    tp = _make_teacher(db_session)
    credit_wallet(tp.user_id, 1000, "session_payout", "Séance validée", db_session)
    tp, actual = debit_wallet(tp.user_id, 300, "withdrawal_request", "Retrait", db_session)
    assert tp.wallet_balance_dzd == 700
    assert actual == 300


def test_debit_wallet_rejects_insufficient_balance(db_session):
    tp = _make_teacher(db_session)
    credit_wallet(tp.user_id, 100, "session_payout", "Séance validée", db_session)
    with pytest.raises(ValueError):
        debit_wallet(tp.user_id, 200, "withdrawal_request", "Retrait", db_session)
    assert db_session.get(TeacherProfile, tp.user_id).wallet_balance_dzd == 100


def test_debit_wallet_clamp_takes_remaining_without_raising(db_session):
    """The clawback path (reject_validation) must never crash — a teacher
    who already withdrew past the clawback amount still gets debited down
    to 0, with the shortfall reported back to the caller for logging."""
    tp = _make_teacher(db_session)
    credit_wallet(tp.user_id, 100, "session_payout", "Séance validée", db_session)
    tp, actual = debit_wallet(tp.user_id, 500, "clawback", "Reprise", db_session, clamp=True)
    assert actual == 100
    assert tp.wallet_balance_dzd == 0


# ── idempotency ──────────────────────────────────────────────────────────────

def test_credit_wallet_idempotent_replay_does_not_double_credit(db_session):
    tp = _make_teacher(db_session)
    ref_id = uuid4()
    credit_wallet(tp.user_id, 500, "session_payout", "Séance", db_session, ref_type="session_payout", ref_id=ref_id)
    tp2, was_credited = credit_wallet(tp.user_id, 500, "session_payout", "Séance", db_session, ref_type="session_payout", ref_id=ref_id)
    assert tp2.wallet_balance_dzd == 500
    assert was_credited is False


def test_debit_wallet_idempotent_replay_does_not_double_debit(db_session):
    tp = _make_teacher(db_session)
    credit_wallet(tp.user_id, 1000, "session_payout", "Séance", db_session)
    ref_id = uuid4()
    debit_wallet(tp.user_id, 300, "withdrawal_request", "Retrait", db_session, ref_type="teacher_payout", ref_id=ref_id)
    tp2, actual = debit_wallet(tp.user_id, 300, "withdrawal_request", "Retrait", db_session, ref_type="teacher_payout", ref_id=ref_id)
    assert tp2.wallet_balance_dzd == 700
    assert actual == 0


# ── admin adjustment ─────────────────────────────────────────────────────────

def test_adjust_wallet_balance_credits_and_journals(db_session):
    tp = _make_teacher(db_session)
    admin = _make_profile(db_session)

    tp = adjust_wallet_balance(teacher_id=tp.user_id, delta=1000, reason="Compensation", actor_id=admin.id, db=db_session)
    assert tp.wallet_balance_dzd == 1000

    from sqlmodel import select
    txn = db_session.exec(
        select(TeacherWalletTransaction).where(
            TeacherWalletTransaction.teacher_id == tp.user_id,
            TeacherWalletTransaction.transaction_type == WalletTransactionType.adjustment.value,
        )
    ).first()
    assert txn is not None
    assert txn.actor_id == admin.id

    audit = db_session.exec(
        select(AuditLog).where(AuditLog.action == "wallet_balance_adjustment", AuditLog.target_id == tp.user_id)
    ).first()
    assert audit is not None
    assert audit.meta["delta"] == 1000


def test_adjust_wallet_balance_rejects_debit_below_zero(db_session):
    tp = _make_teacher(db_session)
    admin = _make_profile(db_session)
    credit_wallet(tp.user_id, 100, "session_payout", "Séance", db_session)

    with pytest.raises(ValueError):
        adjust_wallet_balance(teacher_id=tp.user_id, delta=-500, reason="Erreur", actor_id=admin.id, db=db_session)
    assert db_session.get(TeacherProfile, tp.user_id).wallet_balance_dzd == 100


# ── end-to-end: real call sites ─────────────────────────────────────────────

def test_dzd_withdrawal_request_debits_via_ledger(db_session):
    from app.routers.teachers import request_dzd_withdrawal
    from app.schemas.teacher import DzdWithdrawalRequest

    tp = _make_teacher(db_session, iban="00000000000000000000", bank_holder="Test Teacher", payout_rail="bank")
    credit_wallet(tp.user_id, 1000, "session_payout", "Séance", db_session)

    result = request_dzd_withdrawal(
        DzdWithdrawalRequest(amount_dzd=400), current_user={"id": str(tp.user_id)}, db=db_session,
    )
    assert result["remaining_balance"] == 600
    assert _tx_count(db_session, tp.user_id, transaction_type=WalletTransactionType.debit.value) == 1


def test_admin_process_withdrawal_reject_refunds_via_ledger(db_session):
    from app.models.payment import TeacherPayout
    from app.routers.admin.content import process_withdrawal
    from app.schemas.admin import WithdrawalProcessRequest

    tp = _make_teacher(db_session)
    credit_wallet(tp.user_id, 1000, "session_payout", "Séance", db_session)
    tp2, _ = debit_wallet(tp.user_id, 300, "withdrawal_request", "Retrait", db_session)
    assert tp2.wallet_balance_dzd == 700

    payout = TeacherPayout(teacher_id=tp.user_id, source="wallet", ep_amount=0, dzd_amount=300)
    db_session.add(payout)
    db_session.commit()

    process_withdrawal(
        payout.id, WithdrawalProcessRequest(action="reject"),
        current_user={"id": str(uuid4())}, db=db_session,
    )
    assert db_session.get(TeacherProfile, tp.user_id).wallet_balance_dzd == 1000
    assert _tx_count(db_session, tp.user_id, transaction_type=WalletTransactionType.credit.value, source="withdrawal_rejected") == 1
