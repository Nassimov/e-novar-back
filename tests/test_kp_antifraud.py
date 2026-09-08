from __future__ import annotations

"""EP/KP anti-fraud fixes (business + tokenomics audit, 2026-09-08).

Covers the three confirmed issues found while auditing app/services/kp.py:
  1. award_kp/spend_kp read the balance with no row lock, so a race could
     drive it negative — now locked (see _get_account_locked) and floored
     by a DB CHECK constraint (migration 109, Postgres-only — not
     exercised here, see test_adjust_kp_balance_db_floor_is_documented).
  2. No idempotency: a retried/duplicated request (or, in production, the
     stray DB trigger removed by migration 109) could award/spend twice
     for the same event — now deduped via (ref_type, ref_id,
     transaction_type) and via idempotency_key.
  3. Reversal had no first-class mechanism (a cancelled/refunded EP-
     granting action could only be "fixed" by hand-editing history) — now
     reverse_kp_transaction() inserts an explicit, auditable reversal row.

Also covers adjust_kp_balance() (the new admin-only balance correction
path, itself journaled twice: kp_transactions + audit_logs).
"""

from uuid import uuid4

import pytest

from app.models.admin import AuditLog
from app.models.enums import KpSource, KpTransactionType
from app.models.homework import Homework, HomeworkGrade, HomeworkStatus
from app.models.kp import KpTransaction
from app.models.profile import Profile
from app.services.kp import (
    adjust_kp_balance,
    award_kp,
    get_or_create_kp_account,
    reverse_kp_transaction,
    spend_kp,
)


def _make_profile(db_session, **overrides):
    fields = {"id": uuid4(), "email": f"{uuid4()}@test.local", "first_name": "Test", "last_name": "User"}
    fields.update(overrides)
    profile = Profile(**fields)
    db_session.add(profile)
    db_session.commit()
    return profile


def _tx_count(db_session, user_id, **where) -> int:
    from sqlmodel import select

    stmt = select(KpTransaction).where(KpTransaction.user_id == user_id)
    for k, v in where.items():
        stmt = stmt.where(getattr(KpTransaction, k) == v)
    return len(db_session.exec(stmt).all())


# ── award_kp / spend_kp idempotency ─────────────────────────────────────────

def test_award_kp_idempotent_replay_does_not_double_credit(db_session):
    student = _make_profile(db_session)
    ref_id = uuid4()

    account1, leveled_up1 = award_kp(
        student.id, 50, KpSource.homework, "Devoir noté", db_session,
        ref_type="homework_grade", ref_id=ref_id,
    )
    assert account1.balance == 50

    # Same event replayed (retried request, or — in production — the old
    # trigger this migration removed) must be a no-op, not a second credit.
    account2, leveled_up2 = award_kp(
        student.id, 50, KpSource.homework, "Devoir noté", db_session,
        ref_type="homework_grade", ref_id=ref_id,
    )
    assert account2.balance == 50
    assert leveled_up2 is False
    assert _tx_count(db_session, student.id, ref_type="homework_grade", ref_id=ref_id) == 1


def test_spend_kp_idempotent_replay_does_not_double_debit(db_session):
    student = _make_profile(db_session)
    award_kp(student.id, 100, KpSource.reward, "Crédit initial", db_session)
    ref_id = uuid4()

    account1, was_spent1 = spend_kp(student.id, 30, "Achat", db_session, ref_type="store_claim", ref_id=ref_id)
    assert account1.balance == 70
    assert was_spent1 is True

    account2, was_spent2 = spend_kp(student.id, 30, "Achat", db_session, ref_type="store_claim", ref_id=ref_id)
    assert account2.balance == 70  # unchanged — replay, not a second debit
    assert was_spent2 is False


def test_spend_kp_idempotent_via_idempotency_key(db_session):
    student = _make_profile(db_session)
    award_kp(student.id, 100, KpSource.reward, "Crédit initial", db_session)

    key = "double-click-guard-1"
    spend_kp(student.id, 40, "Achat", db_session, idempotency_key=key)
    account, was_spent = spend_kp(student.id, 40, "Achat", db_session, idempotency_key=key)
    assert account.balance == 60
    assert was_spent is False


# ── insufficient balance / floor ────────────────────────────────────────────

def test_spend_kp_rejects_insufficient_balance(db_session):
    student = _make_profile(db_session)
    award_kp(student.id, 10, KpSource.reward, "Crédit", db_session)

    with pytest.raises(ValueError):
        spend_kp(student.id, 20, "Trop cher", db_session)

    account = get_or_create_kp_account(student.id, db_session)
    assert account.balance == 10  # rejected spend must not partially apply


def test_sequential_spends_correctly_deplete_balance(db_session):
    """Not a true concurrency test (SQLite/pytest is single-threaded), but
    verifies the lock-then-check-then-write ordering that makes concurrent
    calls safe: each spend_kp call re-reads the balance AFTER acquiring the
    lock, so back-to-back spends that together exceed the balance can't
    both succeed — the second correctly sees the first's effect and fails
    instead of both reading the pre-spend balance."""
    student = _make_profile(db_session)
    award_kp(student.id, 100, KpSource.reward, "Crédit", db_session)

    spend_kp(student.id, 70, "Premier achat", db_session)
    with pytest.raises(ValueError):
        spend_kp(student.id, 70, "Deuxième achat (ne devrait pas passer)", db_session)

    account = get_or_create_kp_account(student.id, db_session)
    assert account.balance == 30


# ── reversal ─────────────────────────────────────────────────────────────────

def test_reverse_kp_transaction_undoes_an_earn(db_session):
    student = _make_profile(db_session)
    ref_id = uuid4()
    award_kp(
        student.id, 50, KpSource.homework, "Devoir noté", db_session,
        ref_type="homework_grade", ref_id=ref_id,
    )
    assert get_or_create_kp_account(student.id, db_session).balance == 50

    account = reverse_kp_transaction(
        ref_type="homework_grade", ref_id=ref_id, reason="Devoir annulé par le professeur",
        db=db_session, actor_id=None,
    )
    assert account.balance == 0
    assert _tx_count(db_session, student.id, ref_type="homework_grade", ref_id=ref_id, transaction_type=KpTransactionType.reversal.value) == 1


def test_reverse_kp_transaction_is_idempotent(db_session):
    student = _make_profile(db_session)
    ref_id = uuid4()
    award_kp(student.id, 50, KpSource.homework, "Devoir noté", db_session, ref_type="homework_grade", ref_id=ref_id)

    reverse_kp_transaction(ref_type="homework_grade", ref_id=ref_id, reason="Annulé", db=db_session)
    account = reverse_kp_transaction(ref_type="homework_grade", ref_id=ref_id, reason="Annulé (retry)", db=db_session)

    assert account.balance == 0  # not -50 — second reversal is a no-op
    assert _tx_count(db_session, student.id, ref_type="homework_grade", ref_id=ref_id, transaction_type=KpTransactionType.reversal.value) == 1


def test_reverse_kp_transaction_unknown_ref_returns_none(db_session):
    assert reverse_kp_transaction(ref_type="homework_grade", ref_id=uuid4(), reason="x", db=db_session) is None


# ── admin adjustment ─────────────────────────────────────────────────────────

def test_adjust_kp_balance_credits_and_journals(db_session):
    student = _make_profile(db_session)
    admin = _make_profile(db_session)

    account = adjust_kp_balance(
        user_id=student.id, delta=200, reason="Compensation panne technique",
        actor_id=admin.id, db=db_session,
    )
    assert account.balance == 200

    from sqlmodel import select
    txn = db_session.exec(
        select(KpTransaction).where(
            KpTransaction.user_id == student.id,
            KpTransaction.transaction_type == KpTransactionType.adjustment.value,
        )
    ).first()
    assert txn is not None
    assert txn.actor_id == admin.id
    assert txn.amount == 200

    audit = db_session.exec(
        select(AuditLog).where(AuditLog.action == "kp_balance_adjustment", AuditLog.target_id == student.id)
    ).first()
    assert audit is not None
    assert audit.actor_id == admin.id
    assert audit.meta["delta"] == 200
    assert audit.meta["balance_before"] == 0


def test_adjust_kp_balance_rejects_debit_below_zero(db_session):
    student = _make_profile(db_session)
    admin = _make_profile(db_session)
    award_kp(student.id, 10, KpSource.reward, "Crédit", db_session)

    with pytest.raises(ValueError):
        adjust_kp_balance(user_id=student.id, delta=-50, reason="Erreur", actor_id=admin.id, db=db_session)

    assert get_or_create_kp_account(student.id, db_session).balance == 10


# ── homework double-award regression (the confirmed bug) ────────────────────

def test_homework_grading_awards_kp_exactly_once(db_session):
    """Before migration 109, EVERY graded homework awarded KP twice: once
    via app/routers/homework.py's explicit award_kp() call, and a second
    time via a stray DB trigger (trg_homework_grade) that fired on the same
    HomeworkGrade insert with a different formula. The trigger only exists
    in the real Postgres schema (removed by the migration, not exercised
    by this SQLite suite) — what IS exercised here, and is the actual
    mechanism that makes the fix safe even under a retried request, is
    that a second award for the same (ref_type, ref_id) is a no-op."""
    teacher = _make_profile(db_session)
    student = _make_profile(db_session)
    hw = Homework(
        id=uuid4(), teacher_id=teacher.id, student_id=student.id,
        title="Exercice", statement="...", kp_reward=50, status=HomeworkStatus.submitted,
    )
    db_session.add(hw)
    db_session.commit()

    grade = HomeworkGrade(homework_id=hw.id, teacher_id=teacher.id, score=20.0, kp_awarded=50)
    db_session.add(grade)
    db_session.commit()
    db_session.refresh(grade)

    award_kp(
        student.id, 50, KpSource.homework, f"Devoir noté: {hw.title} (20/20)", db_session,
        ref_type="homework_grade", ref_id=grade.id,
    )

    # Simulates what the removed trigger (or a retried HTTP request) would
    # have done: a second award for the exact same graded homework.
    award_kp(
        student.id, 50, KpSource.homework, f"Devoir noté: {hw.title} (20/20)", db_session,
        ref_type="homework_grade", ref_id=grade.id,
    )

    account = get_or_create_kp_account(student.id, db_session)
    assert account.balance == 50  # not 100
    assert _tx_count(db_session, student.id, ref_type="homework_grade", ref_id=grade.id) == 1
