"""Single source of truth for the country<->currency pairing rule introduced
by migration 100 (international teachers). Mirrors the DB's
chk_teacher_country_currency_pair CHECK constraint exactly — keep both in
sync if a 3rd currency/country group is ever added. Used by both
app/routers/onboarding.py (teacher onboarding) and app/routers/teachers.py
(post-onboarding profile edits) so the two enforcement points can never
drift apart.
"""

from __future__ import annotations


def currency_for_country(country: str) -> str:
    """DZ -> DZD, everything else -> EUR."""
    return "DZD" if country == "DZ" else "EUR"
