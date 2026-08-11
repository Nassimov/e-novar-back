from __future__ import annotations

"""
Domain Event Bus — Competitive Arena Phase 16.

A lightweight, in-process, synchronous pub/sub — NOT a message queue (Redis/
Celery already own that job for anything that needs to cross a process
boundary; this bus is for same-request fan-out only, e.g. "match ended"
triggering a metrics counter AND a moderation check without either knowing
about the other).

Deliberately additive, not a rip-and-replace of 15 phases' worth of already-
working, already-verified direct call chains (gameplay_service ->
arena_achievement_service -> mission_service -> notification_engine, etc.).
Rewiring all of that through the bus would be a large, high-risk refactor
with no regression-test safety net to catch a mistake — "refactor where
necessary without changing business behavior" cuts against that here. New
code (and a few of the most central existing hooks) publish through this
bus; nothing was removed from the direct call chains that already work.

Every handler runs synchronously, in registration order, and NEVER breaks
the publisher on failure (same defensive posture as notification_engine.
emit() and event_log_service.log_event()).
"""

import logging
from collections import defaultdict
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)

# ─── Event type constants (spec's own examples) ────────────────────────────
MATCH_STARTED = "MatchStarted"
MATCH_ENDED = "MatchEnded"
ANSWER_SUBMITTED = "AnswerSubmitted"
REWARD_GRANTED = "RewardGranted"
ACHIEVEMENT_UNLOCKED = "AchievementUnlocked"
LEAGUE_PROMOTED = "LeaguePromoted"
TOURNAMENT_WON = "TournamentWon"
REPLAY_GENERATED = "ReplayGenerated"
MISSION_COMPLETED = "MissionCompleted"
SANCTION_ISSUED = "SanctionIssued"
REPORT_SUBMITTED = "ReportSubmitted"

_subscribers: Dict[str, List[Callable[..., None]]] = defaultdict(list)


def subscribe(event_type: str, handler: Callable[..., None]) -> None:
    """Registers `handler(**payload)` to run whenever `event_type` is
    published. Call at import time from a module that wants to react to an
    event (see app/core/domain_events_bootstrap.py for the actual wiring —
    kept separate from this module to avoid import cycles)."""
    _subscribers[event_type].append(handler)


def publish(event_type: str, **payload: Any) -> None:
    for handler in _subscribers.get(event_type, []):
        try:
            handler(**payload)
        except Exception:
            logger.exception("domain_events.publish: handler=%s failed for event_type=%s", getattr(handler, "__name__", handler), event_type)


def clear_subscribers() -> None:
    """Test-only helper — resets all registrations between test cases."""
    _subscribers.clear()
