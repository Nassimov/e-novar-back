from __future__ import annotations

"""
Ranking Service — Competitive Arena Phase 7: League Engine + MMR
Calculator + Promotion/Demotion Engine.

Replaces the Phase 1 hardcoded _TIER_THRESHOLDS with the real,
admin-editable competitive_leagues table (spec: "No hardcoded values" for
league thresholds/icons/colors/promotion/demotion rules — "future
modifications must require zero code changes"). This is also the first
phase to actually WRITE CompetitiveStatistics.rating/rank_tier after a
match — every phase since Phase 1 only ever read it (see
gameplay_service.finish_match's Phase 3 comment: "Do not calculate ranking
yet, Ranking Engine will consume during Phase 7").

apply_match_result() is the single entry point gameplay_service.finish_match
calls once per participant. It:
  1. computes the new rating via a real Elo-style formula (opponent rating,
     expected win probability, current streak, inflation dampening, floor
     clamp — never a fixed delta, per spec "never use a fixed value");
  2. writes one immutable CompetitiveRatingHistory row (the "Match History"
     MMR fields — everything else about the match is read live from
     competitive_matches/competitive_match_player_stats, never duplicated);
  3. resolves the league before/after from competitive_leagues;
  4. applies demotion protection (a player promoted into their current
     league fewer than league.demotion_protection_matches matches ago is
     floored at that league's min_mmr instead of dropping back out of it —
     "protection after recent promotion");
  5. on promotion: awards the league's configured EP bonus and emits the
     already-seeded competitive_rank_promoted/demoted notifications
     (migration 074) plus the Phase 7 milestone ones (personal best,
     legend — migration 082; top 100/top 10 are rank-position-based and
     live in leaderboard_service instead, since they need a leaderboard
     query this module has no reason to depend on).

Season/leaderboard bookkeeping for the SAME match (season_stats,
rating-history-based anti-abuse signals) are deliberately handled by
separate calls from gameplay_service, not from inside this module — keeps
the Ranking/Season/Leaderboard/Anti-Abuse engines independent services, per
spec's "Backend Services" list, each with a single reason to change.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from sqlmodel import Session, select

from app.models.admin import PlatformSettings
from app.models.competitive import CompetitiveLeague, CompetitiveRatingHistory, CompetitiveStatistics
from app.services.competitive import reward_service
from app.services.notification_engine import emit

logger = logging.getLogger(__name__)


def _settings(db: Session) -> PlatformSettings:
    return db.get(PlatformSettings, True) or PlatformSettings()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─── Phase 13: Ranked vs Casual ──────────────────────────────────────────────

def check_ranked_eligibility(db: Session, user_id: UUID) -> None:
    """Raises HTTPException(403) on failure. Spec's 'Ranked Match
    Restrictions' — every gate defaults to 'no requirement' (0/false) so
    this is a pure no-op until an admin actually configures one. Phone/
    email verification gates are real and enforceable (see migration 093's
    header) but every profile defaults to unverified — an admin who enables
    those two specific gates before a verification flow exists will simply
    lock ranked play for everyone, which is the admin's own configuration
    choice to make, not something this function should silently work around."""
    from fastapi import HTTPException, status as _status

    from app.models.competitive import CompetitiveStatistics
    from app.models.kp import KpAccount
    from app.models.profile import Profile

    settings_row = _settings(db)
    profile = db.get(Profile, user_id)
    if profile is None:
        return

    if settings_row.competitive_ranked_min_account_age_days > 0:
        created_at = profile.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        age_days = (_now() - created_at).days
        if age_days < settings_row.competitive_ranked_min_account_age_days:
            raise HTTPException(status_code=_status.HTTP_403_FORBIDDEN, detail="Ton compte est trop récent pour le mode classé.")

    if settings_row.competitive_ranked_require_onboarding and not profile.onboarding_completed:
        raise HTTPException(status_code=_status.HTTP_403_FORBIDDEN, detail="Termine ton inscription pour accéder au mode classé.")

    if settings_row.competitive_ranked_require_phone_verified and not profile.phone_verified:
        raise HTTPException(status_code=_status.HTTP_403_FORBIDDEN, detail="Vérifie ton numéro de téléphone pour accéder au mode classé.")

    if settings_row.competitive_ranked_require_email_verified and not profile.email_verified:
        raise HTTPException(status_code=_status.HTTP_403_FORBIDDEN, detail="Vérifie ton adresse email pour accéder au mode classé.")

    if settings_row.competitive_ranked_min_ep_balance > 0:
        kp_account = db.exec(select(KpAccount).where(KpAccount.user_id == user_id)).first()
        balance = kp_account.balance if kp_account else 0
        if balance < settings_row.competitive_ranked_min_ep_balance:
            raise HTTPException(status_code=_status.HTTP_403_FORBIDDEN, detail="EP insuffisant pour accéder au mode classé.")

    stats = db.get(CompetitiveStatistics, user_id)
    if stats is not None and stats.fair_play_score < settings_row.competitive_fair_play_min_for_ranked:
        raise HTTPException(status_code=_status.HTTP_403_FORBIDDEN, detail="Ton score de fair-play est trop bas pour accéder au mode classé.")


def is_match_type_ranked_eligible(db: Session, match_type: str) -> bool:
    """Spec: 'Administrator can configure which game modes are ranked' —
    PlatformSettings.competitive_ranked_match_types is a plain comma-
    separated list (e.g. 'duel,battle_royale,tournament') rather than a new
    table, mirroring this codebase's existing convention for small
    admin-editable enums (see e.g. club_service's fixed reserved-word set)."""
    settings_row = _settings(db)
    allowed = {t.strip() for t in settings_row.competitive_ranked_match_types.split(",") if t.strip()}
    return match_type in allowed


# ─── League Engine ───────────────────────────────────────────────────────────

def list_leagues(db: Session) -> List[CompetitiveLeague]:
    return list(db.exec(select(CompetitiveLeague).order_by(CompetitiveLeague.sort_order.asc())).all())


def get_league_for_rating(db: Session, rating: int, *, leagues: Optional[List[CompetitiveLeague]] = None) -> Optional[CompetitiveLeague]:
    """The ladder is ordered/contiguous by construction (admin-managed), so
    the highest league whose min_mmr the rating clears is the answer — no
    max_mmr check needed except to confirm we picked the right one when the
    ladder has gaps (defensive only; a well-configured ladder never gaps).

    `leagues` lets a bulk caller (e.g. the admin ranking-recalculation
    endpoint) pass in a single pre-fetched ladder instead of re-querying it
    once per row — everything else is unaffected (per-match callers keep
    passing nothing and get the same live-fetch behavior as before)."""
    leagues = list_leagues(db) if leagues is None else leagues
    if not leagues:
        return None
    candidate = leagues[0]
    for league in leagues:
        if rating >= league.min_mmr:
            candidate = league
        else:
            break
    return candidate


def league_slug(league: Optional[CompetitiveLeague]) -> str:
    if league is None:
        return "bronze"
    return league.league_name.lower().replace(" ", "_")


def get_rank_tier(db: Session, rating: int) -> str:
    """Backward-compatible: the coarse family slug for a given rating (used
    by app/routers/competitive/stats.py and opponent_service.py display)."""
    return league_slug(get_league_for_rating(db, rating))


# ─── MMR Calculator ────────────────────────────────────────────────────────

_RESULT_SCORE = {"win": 1.0, "draw": 0.5, "loss": 0.0}


def _compute_delta(
    *, rating_a: int, rating_b: int, result: str, streak_after: int, settings_row: PlatformSettings,
    k_factor_multiplier: float = 1.0,
) -> int:
    """Classic Elo expected-score formula, K-factor from platform_settings
    (never hardcoded). A win-only streak bonus rewards momentum (spec:
    "Rating Engine must consider ... Current Streak"). Inflation dampening
    shrinks *gains* once a player is already above the configured
    threshold — losses are never dampened, only rewards ("rating inflation
    protection"). k_factor_multiplier (Phase 13) widens rating swings during
    placement matches (spec: converge to a real rank faster) — 1.0 outside
    placement, PlatformSettings.competitive_placement_k_factor_multiplier
    (default 2.0) during it — the SAME formula, never a parallel algorithm,
    per spec's "architecture must allow replacing the algorithm later
    without modifying business logic" (the multiplier IS that seam)."""
    expected_a = 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400.0))
    actual_a = _RESULT_SCORE[result]
    delta = settings_row.competitive_mmr_k_factor * k_factor_multiplier * (actual_a - expected_a)

    if result == "win":
        delta += min(
            settings_row.competitive_mmr_streak_bonus_max,
            max(0, streak_after) * settings_row.competitive_mmr_streak_bonus_per_win,
        )

    if delta > 0 and rating_a >= settings_row.competitive_mmr_inflation_dampening_threshold:
        delta *= settings_row.competitive_mmr_inflation_dampening_factor

    return round(delta)


@dataclass
class RatingChangeResult:
    rating_before: int
    rating_after: int
    delta: int
    league_before: Optional[CompetitiveLeague]
    league_after: Optional[CompetitiveLeague]
    promoted: bool
    demoted: bool


def _matches_since_promotion(db: Session, *, user_id: UUID, league_id: UUID) -> Optional[int]:
    """None if there's no recorded promotion INTO this league yet (e.g. a
    brand-new player whose very first league isn't a "promotion" from
    anywhere — nothing to protect against dropping out of)."""
    last_promotion = db.exec(
        select(CompetitiveRatingHistory)
        .where(CompetitiveRatingHistory.user_id == user_id)
        .where(CompetitiveRatingHistory.league_after_id == league_id)
        .where(CompetitiveRatingHistory.league_before_id != league_id)
        .order_by(CompetitiveRatingHistory.created_at.desc())
        .limit(1)
    ).first()
    if last_promotion is None:
        return None
    since_count = len(list(db.exec(
        select(CompetitiveRatingHistory.id)
        .where(CompetitiveRatingHistory.user_id == user_id)
        .where(CompetitiveRatingHistory.created_at > last_promotion.created_at)
    ).all()))
    return since_count


def _finalize_rating_change(
    db: Session, *, match_id: UUID, user_id: UUID, opponent_id: Optional[UUID], result: str,
    season_id: Optional[UUID], delta: int, settings_row: PlatformSettings,
) -> RatingChangeResult:
    """The shared back half of a rating update, factored out of
    apply_match_result (Phase 7) so Phase 10 Part C's Battle Royale path can
    feed in a DIFFERENT delta computation (an N-player pairwise average, see
    apply_battle_royale_match_result below) while still going through the
    EXACT SAME floor clamp / league resolution / demotion-protection /
    rating-history write / promotion-demotion notification pipeline every
    other match type already relies on. apply_match_result's own behavior
    for duels/tournament matches/club battles is UNCHANGED by this
    refactor — it still computes its delta the same way it always did
    (single-opponent Elo) and simply hands it to this helper instead of
    inlining the rest of the function itself."""
    stats = db.get(CompetitiveStatistics, user_id)
    if stats is None:
        from app.services.competitive import statistics_service
        stats = statistics_service.get_or_create_statistics(db, user_id)

    rating_before = stats.rating
    rating_after = max(settings_row.competitive_mmr_floor, rating_before + delta)

    league_before = get_league_for_rating(db, rating_before)
    league_after = get_league_for_rating(db, rating_after)

    promoted = bool(league_after and league_before and league_after.sort_order > league_before.sort_order)
    demoted = bool(league_after and league_before and league_after.sort_order < league_before.sort_order)

    if demoted and league_before is not None:
        matches_since = _matches_since_promotion(db, user_id=user_id, league_id=league_before.id)
        if matches_since is not None and matches_since < league_before.demotion_protection_matches:
            rating_after = max(rating_after, league_before.min_mmr)
            league_after = league_before
            demoted = False

    stats.rating = rating_after
    stats.rank_tier = league_slug(league_after)
    stats.league_id = league_after.id if league_after else None
    if rating_after > stats.highest_rating:
        stats.highest_rating = rating_after
        if league_after is not None:
            stats.best_rank_tier = league_slug(league_after)
    stats.last_ranked_match_at = _now()

    # Phase 13 — Placement Service, folded into this single shared choke
    # point (both apply_match_result and apply_battle_royale_match_result
    # funnel through here) so placement counts a match regardless of
    # match_type, exactly like the rest of this pipeline already does.
    just_completed_placement = False
    if not stats.placement_complete:
        stats.placement_matches_played += 1
        if stats.placement_matches_played >= settings_row.competitive_placement_matches_required:
            stats.placement_complete = True
            just_completed_placement = True

    # Phase 13 — "Season Win Streak": a running high-water mark of
    # current_streak, reset to 0 by season_service.end_season alongside its
    # own rating reset pass (see that function) — between resets this is
    # exactly "best streak achieved so far this season".
    stats.season_best_streak = max(stats.season_best_streak, stats.current_streak)
    db.add(stats)

    db.add(CompetitiveRatingHistory(
        match_id=match_id, user_id=user_id, opponent_id=opponent_id, season_id=season_id,
        result=result, rating_before=rating_before, rating_after=rating_after, delta=rating_after - rating_before,
        league_before_id=league_before.id if league_before else None,
        league_after_id=league_after.id if league_after else None,
    ))
    db.commit()
    db.refresh(stats)

    change = RatingChangeResult(
        rating_before=rating_before, rating_after=rating_after, delta=rating_after - rating_before,
        league_before=league_before, league_after=league_after, promoted=promoted, demoted=demoted,
    )
    _handle_promotion_demotion(db, user_id=user_id, season_id=season_id, change=change)

    if just_completed_placement:
        emit(
            db, event_type="competitive_placement_complete", user_id=user_id,
            context={"rank_tier": league_label(league_after) if league_after else ""},
            data={"league_id": str(league_after.id) if league_after else None, "rating": rating_after},
            dedup_key=f"competitive_placement_complete:{user_id}",
        )
    return change


def apply_match_result(
    db: Session, *, match_id: UUID, user_id: UUID, opponent_id: Optional[UUID], result: str, season_id: Optional[UUID],
) -> RatingChangeResult:
    settings_row = _settings(db)
    stats = db.get(CompetitiveStatistics, user_id)
    if stats is None:
        from app.services.competitive import statistics_service
        stats = statistics_service.get_or_create_statistics(db, user_id)

    opponent_stats = db.get(CompetitiveStatistics, opponent_id) if opponent_id else None
    rating_before = stats.rating
    opponent_rating = opponent_stats.rating if opponent_stats else rating_before

    delta = _compute_delta(
        rating_a=rating_before, rating_b=opponent_rating, result=result,
        streak_after=stats.current_streak, settings_row=settings_row,
        k_factor_multiplier=settings_row.competitive_placement_k_factor_multiplier if not stats.placement_complete else 1.0,
    )
    return _finalize_rating_change(
        db, match_id=match_id, user_id=user_id, opponent_id=opponent_id, result=result,
        season_id=season_id, delta=delta, settings_row=settings_row,
    )


def apply_battle_royale_match_result(
    db: Session, *, match_id: UUID, user_id: UUID, result: str, season_id: Optional[UUID], delta: int,
) -> RatingChangeResult:
    """Phase 10 Part C — the N-player counterpart of apply_match_result.

    A Battle Royale has no single "opponent" — `delta` here is precomputed
    by the caller (gameplay_service.finish_match) as the AVERAGE of pairwise
    Elo deltas (via _compute_delta) against every OTHER participant, each
    pairwise comparison's win/loss/draw derived from RELATIVE final_rank
    (lower number = better placement), never from apply_match_result's
    single "other = next(...)" pairing, which is meaningless once more than
    two participants exist (see this function's caller for the full
    derivation). Everything past delta computation — floor clamp, league
    resolution, demotion protection, rating-history write, promotion/
    demotion notifications — reuses _finalize_rating_change VERBATIM, the
    exact same pipeline apply_match_result itself uses.

    opponent_id is always None on the resulting CompetitiveRatingHistory row
    (no single opponent exists in a Battle Royale); `result` is still
    recorded for audit purposes (the coarse forced_results label — 'win'
    only for the sole rank=1 winner, 'loss' for everyone else — NOT the
    pairwise-derived per-comparison label, which only ever exists internally
    and isn't a single result for the whole match)."""
    settings_row = _settings(db)
    return _finalize_rating_change(
        db, match_id=match_id, user_id=user_id, opponent_id=None, result=result,
        season_id=season_id, delta=delta, settings_row=settings_row,
    )


# ─── Phase 13: Inactivity Decay ──────────────────────────────────────────────

def apply_inactivity_decay(db: Session) -> int:
    """Celery beat entry point (app/workers/competitive_tasks.py). Optional
    — no-ops entirely unless an admin enables it. Decays at most once per
    week per player while they stay inactive (never a runaway daily drain),
    floored at PlatformSettings.competitive_inactivity_decay_floor — a
    lifetime placement/legend achievement (highest_rating) is NEVER reduced
    by decay, only the live, currently-displayed rating."""
    from datetime import timedelta

    settings_row = _settings(db)
    if not settings_row.competitive_inactivity_decay_enabled:
        return 0

    cutoff = _now() - timedelta(days=settings_row.competitive_inactivity_decay_after_days)
    reapply_cutoff = _now() - timedelta(days=7)

    candidates = list(db.exec(
        select(CompetitiveStatistics)
        .where(CompetitiveStatistics.last_ranked_match_at.is_not(None))
        .where(CompetitiveStatistics.last_ranked_match_at < cutoff)
        .where(CompetitiveStatistics.rating > settings_row.competitive_inactivity_decay_floor)
    ).all())

    decayed = 0
    for stats in candidates:
        if stats.inactivity_decay_applied_at is not None and stats.inactivity_decay_applied_at >= reapply_cutoff:
            continue
        new_rating = max(settings_row.competitive_inactivity_decay_floor, stats.rating - settings_row.competitive_inactivity_decay_amount)
        if new_rating == stats.rating:
            continue
        old_rating = stats.rating
        stats.rating = new_rating
        stats.inactivity_decay_applied_at = _now()
        new_league = get_league_for_rating(db, new_rating)
        stats.league_id = new_league.id if new_league else None
        stats.rank_tier = league_slug(new_league)
        db.add(stats)
        db.add(CompetitiveRatingHistory(
            match_id=None, user_id=stats.user_id, opponent_id=None, season_id=None, result="decay",
            rating_before=old_rating, rating_after=new_rating, delta=new_rating - old_rating,
        ))
        decayed += 1
    db.commit()
    logger.info("competitive inactivity decay applied: players=%s", decayed)
    return decayed


def league_label(league: CompetitiveLeague) -> str:
    return league.league_name + (f" {league.division_label}" if league.division_label else "")


def _handle_promotion_demotion(db: Session, *, user_id: UUID, season_id: Optional[UUID], change: RatingChangeResult) -> None:
    if change.promoted and change.league_after is not None:
        emit(
            db, event_type="competitive_rank_promoted", user_id=user_id,
            context={"rank_tier": league_label(change.league_after)},
            data={"league_id": str(change.league_after.id), "rating": change.rating_after},
            dedup_key=f"competitive_rank_promoted:{user_id}:{change.league_after.id}:{_now().date().isoformat()}",
        )
        if change.league_after.promotion_bonus_ep > 0:
            reward_service.grant_promotion_bonus(
                db, user_id=user_id, season_id=season_id, amount=change.league_after.promotion_bonus_ep,
            )
        if league_slug(change.league_after) == "legend":
            emit(db, event_type="competitive_legend_promotion", user_id=user_id,
                 dedup_key=f"competitive_legend_promotion:{user_id}")
    elif change.demoted and change.league_after is not None:
        emit(
            db, event_type="competitive_rank_demoted", user_id=user_id,
            context={"rank_tier": league_label(change.league_after)},
            data={"league_id": str(change.league_after.id), "rating": change.rating_after},
            dedup_key=f"competitive_rank_demoted:{user_id}:{change.league_after.id}:{_now().date().isoformat()}",
        )

    stats = db.get(CompetitiveStatistics, user_id)
    if stats is not None and change.delta > 0 and change.rating_after >= stats.highest_rating:
        emit(
            db, event_type="competitive_new_personal_best", user_id=user_id,
            context={"rating": stats.rating},
            dedup_key=f"competitive_new_personal_best:{user_id}:{stats.rating}",
        )
