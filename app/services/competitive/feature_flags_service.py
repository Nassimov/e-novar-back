from __future__ import annotations

"""
Feature Flags — Competitive Arena Phase 16.

Flags live directly on PlatformSettings (the platform's existing single
source of admin-configurable knobs — no separate "feature_flags" table,
same "everything configurable, nothing hardcoded" posture every prior
phase already followed for its own settings). Toggling one takes effect on
the next request — no deploy required.
"""

import logging
from typing import Dict

from fastapi import Depends, HTTPException, status
from sqlmodel import Session

from app.dependencies import get_db
from app.models.admin import PlatformSettings

logger = logging.getLogger(__name__)

#: flag_name -> PlatformSettings column name.
FLAGS = {
    "battle_royale": "feature_battle_royale_enabled",
    "tournament": "feature_tournament_enabled",
    "replay": "feature_replay_enabled",
    "ai_analysis": "feature_ai_analysis_enabled",
    "ranked": "feature_ranked_enabled",
    "liveops": "feature_liveops_enabled",
    "clubs": "feature_clubs_enabled",
    "spectator": "feature_spectator_enabled",
}


def _settings(db: Session) -> PlatformSettings:
    return db.get(PlatformSettings, True) or PlatformSettings()


def is_enabled(db: Session, flag_name: str) -> bool:
    column = FLAGS.get(flag_name)
    if column is None:
        return True  # unknown flag name — fail open rather than 500 on a typo
    return bool(getattr(_settings(db), column, True))


def list_flags(db: Session) -> Dict[str, bool]:
    settings_row = _settings(db)
    return {name: bool(getattr(settings_row, column)) for name, column in FLAGS.items()}


def set_flag(db: Session, flag_name: str, enabled: bool) -> Dict[str, bool]:
    column = FLAGS.get(flag_name)
    if column is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Fonctionnalité inconnue.")
    settings_row = _settings(db)
    setattr(settings_row, column, enabled)
    db.add(settings_row)
    db.commit()
    return list_flags(db)


def require_feature(flag_name: str):
    """FastAPI dependency factory — Depends(require_feature("battle_royale"))
    on a router (applies to every route under it) or a single endpoint."""

    def _dependency(db: Session = Depends(get_db)) -> None:
        if not is_enabled(db, flag_name):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cette fonctionnalité est actuellement désactivée.")

    return _dependency
