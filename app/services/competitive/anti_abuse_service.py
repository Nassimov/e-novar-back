from __future__ import annotations

"""
Anti-Abuse Analyzer — Competitive Arena Phase 7.

Detection only — "No automatic punishment yet. Only detection." per spec.
Every signal is logged as a CompetitiveMatchEvent (mirrors Phase 6's smurf
signal pattern in matchmaking_service.log_smurf_signal_if_any — same
convention, never duplicated into a separate flags table) so a future
trust-and-safety phase has a real, already-collected trail to act on. The
admin dashboard (app/routers/admin/competitive.py's suspicious-accounts
endpoint) reads these events live.

Deliberately NOT attempted here: multiple-account detection and
"artificial boosting" beyond what's below — both would need device
fingerprinting / IP / session-correlation infrastructure that does not
exist anywhere in this codebase, and fabricating a fake signal from data
that isn't actually collected would be worse than no signal at all.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional
from uuid import UUID

from sqlmodel import Session, select

from app.models.competitive import CompetitiveRatingHistory, CompetitiveStatistics
from app.services.competitive import event_log_service

logger = logging.getLogger(__name__)

_REPEATED_OPPONENT_WINDOW_MATCHES = 10
_REPEATED_OPPONENT_THRESHOLD = 6  # 6+ of the last 10 matches vs. the same opponent
_ABNORMAL_GAIN_WINDOW_HOURS = 24
_ABNORMAL_GAIN_THRESHOLD = 300  # net MMR gained inside the window
_INTENTIONAL_LOSS_MIN_HISTORICAL_MATCHES = 10
_INTENTIONAL_LOSS_WIN_RATE_THRESHOLD = 0.6
_INTENTIONAL_LOSS_MAX_ACCURACY_PCT = 10.0
# Phase 16 — reward farming: matches finishing unnaturally fast, back to
# back. Reuses the same CompetitiveRatingHistory rows already queried for
# the abnormal-gain check — one real, already-collected timestamp series,
# not a new tracking mechanism.
_REWARD_FARMING_WINDOW_MINUTES = 15
_REWARD_FARMING_MATCH_THRESHOLD = 10


def analyze_after_match(
    db: Session, *, match_id: UUID, user_id: UUID, opponent_id: Optional[UUID],
    result: str, player_stats: Dict, stats_after: Optional[CompetitiveStatistics],
) -> None:
    """Called once per participant from gameplay_service.finish_match, after
    ranking_service has already updated rating/rating_history — a detection
    signal must never block or slow down the gameplay result itself."""
    try:
        _check_repeated_opponent(db, match_id=match_id, user_id=user_id, opponent_id=opponent_id)
        _check_abnormal_gain(db, match_id=match_id, user_id=user_id)
        _check_intentional_losing(db, match_id=match_id, user_id=user_id, result=result, player_stats=player_stats, stats_after=stats_after)
        _check_reward_farming(db, match_id=match_id, user_id=user_id)
    except Exception:
        logger.exception("anti_abuse_service.analyze_after_match failed for user=%s — swallowed (detection-only)", user_id)


def _check_repeated_opponent(db: Session, *, match_id: UUID, user_id: UUID, opponent_id: Optional[UUID]) -> None:
    if opponent_id is None:
        return
    recent = list(db.exec(
        select(CompetitiveRatingHistory)
        .where(CompetitiveRatingHistory.user_id == user_id)
        .order_by(CompetitiveRatingHistory.created_at.desc())
        .limit(_REPEATED_OPPONENT_WINDOW_MATCHES)
    ).all())
    same_opponent_rows = [r for r in recent if r.opponent_id == opponent_id]
    if len(same_opponent_rows) < _REPEATED_OPPONENT_THRESHOLD:
        return

    event_log_service.log_event(
        db, match_id=match_id, actor_id=user_id, event_type="anti_abuse_repeated_opponent_signal",
        meta={"opponent_id": str(opponent_id), "count": len(same_opponent_rows), "window": _REPEATED_OPPONENT_WINDOW_MATCHES},
    )

    # Win-trading heuristic: results against that same opponent alternate
    # win/loss almost every time, rather than reflecting genuine skill
    # variance — a real, already-tracked data point (rating_history.result),
    # not a fabricated one.
    results = [r.result for r in reversed(same_opponent_rows)]
    if len(results) >= 4:
        alternations = sum(1 for i in range(1, len(results)) if results[i] != results[i - 1])
        if alternations >= len(results) - 1:
            event_log_service.log_event(
                db, match_id=match_id, actor_id=user_id, event_type="anti_abuse_win_trading_signal",
                meta={"opponent_id": str(opponent_id), "pattern": results},
            )


def _check_abnormal_gain(db: Session, *, match_id: UUID, user_id: UUID) -> None:
    since = datetime.now(timezone.utc) - timedelta(hours=_ABNORMAL_GAIN_WINDOW_HOURS)
    rows = list(db.exec(
        select(CompetitiveRatingHistory)
        .where(CompetitiveRatingHistory.user_id == user_id)
        .where(CompetitiveRatingHistory.created_at >= since)
    ).all())
    net_gain = sum(r.delta for r in rows)
    if net_gain >= _ABNORMAL_GAIN_THRESHOLD:
        event_log_service.log_event(
            db, match_id=match_id, actor_id=user_id, event_type="anti_abuse_abnormal_gain_signal",
            meta={"net_gain": net_gain, "window_hours": _ABNORMAL_GAIN_WINDOW_HOURS, "matches": len(rows)},
        )


def _check_reward_farming(db: Session, *, match_id: UUID, user_id: UUID) -> None:
    since = datetime.now(timezone.utc) - timedelta(minutes=_REWARD_FARMING_WINDOW_MINUTES)
    count = len(list(db.exec(
        select(CompetitiveRatingHistory)
        .where(CompetitiveRatingHistory.user_id == user_id)
        .where(CompetitiveRatingHistory.created_at >= since)
    ).all()))
    if count >= _REWARD_FARMING_MATCH_THRESHOLD:
        event_log_service.log_event(
            db, match_id=match_id, actor_id=user_id, event_type="anti_abuse_reward_farming_signal",
            meta={"matches_in_window": count, "window_minutes": _REWARD_FARMING_WINDOW_MINUTES},
        )


def _check_intentional_losing(
    db: Session, *, match_id: UUID, user_id: UUID, result: str, player_stats: Dict, stats_after: Optional[CompetitiveStatistics],
) -> None:
    if result != "loss" or stats_after is None or stats_after.matches_played < _INTENTIONAL_LOSS_MIN_HISTORICAL_MATCHES:
        return
    historical_win_rate = stats_after.wins / stats_after.matches_played
    if historical_win_rate >= _INTENTIONAL_LOSS_WIN_RATE_THRESHOLD and player_stats.get("accuracy_pct", 100) <= _INTENTIONAL_LOSS_MAX_ACCURACY_PCT:
        event_log_service.log_event(
            db, match_id=match_id, actor_id=user_id, event_type="anti_abuse_intentional_loss_signal",
            meta={"historical_win_rate": round(historical_win_rate, 2), "match_accuracy_pct": player_stats.get("accuracy_pct")},
        )
