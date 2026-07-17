"""Teacher tenure — "years of experience" shown on teacher profiles is, for
V1, simply years since the teacher registered on E-NOVAR (not a self-reported
real-world career length, which nothing in the product currently collects or
edits). Computed live from Profile.created_at everywhere it's displayed, so
it advances on its own every year — never stored, never manually set.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def experience_years_from(created_at: Optional[datetime]) -> int:
    if created_at is None:
        return 0
    now = datetime.now(timezone.utc)
    # Postgres timestamptz columns come back tz-aware; naive-UTC columns don't —
    # normalize both to aware UTC so the subtraction below never raises.
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return max(0, (now - created_at).days // 365)
