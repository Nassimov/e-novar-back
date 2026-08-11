from __future__ import annotations

"""
Reward Distributor — Competitive Arena Phase 7.

Applies rewards for real via existing infrastructure wherever that
infrastructure exists (EP via app/services/kp.py, badges via UserBadge),
and records everything else (sticker/frame/title/avatar_decoration) as an
audit-trail grant only — status='recorded_only' — since no per-item
ownership/equip system exists yet for those cosmetic types (see migration
082's header comment for why this is a deliberate scope decision, not an
oversight: the existing store has no "which specific sticker does this
user own" ledger to safely hook an auto-grant into without risking
duplicating that system's own invariants). Every grant, real or
recorded-only, is written to competitive_reward_grants so nothing is ever
silently lost — an admin/future phase can always reconcile from this trail.
"""

import logging
from typing import Optional
from uuid import UUID

from sqlmodel import Session, select

from app.models.competitive import CompetitiveRewardGrant
from app.models.enums import KpSource
from app.services import kp as kp_service
from app.services.notification_engine import emit

logger = logging.getLogger(__name__)

#: reward_type values with real, already-existing infrastructure to apply
#: them immediately. Everything else is recorded-only (see module docstring).
#: Phase 14 — title/frame/banner/effect/sticker now have real infrastructure
#: too (app/models/arena_cosmetics.py's cosmetic inventory) — see
#: _apply_cosmetic_reward below. 'avatar_decoration' stays recorded_only
#: (no catalogue row type maps to it — it predates the Phase 14 cosmetic
#: system and was never actually seeded as a real reward_ref anywhere).
#: Phase 15 — 'arena_xp' is now live too (competitive_statistics.arena_xp,
#: migration 095) — missions/events/login rewards are its first real
#: consumers; previously it only existed as a stub on the unrelated,
#: still-stub CompetitiveSpectatorStats.arena_xp (Phase 8).
_LIVE_REWARD_TYPES = {"ep", "arena_xp", "badge", "title", "frame", "banner", "effect", "sticker"}


def _apply_cosmetic_reward(db: Session, *, user_id: UUID, cosmetic_type: str, reward_ref: Optional[str], source: str) -> bool:
    """Returns True if a matching catalogue row existed and was granted —
    False (caller then records status='recorded_only' instead, exactly
    like the pre-Phase-14 behavior) if reward_ref doesn't match any
    arena_cosmetics row, e.g. a stale/typo'd admin-configured reward_ref."""
    if not reward_ref:
        return False
    from app.services.competitive import cosmetic_service
    entry = cosmetic_service.grant_cosmetic_by_code(db, user_id, cosmetic_type=cosmetic_type, code=reward_ref, source=source)
    return entry is not None


def _apply_arena_xp_reward(db: Session, *, user_id: UUID, amount: int) -> None:
    from app.models.competitive import CompetitiveStatistics
    stats = db.get(CompetitiveStatistics, user_id)
    if stats is None:
        stats = CompetitiveStatistics(user_id=user_id)
    stats.arena_xp += amount
    db.add(stats)
    db.commit()

    try:
        from app.services.competitive import mission_service
        mission_service.record_event(db, user_id=user_id, metric_key="arena_xp_earned", amount=amount)
    except Exception:
        logger.debug("reward_service: arena_xp mission feed failed for user=%s", user_id, exc_info=True)


def grant_reward(
    db: Session, *, user_id: UUID, season_id: Optional[UUID], source: str,
    reward_type: str, reward_ref: Optional[str] = None, reward_amount: Optional[int] = None,
    notify: bool = True, tournament_id: Optional[UUID] = None, battle_royale_match_id: Optional[UUID] = None,
) -> CompetitiveRewardGrant:
    status = "granted" if reward_type in _LIVE_REWARD_TYPES else "recorded_only"

    # Phase 15 — Happy Hour / Double Rewards: multiplies EP/Arena XP grants
    # at this single existing choke-point, same trick Phase 14 used for
    # cosmetics — every past and future call site benefits with zero changes.
    if reward_type in ("ep", "arena_xp") and reward_amount:
        try:
            from app.services.competitive import event_service
            multiplier = event_service.get_active_multiplier(db, reward_type)
            if multiplier != 1.0:
                reward_amount = round(reward_amount * multiplier)
        except Exception:
            logger.debug("reward_service: happy-hour multiplier lookup failed", exc_info=True)

    try:
        if reward_type == "ep" and reward_amount:
            kp_service.award_kp(user_id, reward_amount, KpSource.competitive, f"Récompense Arène ({source})", db)
        elif reward_type == "arena_xp" and reward_amount:
            _apply_arena_xp_reward(db, user_id=user_id, amount=reward_amount)
        elif reward_type == "badge" and reward_ref:
            _grant_badge(db, user_id=user_id, badge_id=reward_ref)
        elif reward_type in ("title", "frame", "banner", "effect", "sticker"):
            applied = _apply_cosmetic_reward(db, user_id=user_id, cosmetic_type=reward_type, reward_ref=reward_ref, source=source)
            if not applied:
                status = "recorded_only"
    except Exception:
        logger.exception("reward_service.grant_reward: applying reward_type=%s failed for user=%s — grant still recorded", reward_type, user_id)
        status = "recorded_only"

    grant = CompetitiveRewardGrant(
        user_id=user_id, season_id=season_id, tournament_id=tournament_id, source=source, reward_type=reward_type,
        reward_ref=reward_ref, reward_amount=reward_amount, status=status,
        battle_royale_match_id=battle_royale_match_id,
    )
    db.add(grant)
    db.commit()
    db.refresh(grant)

    if notify:
        emit(
            db, event_type="competitive_reward_received", user_id=user_id,
            data={
                "reward_type": reward_type, "reward_ref": reward_ref,
                "reward_amount": reward_amount, "source": source,
            },
            dedup_key=f"competitive_reward_received:{grant.id}",
        )

    from app.core import domain_events
    domain_events.publish(
        domain_events.REWARD_GRANTED, user_id=user_id, reward_type=reward_type,
        reward_amount=reward_amount, source=source, status=status,
    )
    return grant


def grant_promotion_bonus(db: Session, *, user_id: UUID, season_id: Optional[UUID], amount: int) -> Optional[CompetitiveRewardGrant]:
    """EP bonus configured per-league (competitive_leagues.promotion_bonus_ep)
    — the promotion itself is already notified via competitive_rank_promoted,
    so this grant is silent (notify=False) to avoid a redundant second
    notification for the same event."""
    if amount <= 0:
        return None
    return grant_reward(
        db, user_id=user_id, season_id=season_id, source="promotion",
        reward_type="ep", reward_amount=amount, notify=False,
    )


def distribute_season_rewards(db: Session, season_id: UUID) -> int:
    """Season End Workflow step — "Generate rewards" (automatic). Every
    competitive_season_rewards rule for this season is matched against every
    competitive_season_stats row (final_rank must already be frozen by the
    caller before this runs — see season_service.end_season)."""
    from app.models.competitive import CompetitiveSeasonReward, CompetitiveSeasonStats

    rules = list(db.exec(select(CompetitiveSeasonReward).where(CompetitiveSeasonReward.season_id == season_id)).all())
    if not rules:
        return 0
    stats_rows = list(db.exec(select(CompetitiveSeasonStats).where(CompetitiveSeasonStats.season_id == season_id)).all())

    granted = 0
    for rule in rules:
        for row in stats_rows:
            matches_league = rule.league_id is not None and row.league_id == rule.league_id
            matches_rank = (
                rule.rank_min is not None and row.final_rank is not None
                and rule.rank_min <= row.final_rank <= (rule.rank_max or rule.rank_min)
            )
            if not (matches_league or matches_rank):
                continue
            grant_reward(
                db, user_id=row.user_id, season_id=season_id, source="season_end",
                reward_type=rule.reward_type, reward_ref=rule.reward_ref, reward_amount=rule.reward_amount,
            )
            granted += 1
    return granted


def _grant_badge(db: Session, *, user_id: UUID, badge_id: str) -> None:
    from app.models.gamification import Badge, UserBadge

    badge = db.get(Badge, badge_id)
    if badge is None:
        logger.warning("reward_service: badge_id=%s not found in catalogue — grant recorded but not applied", badge_id)
        raise ValueError(f"unknown badge_id={badge_id}")
    existing = db.exec(
        select(UserBadge).where(UserBadge.user_id == user_id).where(UserBadge.badge_id == badge_id)
    ).first()
    if existing is not None:
        return  # already owned — the audit-trail grant row is still written by the caller
    db.add(UserBadge(user_id=user_id, badge_id=badge_id, progress_current=1, progress_total=1))
    db.commit()
