from __future__ import annotations

"""
Cosmetic Inventory & Showcase — Competitive Arena Phase 14.

Genuinely new (unlike the Achievement Engine, which reuses public.badges/
user_badges verbatim — see app/services/arena_achievement_service.py's own
docstring). Every non-EP/badge reward type across Phases 4/7/9/10/11/12/13
(title/sticker/frame/avatar_decoration/arena_xp/banner/effect) has been
"recorded_only" until now — audited but never actually owned or equippable
— because no per-item ownership/equip ledger existed. This module builds
exactly that, closing the loop on every one of those previously-deferred
reward types via reward_service.grant_reward's Phase 14 extension, with
zero changes needed to any of those phases' own call sites.

ONE unified catalogue (ArenaCosmetic, `type` discriminator) rather than 4
near-identical tables (frames/banners/effects/stickers) — same
"discriminator over parallel tables" precedent already used repeatedly in
this codebase (e.g. club_invitations.kind, migration 089's own header)."""

from datetime import datetime, timezone
from typing import Any, List, Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlmodel import Field, SQLModel

COSMETIC_TYPES = ["title", "frame", "banner", "effect", "sticker"]
RARITIES = ["common", "uncommon", "rare", "epic", "legendary", "mythic"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ArenaCosmetic(SQLModel, table=True):
    """Mirrors public.arena_cosmetics — the unified catalogue for title/
    frame/banner/effect/sticker. UNIQUE(type, code)."""

    __tablename__ = "arena_cosmetics"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    type: str = Field()
    code: str = Field()
    name: str = Field()
    description: Optional[str] = Field(default=None)
    icon: Optional[str] = Field(default=None)
    asset_url: Optional[str] = Field(default=None)
    category: Optional[str] = Field(default=None)
    rarity: str = Field(default="common")
    is_hidden: bool = Field(default=False)
    season_id: Optional[UUID] = Field(default=None, foreign_key="competitive_seasons.id")
    event_key: Optional[str] = Field(default=None)
    available_from: Optional[datetime] = Field(default=None)
    available_until: Optional[datetime] = Field(default=None)
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=_now)


class ArenaCosmeticInventory(SQLModel, table=True):
    """Mirrors public.arena_cosmetic_inventory — ownership ledger.
    UNIQUE(user_id, cosmetic_id). `source` is a free-text audit trail
    (e.g. 'achievement:arena-win-100', 'season_reward:<season_id>')."""

    __tablename__ = "arena_cosmetic_inventory"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    cosmetic_id: UUID = Field(foreign_key="arena_cosmetics.id")
    source: Optional[str] = Field(default=None)
    granted_at: datetime = Field(default_factory=_now)


class ArenaPlayerShowcase(SQLModel, table=True):
    """Mirrors public.arena_player_showcase — ONE row per player holding
    every 'currently equipped/pinned' pointer (spec's 'Showcase Profile').
    pinned_badge_ids/achievement_showcase_ids reference badges.id (TEXT PK)
    — pinned_sticker_ids reference arena_cosmetics.id (UUID)."""

    __tablename__ = "arena_player_showcase"

    user_id: UUID = Field(primary_key=True, foreign_key="profiles.id")
    active_title_id: Optional[UUID] = Field(default=None, foreign_key="arena_cosmetics.id")
    active_frame_id: Optional[UUID] = Field(default=None, foreign_key="arena_cosmetics.id")
    active_banner_id: Optional[UUID] = Field(default=None, foreign_key="arena_cosmetics.id")
    active_effect_id: Optional[UUID] = Field(default=None, foreign_key="arena_cosmetics.id")
    pinned_badge_ids: List[str] = Field(
        default_factory=list,
        sa_column=sa.Column(ARRAY(sa.Text), nullable=False, server_default="{}"),
    )
    pinned_sticker_ids: List[UUID] = Field(
        default_factory=list,
        sa_column=sa.Column(ARRAY(sa.UUID(as_uuid=True)), nullable=False, server_default="{}"),
    )
    achievement_showcase_ids: List[str] = Field(
        default_factory=list,
        sa_column=sa.Column(ARRAY(sa.Text), nullable=False, server_default="{}"),
    )
    updated_at: datetime = Field(default_factory=_now)


class ArenaCollection(SQLModel, table=True):
    """Mirrors public.arena_collections — meta-achievements ("Complete all
    Math achievements"). badge_ids references badges.id (TEXT PK) — never
    duplicates achievement logic of its own, only checks completion of
    already-existing badges."""

    __tablename__ = "arena_collections"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    code: str = Field()
    name: str = Field()
    description: Optional[str] = Field(default=None)
    icon: Optional[str] = Field(default=None)
    badge_ids: List[str] = Field(
        default_factory=list,
        sa_column=sa.Column(ARRAY(sa.Text), nullable=False, server_default="{}"),
    )
    reward_config: Any = Field(
        default_factory=list,
        sa_column=sa.Column(JSONB, nullable=False, server_default="'[]'::jsonb"),
    )
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=_now)


class ArenaCollectionProgress(SQLModel, table=True):
    """Mirrors public.arena_collection_progress. UNIQUE(user_id,
    collection_id). completed_at is None until every badge_id in the
    parent collection is owned."""

    __tablename__ = "arena_collection_progress"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    collection_id: UUID = Field(foreign_key="arena_collections.id")
    completed_at: Optional[datetime] = Field(default=None)
