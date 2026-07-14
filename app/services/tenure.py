"""Teacher tenure — "years of experience" shown on teacher profiles is, for
V1, simply years since the teacher registered on E-NOVAR (not a self-reported
real-world career length, which nothing in the product currently collects or
edits). Computed live from Profile.created_at everywhere it's displayed, so
it advances on its own every year — never stored, never manually set.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional


def experience_years_from(created_at: Optional[datetime]) -> int:
    if created_at is None:
        return 0
    return max(0, (datetime.utcnow() - created_at).days // 365)
