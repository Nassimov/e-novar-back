from __future__ import annotations

"""
Phase 16 — Production Hardening: focused, real automated tests.

Not exhaustive coverage of all 16 phases (that would need a much larger
test investment than this pass) — targets the genuinely new/changed Phase
16 surfaces: rate limiting, feature flags, moderation (reports/sanctions/
appeals) with real enforcement side-effects, concurrency-safe reward
claims, and the fraud-detection heuristic addition.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.admin import PlatformSettings
from app.models.competitive import CompetitiveRatingHistory, CompetitiveStatistics
from app.models.liveops import ArenaMission, ArenaPlayerMission
from app.models.moderation import ArenaSanction
from app.models.profile import Profile


def _make_profile(db_session, **overrides):
    # full_name is a DB-generated column (first_name || ' ' || last_name) —
    # never set directly, matching the real Postgres schema's behavior.
    fields = {"id": uuid4(), "email": f"{uuid4()}@test.local", "first_name": "Test", "last_name": "User", "role": "student"}
    fields.update(overrides)
    profile = Profile(**fields)
    db_session.add(profile)
    db_session.commit()
    db_session.refresh(profile)
    return profile


# ─── Rate limiter ───────────────────────────────────────────────────────────

def test_rate_limiter_allows_under_limit():
    from app.core.rate_limiter import check_rate_limit

    fake_redis = MagicMock()
    fake_redis.incr.return_value = 1
    with patch("app.core.rate_limiter.get_redis_client", return_value=fake_redis):
        check_rate_limit(key="test:key", limit=5, window_seconds=10)  # must not raise


def test_rate_limiter_blocks_over_limit():
    from app.core.rate_limiter import check_rate_limit

    fake_redis = MagicMock()
    fake_redis.incr.return_value = 6
    with patch("app.core.rate_limiter.get_redis_client", return_value=fake_redis):
        with pytest.raises(HTTPException) as exc_info:
            check_rate_limit(key="test:key", limit=5, window_seconds=10)
        assert exc_info.value.status_code == 429


def test_rate_limiter_fails_open_on_redis_error():
    from app.core.rate_limiter import check_rate_limit

    with patch("app.core.rate_limiter.get_redis_client", side_effect=ConnectionError("redis down")):
        check_rate_limit(key="test:key", limit=1, window_seconds=10)  # must not raise


# ─── Feature flags ──────────────────────────────────────────────────────────

def test_feature_flag_default_enabled(db_session):
    from app.services.competitive import feature_flags_service
    assert feature_flags_service.is_enabled(db_session, "battle_royale") is True


def test_feature_flag_toggle_updates_in_memory_settings_row():
    # PlatformSettings has several legacy Postgres-ARRAY-typed columns
    # (unrelated to Phase 16) that SQLite's DBAPI can't bind values for —
    # a test-infra limitation, not a Phase 16 correctness question — so
    # this exercises set_flag's actual logic against a real PlatformSettings
    # instance without round-tripping it through the SQLite test DB.
    from app.services.competitive import feature_flags_service

    settings_row = PlatformSettings()
    fake_db = MagicMock()
    fake_db.get.return_value = settings_row

    assert feature_flags_service.is_enabled(fake_db, "battle_royale") is True
    feature_flags_service.set_flag(fake_db, "battle_royale", False)
    assert settings_row.feature_battle_royale_enabled is False
    assert feature_flags_service.is_enabled(fake_db, "battle_royale") is False


def test_feature_flag_unknown_name_rejected(db_session):
    from app.services.competitive import feature_flags_service
    with pytest.raises(HTTPException):
        feature_flags_service.set_flag(db_session, "not_a_real_flag", False)


# ─── Moderation: reports -> sanctions -> real enforcement -> appeals ───────

def test_issue_suspension_sanction_sets_real_enforcement_field(db_session):
    from app.services.competitive import moderation_service, statistics_service

    user = _make_profile(db_session)
    admin = _make_profile(db_session, role="admin")
    statistics_service.get_or_create_statistics(db_session, user.id)

    sanction = moderation_service.issue_sanction(
        db_session, user_id=user.id, sanction_type="suspension_temporary",
        reason="test", issued_by=admin.id, duration_days=3,
    )
    stats = db_session.get(CompetitiveStatistics, user.id)
    assert stats.suspended_until is not None
    # SQLite (test DB only) doesn't round-trip tzinfo the way Postgres
    # timestamptz does — compare naively.
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    suspended_until_naive = stats.suspended_until.replace(tzinfo=None) if stats.suspended_until.tzinfo else stats.suspended_until
    assert suspended_until_naive > now_naive
    assert sanction.status == "active"


def test_revoke_sanction_clears_enforcement(db_session):
    from app.services.competitive import moderation_service, statistics_service

    user = _make_profile(db_session)
    admin = _make_profile(db_session, role="admin")
    statistics_service.get_or_create_statistics(db_session, user.id)

    sanction = moderation_service.issue_sanction(
        db_session, user_id=user.id, sanction_type="suspension_temporary",
        reason="test", issued_by=admin.id, duration_days=3,
    )
    moderation_service.revoke_sanction(db_session, sanction.id, admin_id=admin.id)

    stats = db_session.get(CompetitiveStatistics, user.id)
    assert stats.suspended_until is None
    refreshed = db_session.get(ArenaSanction, sanction.id)
    assert refreshed.status == "revoked"


def test_appeal_accepted_revokes_sanction(db_session):
    from app.services.competitive import moderation_service, statistics_service

    user = _make_profile(db_session)
    admin = _make_profile(db_session, role="admin")
    statistics_service.get_or_create_statistics(db_session, user.id)

    sanction = moderation_service.issue_sanction(
        db_session, user_id=user.id, sanction_type="mute_temporary", reason="test", issued_by=admin.id,
    )
    appeal = moderation_service.submit_appeal(db_session, user_id=user.id, sanction_id=sanction.id, message="please review")
    assert appeal.status == "pending"

    moderation_service.review_appeal(db_session, appeal.id, admin_id=admin.id, decision="accepted")
    refreshed_sanction = db_session.get(ArenaSanction, sanction.id)
    assert refreshed_sanction.status == "revoked"


def test_duplicate_appeal_rejected(db_session):
    from app.services.competitive import moderation_service

    user = _make_profile(db_session)
    admin = _make_profile(db_session, role="admin")
    sanction = moderation_service.issue_sanction(db_session, user_id=user.id, sanction_type="warning", reason="test", issued_by=admin.id)
    moderation_service.submit_appeal(db_session, user_id=user.id, sanction_id=sanction.id, message="first")
    with pytest.raises(HTTPException) as exc_info:
        moderation_service.submit_appeal(db_session, user_id=user.id, sanction_id=sanction.id, message="second")
    assert exc_info.value.status_code == 409


def test_report_to_sanction_marks_report_actioned(db_session):
    from app.services.competitive import moderation_service

    reporter = _make_profile(db_session)
    target = _make_profile(db_session)
    admin = _make_profile(db_session, role="admin")

    report = moderation_service.submit_report(
        db_session, reporter_id=reporter.id, target_type="user", target_id=target.id,
        category="cheating", reason="suspicious play",
    )
    moderation_service.issue_sanction(
        db_session, user_id=target.id, sanction_type="warning", reason="reviewed",
        issued_by=admin.id, report_id=report.id,
    )
    from app.models.admin import Report
    refreshed = db_session.get(Report, report.id)
    assert refreshed.status == "actioned"
    assert refreshed.reviewed_by == admin.id


def test_merge_reports(db_session):
    from app.services.competitive import moderation_service

    reporter = _make_profile(db_session)
    target = _make_profile(db_session)
    admin = _make_profile(db_session, role="admin")

    r1 = moderation_service.submit_report(db_session, reporter_id=reporter.id, target_type="user", target_id=target.id, category="spam", reason=None)
    r2 = moderation_service.submit_report(db_session, reporter_id=reporter.id, target_type="user", target_id=target.id, category="spam", reason=None)
    merged = moderation_service.merge_reports(db_session, r1.id, into_report_id=r2.id, admin_id=admin.id)
    assert merged.status == "merged"
    assert merged.merged_into_id == r2.id


# ─── Concurrency: idempotent claims ─────────────────────────────────────────

def test_claim_mission_twice_raises_conflict(db_session):
    from app.services.competitive import mission_service

    user = _make_profile(db_session)
    mission = ArenaMission(
        code=f"test_{uuid4()}", title="Test mission", mission_type="counter", period="daily",
        metric_key="test_metric", target_value=1, status="published",
    )
    db_session.add(mission)
    db_session.commit()
    db_session.refresh(mission)

    player_mission = ArenaPlayerMission(
        user_id=user.id, mission_id=mission.id, period_key="test-period",
        target_value=1, progress_current=1, status="completed",
    )
    db_session.add(player_mission)
    db_session.commit()
    db_session.refresh(player_mission)

    result = mission_service.claim_mission(db_session, user.id, player_mission.id)
    assert result["player_mission_id"] == player_mission.id

    with pytest.raises(HTTPException) as exc_info:
        mission_service.claim_mission(db_session, user.id, player_mission.id)
    assert exc_info.value.status_code == 409


# ─── Fraud detection: reward farming heuristic ─────────────────────────────

def test_reward_farming_signal_logged_above_threshold(db_session):
    from app.services.competitive import anti_abuse_service
    from app.services.competitive import event_log_service

    user = _make_profile(db_session)
    now = datetime.now(timezone.utc)
    for i in range(anti_abuse_service._REWARD_FARMING_MATCH_THRESHOLD):
        db_session.add(CompetitiveRatingHistory(
            user_id=user.id, match_id=uuid4(), rating_before=1000, rating_after=1005,
            delta=5, result="win", created_at=now - timedelta(minutes=1),
        ))
    db_session.commit()

    logged = {"called": False}
    original = event_log_service.log_event

    def _spy(db, *, match_id, actor_id, event_type, meta=None):
        if event_type == "anti_abuse_reward_farming_signal":
            logged["called"] = True
        return original(db, match_id=match_id, actor_id=actor_id, event_type=event_type, meta=meta)

    with patch("app.services.competitive.anti_abuse_service.event_log_service.log_event", side_effect=_spy):
        anti_abuse_service._check_reward_farming(db_session, match_id=uuid4(), user_id=user.id)

    assert logged["called"] is True


def test_reward_farming_signal_not_logged_below_threshold(db_session):
    from app.services.competitive import anti_abuse_service, event_log_service

    user = _make_profile(db_session)
    now = datetime.now(timezone.utc)
    db_session.add(CompetitiveRatingHistory(
        user_id=user.id, match_id=uuid4(), rating_before=1000, rating_after=1005,
        delta=5, result="win", created_at=now,
    ))
    db_session.commit()

    logged = {"called": False}

    def _spy(*args, **kwargs):
        logged["called"] = True

    with patch("app.services.competitive.anti_abuse_service.event_log_service.log_event", side_effect=_spy):
        anti_abuse_service._check_reward_farming(db_session, match_id=uuid4(), user_id=user.id)

    assert logged["called"] is False
