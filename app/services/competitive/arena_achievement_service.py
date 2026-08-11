from __future__ import annotations

"""
Arena Achievement Engine — Competitive Arena Phase 14.

Mirrors app/services/badge_engine.py's EXACT pattern (a stats dataclass +
a condition_type dispatcher + check_and_unlock) — reuses the SAME public.
badges/user_badges tables (never a parallel arena_achievements table, see
migration 094's header), just with Arena-specific condition_types this
module owns and badge_engine.py never touches. A badge row is either a
"legacy" one (sessions_completed/streak_days/... — badge_engine.py's own
condition_types) or an "Arena" one (arena_wins_total/arena_accuracy_pct/...
— this module's) — the two dispatchers never collide because each only
recognizes its own prefix-free set of condition_type strings; a badge with
an unrecognized condition_type is simply never matched by either (never
silently mis-evaluated).

Called from gameplay_service.finish_match (after a RANKED match — placement
and casual matches still update the underlying stats, so achievements
still progress from them too, per spec's "every completed activity should
contribute"), season_service.end_season, and club/club_service.create_club
(club_owner check) — see each call site's own comment for why.
"""

import logging
from dataclasses import dataclass
from typing import List, Optional, Set
from uuid import UUID

from sqlmodel import Session, func, select

from app.models.competitive import (
    CompetitiveMatchEvent,
    CompetitiveQuestionAttempt,
    CompetitiveSeasonStats,
    CompetitiveStatistics,
    RANK_TIERS,
)
from app.models.gamification import Badge, UserBadge

logger = logging.getLogger(__name__)


@dataclass
class ArenaStats:
    wins_total: int = 0
    matches_played: int = 0
    accuracy_pct: float = 0.0
    avg_response_time_ms: Optional[int] = None
    longest_streak: int = 0
    br_wins: int = 0
    br_top3_finishes: int = 0
    br_top10_finishes: int = 0
    tournament_wins: int = 0
    club_wins: int = 0
    club_owner: bool = False
    best_league_family_index: int = 0  # 1-based index into RANK_TIERS, 0 = unranked
    best_season_final_rank: Optional[int] = None  # lower is better; None = never finished a season
    questions_answered: int = 0
    replays_watched: int = 0
    comeback_wins: int = 0


#: Minimum sample size before an accuracy/speed achievement can trigger —
#: prevents a single lucky/short match from unlocking "95% Accuracy".
_MIN_MATCHES_FOR_ACCURACY_SPEED = 10


def compute_arena_stats(db: Session, user_id: UUID) -> ArenaStats:
    stats = ArenaStats()

    life = db.get(CompetitiveStatistics, user_id)
    if life is not None:
        stats.wins_total = life.wins
        stats.matches_played = life.matches_played
        stats.accuracy_pct = life.accuracy_pct
        stats.avg_response_time_ms = life.avg_response_time_ms
        stats.longest_streak = life.longest_streak
        stats.tournament_wins = life.tournament_wins
        stats.club_wins = life.club_wins
        if life.best_rank_tier in RANK_TIERS:
            stats.best_league_family_index = RANK_TIERS.index(life.best_rank_tier) + 1

    from app.models.competitive import CompetitiveBattleRoyaleStatistics
    br = db.get(CompetitiveBattleRoyaleStatistics, user_id)
    if br is not None:
        stats.br_wins = br.wins
        stats.br_top3_finishes = br.top3_finishes
        stats.br_top10_finishes = br.top10_finishes

    from app.models.club import ClubMember
    stats.club_owner = db.exec(
        select(ClubMember.id).where(ClubMember.user_id == user_id).where(ClubMember.role == "owner").where(ClubMember.status == "active")
    ).first() is not None

    best_rank = db.exec(
        select(func.min(CompetitiveSeasonStats.final_rank))
        .where(CompetitiveSeasonStats.user_id == user_id)
        .where(CompetitiveSeasonStats.final_rank.is_not(None))
    ).one()
    stats.best_season_final_rank = best_rank

    stats.questions_answered = int(db.exec(
        select(func.count()).select_from(CompetitiveQuestionAttempt).where(CompetitiveQuestionAttempt.user_id == user_id)
    ).one() or 0)

    from app.models.competitive import CompetitiveReplayView
    stats.replays_watched = int(db.exec(
        select(func.count(func.distinct(CompetitiveReplayView.match_id))).where(CompetitiveReplayView.user_id == user_id)
    ).one() or 0)

    stats.comeback_wins = int(db.exec(
        select(func.count()).select_from(CompetitiveMatchEvent)
        .where(CompetitiveMatchEvent.actor_id == user_id)
        .where(CompetitiveMatchEvent.event_type == "achievement_comeback")
    ).one() or 0)

    return stats


def _meets(ct: str, threshold: int, stats: ArenaStats) -> bool:
    if ct == "arena_wins_total":
        return stats.wins_total >= threshold
    if ct == "arena_accuracy_pct":
        return stats.matches_played >= _MIN_MATCHES_FOR_ACCURACY_SPEED and stats.accuracy_pct >= threshold
    if ct == "arena_avg_response_under_ms":
        return (
            stats.matches_played >= _MIN_MATCHES_FOR_ACCURACY_SPEED
            and stats.avg_response_time_ms is not None
            and stats.avg_response_time_ms <= threshold
        )
    if ct == "arena_win_streak":
        return stats.longest_streak >= threshold
    if ct == "arena_br_wins":
        return stats.br_wins >= threshold
    if ct == "arena_br_top3_finishes":
        return stats.br_top3_finishes >= threshold
    if ct == "arena_br_top10_finishes":
        return stats.br_top10_finishes >= threshold
    if ct == "arena_tournament_wins":
        return stats.tournament_wins >= threshold
    if ct == "arena_club_wins":
        return stats.club_wins >= threshold
    if ct == "arena_club_owner":
        return stats.club_owner
    if ct == "arena_league_reached":
        return stats.best_league_family_index >= threshold
    if ct == "arena_season_final_rank":
        return stats.best_season_final_rank is not None and stats.best_season_final_rank <= threshold
    if ct == "arena_questions_answered":
        return stats.questions_answered >= threshold
    if ct == "arena_replays_watched":
        return stats.replays_watched >= threshold
    if ct == "arena_comeback_wins":
        return stats.comeback_wins >= threshold
    return False


#: Every condition_type this module recognizes — badge_engine.py's own
#: check_and_unlock_badges() query already filters to `condition_type IS
#: NOT NULL`, so this module additionally filters to ONLY these, leaving
#: every legacy condition_type (sessions_completed, streak_days, ...)
#: strictly badge_engine.py's own concern.
ARENA_CONDITION_TYPES = {
    "arena_wins_total", "arena_accuracy_pct", "arena_avg_response_under_ms", "arena_win_streak",
    "arena_br_wins", "arena_br_top3_finishes", "arena_br_top10_finishes", "arena_tournament_wins",
    "arena_club_wins", "arena_club_owner", "arena_league_reached", "arena_season_final_rank",
    "arena_questions_answered", "arena_replays_watched", "arena_comeback_wins",
}


def achievement_progress(badge: Badge, stats: ArenaStats) -> tuple[int, int]:
    """(current, total) for a locked Arena achievement's progress bar —
    mirrors badge_engine.badge_progress's exact shape."""
    threshold = badge.condition_threshold or 1
    ct = badge.condition_type
    if ct == "arena_season_final_rank":
        # Inverted (lower is better) — "progress" toward a rank ceiling
        # doesn't map to a simple min()/threshold ratio the same way every
        # other (higher-is-better) condition does, so this one is binary.
        cur = threshold if _meets(ct, threshold, stats) else 0
        return cur, threshold
    if ct == "arena_avg_response_under_ms":
        cur = threshold if _meets(ct, threshold, stats) else 0
        return cur, threshold

    value_map = {
        "arena_wins_total": stats.wins_total, "arena_accuracy_pct": stats.accuracy_pct,
        "arena_win_streak": stats.longest_streak, "arena_br_wins": stats.br_wins,
        "arena_br_top3_finishes": stats.br_top3_finishes, "arena_br_top10_finishes": stats.br_top10_finishes,
        "arena_tournament_wins": stats.tournament_wins, "arena_club_wins": stats.club_wins,
        "arena_club_owner": 1 if stats.club_owner else 0, "arena_league_reached": stats.best_league_family_index,
        "arena_questions_answered": stats.questions_answered, "arena_replays_watched": stats.replays_watched,
        "arena_comeback_wins": stats.comeback_wins,
    }
    cur = min(int(value_map.get(ct, 0)), threshold)
    return cur, threshold


def check_and_unlock_arena_achievements(db: Session, user_id: UUID, *, stats: Optional[ArenaStats] = None) -> List[str]:
    """Returns newly-unlocked badge ids. Best-effort — an achievement
    failure must never break the gameplay/season flow that triggered it."""
    try:
        if stats is None:
            stats = compute_arena_stats(db, user_id)

        already_unlocked: Set[str] = set(db.exec(select(UserBadge.badge_id).where(UserBadge.user_id == user_id)).all())

        badges = list(db.exec(
            select(Badge).where(Badge.active == True).where(Badge.condition_type.in_(ARENA_CONDITION_TYPES))  # noqa: E712
        ).all())

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        newly_unlocked: List[Badge] = []
        for badge in badges:
            if badge.id in already_unlocked:
                continue
            if badge.available_from and now < badge.available_from:
                continue
            if badge.available_until and now > badge.available_until:
                continue
            threshold = badge.condition_threshold or 0
            if _meets(badge.condition_type, threshold, stats):
                db.add(UserBadge(
                    user_id=user_id, badge_id=badge.id, unlocked_at=now,
                    progress_current=threshold, progress_total=threshold, viewed_at=None,
                ))
                newly_unlocked.append(badge)

        if not newly_unlocked:
            return []
        db.commit()

        from app.services.competitive import reward_service as _reward_service
        from app.services.notification_engine import emit
        for badge in newly_unlocked:
            _apply_badge_rewards(db, user_id=user_id, badge=badge)

            event_type = "badge_unlocked"
            if badge.is_hidden:
                event_type = "arena_secret_achievement_unlocked"
            elif badge.category == "Milestone":
                event_type = "arena_milestone_reached"
            elif badge.rarity in ("epic", "legendary", "mythic"):
                event_type = "arena_rare_reward_unlocked"

            emit(
                db, event_type=event_type, user_id=user_id, context={"badge_name": badge.name},
                data={"badge_id": badge.id, "rarity": badge.rarity},
                dedup_key=f"{event_type}:{user_id}:{badge.id}",
            )

            from app.core import domain_events
            domain_events.publish(domain_events.ACHIEVEMENT_UNLOCKED, user_id=user_id, badge_id=badge.id, rarity=badge.rarity)

        from app.services.competitive import collection_service
        collection_service.check_and_complete_collections(db, user_id)

        return [b.id for b in newly_unlocked]
    except Exception:
        logger.exception("arena_achievement_service.check_and_unlock_arena_achievements failed for user=%s", user_id)
        return []


def _apply_badge_rewards(db: Session, *, user_id: UUID, badge: Badge) -> None:
    """EP (existing ep_reward field — never actually applied by the legacy
    badge_engine.py path, see this module's own migration header; Arena
    achievements DO apply it) + every reward_config entry (title/frame/
    banner/effect/sticker → real cosmetic inventory grant; anything else →
    reward_service's existing recorded_only fallback)."""
    from app.models.enums import KpSource
    from app.services import kp as kp_service
    from app.services.competitive import reward_service

    if badge.ep_reward and badge.ep_reward > 0:
        try:
            kp_service.award_kp(user_id, badge.ep_reward, KpSource.competitive, f"Succès : {badge.name}", db)
        except Exception:
            logger.exception("arena achievement EP grant failed user=%s badge=%s", user_id, badge.id)

    for entry in (badge.reward_config or []):
        reward_type = entry.get("reward_type")
        reward_ref = entry.get("reward_ref")
        reward_amount = entry.get("reward_amount")
        if not reward_type:
            continue
        try:
            reward_service.grant_reward(
                db, user_id=user_id, season_id=None, source=f"achievement:{badge.id}",
                reward_type=reward_type, reward_ref=reward_ref, reward_amount=reward_amount,
            )
        except Exception:
            logger.exception("arena achievement reward_config grant failed user=%s badge=%s entry=%s", user_id, badge.id, entry)
