from __future__ import annotations

"""
Statistics Service — Competitive Arena Phase 1.

Lazily creates the zero-state row per student. Initial Arena Rating (MMR)
is read from PlatformSettings.competitive_mmr_initial_rating (Phase 6 —
"Administrator configurable... do not hardcode") rather than the model
field's own default, so an admin change takes effect for every new player
immediately without a migration.
"""

from uuid import UUID

from sqlmodel import Session

from app.models.admin import PlatformSettings
from app.models.competitive import CompetitiveStatistics


def get_or_create_statistics(db: Session, user_id: UUID) -> CompetitiveStatistics:
    stats = db.get(CompetitiveStatistics, user_id)
    if stats is not None:
        return stats
    settings_row = db.get(PlatformSettings, True) or PlatformSettings()
    stats = CompetitiveStatistics(user_id=user_id, rating=settings_row.competitive_mmr_initial_rating)
    db.add(stats)
    db.commit()
    db.refresh(stats)
    return stats
