from __future__ import annotations

"""Multi-account monitoring for referral farming (Point 5.3, migration 112,
business/EP audit 2026-09-08). No device/IP infrastructure existed
anywhere — scoped narrowly to the one concrete abuse vector already
identified (referral EP farming via fake accounts), not a platform-wide
fingerprinting system. Purely observational: nothing here blocks a
referral or a user."""

from uuid import uuid4

from app.models.profile import Profile
from app.services.referral import apply_referral_code, get_or_create_code


def _make_profile(db_session, **overrides):
    fields = {"id": uuid4(), "email": f"{uuid4()}@test.local", "first_name": "Test", "last_name": "User"}
    fields.update(overrides)
    profile = Profile(**fields)
    db_session.add(profile)
    db_session.commit()
    return profile


def test_apply_referral_code_stores_referee_ip(db_session):
    referrer = _make_profile(db_session)
    code = get_or_create_code(referrer.id, "student", db_session)

    referee = _make_profile(db_session)
    apply_referral_code(referee.id, "student", code, db_session, referee_ip="203.0.113.5")

    from sqlmodel import select
    from app.models.referral import Referral
    row = db_session.exec(select(Referral).where(Referral.referee_id == referee.id)).first()
    assert row.referee_ip == "203.0.113.5"


def test_admin_suspicious_ips_flags_shared_ip_above_threshold(db_session):
    from app.routers.admin.referrals import admin_list_suspicious_referral_ips

    referrer = _make_profile(db_session)
    code = get_or_create_code(referrer.id, "student", db_session)

    # Three distinct "referees" all applying from the SAME IP — the
    # farming pattern this endpoint is meant to surface.
    shared_ip = "198.51.100.7"
    for _ in range(3):
        referee = _make_profile(db_session)
        apply_referral_code(referee.id, "student", code, db_session, referee_ip=shared_ip)

    # One legitimate, unrelated referral from a different IP.
    lone_referee = _make_profile(db_session)
    apply_referral_code(lone_referee.id, "student", code, db_session, referee_ip="192.0.2.99")

    result = admin_list_suspicious_referral_ips(min_count=2, _={}, db=db_session)

    assert len(result) == 1
    assert result[0].referee_ip == shared_ip
    assert result[0].referral_count == 3


def test_admin_suspicious_ips_does_not_flag_a_lone_ip(db_session):
    from app.routers.admin.referrals import admin_list_suspicious_referral_ips

    referrer = _make_profile(db_session)
    code = get_or_create_code(referrer.id, "student", db_session)
    referee = _make_profile(db_session)
    lone_ip = "203.0.113.9"
    apply_referral_code(referee.id, "student", code, db_session, referee_ip=lone_ip)

    result = admin_list_suspicious_referral_ips(min_count=2, _={}, db=db_session)
    assert lone_ip not in [group.referee_ip for group in result]
