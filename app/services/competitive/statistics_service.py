from __future__ import annotations

"""
Statistics Service — Competitive Arena Phase 1.

Only lazily creates the zero-state row per student in this phase — no
match ever completes yet, so nothing updates these counters. Future phases
will increment them from the Match Engine's completion handler.
"""

from uuid import UUID

from sqlmodel import Session

from app.models.competitive import CompetitiveStatistics


def get_or_create_statistics(db: Session, user_id: UUID) -> CompetitiveStatistics:
    stats = db.get(CompetitiveStatistics, user_id)
    if stats is not None:
        return stats
    stats = CompetitiveStatistics(user_id=user_id)
    db.add(stats)
    db.commit()
    db.refresh(stats)
    return stats
