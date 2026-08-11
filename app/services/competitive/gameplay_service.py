from __future__ import annotations

"""
Gameplay Service — Competitive Arena Phase 3.

The backend is the ONLY source of truth: every score, timer deadline, and
winner is computed here from server timestamps — the frontend only renders
whatever compute_state() returns. Nothing here is Duel-specific in a way
that would block reuse by Club Battle/Battle Royale/Tournament later (a
"match" is just N participants working through the same ordered question
list; team aggregation for Club Battle is additive on top of this, not a
rewrite).

Phase progression (reading -> answering -> locked -> reveal -> transition)
is entirely DERIVED from timestamps on CompetitiveMatchQuestion, never a
stored enum — see _phase_of(). Celery tasks (app/workers/competitive_tasks.py)
are the primary driver of proactive WS pushes at each deadline, but every
entry point here also self-heals via ensure_progressed() if a scheduled task
lagged — so correctness never depends on perfect Celery timing.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.core.spectator_ws import publish_spectator_event
from app.models.admin import PlatformSettings
from app.models.competitive import (
    CompetitiveMatch,
    CompetitiveMatchEvent,
    CompetitiveMatchParticipant,
    CompetitiveMatchPlayerStats,
    CompetitiveMatchQuestion,
    CompetitiveQuestionAttempt,
    CompetitiveSeasonStats,
    CompetitiveStatistics,
    CompetitiveTournamentMatch,
)
from app.models.practice import Question, QuestionChoice
from app.models.profile import Profile
from app.schemas.competitive import (
    BattleRoyaleLeaderboardEntryOut,
    BattleRoyaleRevealOut,
    BattleRoyaleStateResponse,
    ChoiceOut,
    CurrentQuestionOut,
    FinalResultOut,
    MatchStateResponse,
    RevealOut,
    ScoreboardEntryOut,
    SpectatorRevealOut,
    SpectatorStateResponse,
)
from app.services.competitive import (
    anti_abuse_service,
    event_log_service,
    leaderboard_service,
    match_service,
    question_engine,
    ranking_service,
    season_service,
    tournament_service,
)
from app.services.notification_engine import emit

#: Phase 8 — a spectator-facing "combo" timeline event fires once a
#: participant's live correct-answer streak reaches this length. A display
#: heuristic only (never affects scoring), so it's a local constant rather
#: than an admin-configurable PlatformSettings knob — same treatment as
#: e.g. leaderboard_service's period tuple.
_SPECTATOR_COMBO_THRESHOLD = 3

logger = logging.getLogger(__name__)


def _settings(db: Session) -> PlatformSettings:
    return db.get(PlatformSettings, True) or PlatformSettings()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─── Match start ────────────────────────────────────────────────────────────

def start_match(db: Session, match: CompetitiveMatch) -> CompetitiveMatch:
    """Fired once the pre-match countdown (status='countdown') elapses.
    Locks the match configuration (nothing below can change afterward),
    generates the authoritative question list, and begins Question 1."""
    if match.status != "countdown":
        return match  # already progressed / cancelled meanwhile — no-op

    participants = match_service.get_participants(db, match.id)
    player_ids = [p.user_id for p in participants]

    questions = question_engine.select_questions_for_match(
        db,
        subject_ids=match.subject_ids,
        school_level_id=match.school_level_id,
        difficulty=match.difficulty,
        question_count=match.question_count,
        player_ids=player_ids,
    )
    for position, question in enumerate(questions):
        db.add(CompetitiveMatchQuestion(match_id=match.id, question_id=question.id, position=position))

    match.started_at = _now()
    match_service.transition(match, "in_progress")
    db.add(match)
    db.commit()
    db.refresh(match)

    event_log_service.log_event(db, match_id=match.id, actor_id=None, event_type="match_started", meta={
        "question_count": len(questions),
    })

    # Phase 10, Part B — hand-off from Part A's waiting-room/countdown
    # machinery into the live elimination engine: flips
    # CompetitiveBattleRoyaleMatch.current_phase 'countdown' -> 'running',
    # freezes remaining_players_count, and (elimination_mode='lives') seeds
    # every participant's starting lives. See battle_royale_service.
    # handle_gameplay_started's own docstring for why this is deliberately
    # NOT wrapped in try/except.
    if match.match_type == "battle_royale":
        from app.services.competitive import battle_royale_service
        battle_royale_service.handle_gameplay_started(db, match)

    _begin_question(db, match, position=0)
    schedule_next_tick(db, match)
    return match


def _begin_question(db: Session, match: CompetitiveMatch, *, position: int) -> CompetitiveMatchQuestion:
    settings_row = _settings(db)
    mq = db.exec(
        select(CompetitiveMatchQuestion)
        .where(CompetitiveMatchQuestion.match_id == match.id)
        .where(CompetitiveMatchQuestion.position == position)
    ).first()
    if mq is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Question de match introuvable.")

    question = db.get(Question, mq.question_id)
    now = _now()
    mq.reading_ends_at = now + timedelta(seconds=settings_row.competitive_reading_time_seconds)
    mq.answer_ends_at = mq.reading_ends_at + timedelta(seconds=question.estimated_time_sec if question else 30)
    db.add(mq)

    match.current_question_position = position
    db.add(match)
    db.commit()
    db.refresh(mq)

    event_log_service.log_event(
        db, match_id=match.id, actor_id=None, event_type="question_started",
        meta={"position": position, "question_id": str(mq.question_id)},
    )
    return mq


def get_next_deadline(db: Session, match: CompetitiveMatch) -> Optional[datetime]:
    mq = get_current_match_question(db, match)
    if mq is None:
        return None
    if mq.revealed_at is None:
        return mq.answer_ends_at
    settings_row = _settings(db)
    return mq.revealed_at + timedelta(seconds=settings_row.competitive_transition_time_seconds)


def schedule_next_tick(db: Session, match: CompetitiveMatch) -> None:
    """Chains the next Celery gameplay tick to fire exactly at the current
    question's next phase deadline — see app/workers/competitive_tasks.py's
    task_gameplay_tick. Submitting an early answer (both players done before
    timeout) calls this too so the chain catches up immediately instead of
    waiting for the now-stale scheduled tick (which will still fire later,
    harmlessly, as a no-op thanks to ensure_progressed's idempotency)."""
    if match.status != "in_progress":
        return
    deadline = get_next_deadline(db, match)
    if deadline is None:
        return
    delay = max(1.0, (deadline - _now()).total_seconds())
    try:
        from app.workers.competitive_tasks import task_gameplay_tick
        task_gameplay_tick.apply_async(args=[str(match.id)], countdown=delay)
    except Exception:
        logger.exception("failed to schedule gameplay tick for match=%s", match.id)


def get_current_match_question(db: Session, match: CompetitiveMatch) -> Optional[CompetitiveMatchQuestion]:
    if match.current_question_position is None:
        return None
    return db.exec(
        select(CompetitiveMatchQuestion)
        .where(CompetitiveMatchQuestion.match_id == match.id)
        .where(CompetitiveMatchQuestion.position == match.current_question_position)
    ).first()


def _attempts_for(db: Session, match_question_id: UUID) -> List[CompetitiveQuestionAttempt]:
    return list(
        db.exec(
            select(CompetitiveQuestionAttempt).where(CompetitiveQuestionAttempt.match_question_id == match_question_id)
        ).all()
    )


def _current_streak(db: Session, match: CompetitiveMatch, user_id: UUID) -> int:
    """Live consecutive-correct-answers streak, ordered by question position
    — same computation _scoreboard() already does per participant, factored
    out here so Phase 8's combo detection in _lock_and_reveal doesn't
    duplicate it inline."""
    rows = list(
        db.exec(
            select(CompetitiveQuestionAttempt, CompetitiveMatchQuestion.position)
            .join(CompetitiveMatchQuestion, CompetitiveMatchQuestion.id == CompetitiveQuestionAttempt.match_question_id)
            .where(CompetitiveMatchQuestion.match_id == match.id)
            .where(CompetitiveQuestionAttempt.user_id == user_id)
            .order_by(CompetitiveMatchQuestion.position)
        ).all()
    )
    streak = 0
    for a, _ in rows:
        streak = streak + 1 if a.is_correct else 0
    return streak


def _both_answered(db: Session, match: CompetitiveMatch, mq: CompetitiveMatchQuestion) -> bool:
    participants = match_service.get_participants(db, match.id)
    attempts = _attempts_for(db, mq.id)
    answered_ids = {a.user_id for a in attempts}
    return all(p.user_id in answered_ids for p in participants) and len(participants) > 0


# ─── Locking & scoring ──────────────────────────────────────────────────────

def _lock_and_reveal(db: Session, match: CompetitiveMatch, mq: CompetitiveMatchQuestion) -> None:
    if mq.revealed_at is not None:
        return  # already processed — idempotent no-op (race-safe)

    settings_row = _settings(db)
    correct_choice_ids = {
        c.id for c in db.exec(select(QuestionChoice).where(QuestionChoice.question_id == mq.question_id)).all()
        if c.is_correct
    }
    answer_window_ms = None
    if mq.reading_ends_at and mq.answer_ends_at:
        answer_window_ms = int((mq.answer_ends_at - mq.reading_ends_at).total_seconds() * 1000)

    attempts = _attempts_for(db, mq.id)
    for attempt in attempts:
        is_correct = bool(correct_choice_ids) and set(attempt.selected_choice_ids) == correct_choice_ids
        attempt.is_correct = is_correct
        points = settings_row.competitive_points_per_correct if is_correct else 0
        if is_correct and settings_row.competitive_speed_bonus_enabled and answer_window_ms and attempt.response_time_ms is not None:
            # Linear decay: instant answer -> full bonus, answer at the very
            # end of the window -> zero bonus. "Do NOT implement [in Phase
            # 3]. Only prepare architecture" — this is that future phase.
            elapsed_ratio = min(1.0, max(0.0, attempt.response_time_ms / answer_window_ms))
            points += round(settings_row.competitive_speed_bonus_max_points * (1 - elapsed_ratio))
        attempt.points_earned = points
        db.add(attempt)

        participant = db.exec(
            select(CompetitiveMatchParticipant)
            .where(CompetitiveMatchParticipant.match_id == match.id)
            .where(CompetitiveMatchParticipant.user_id == attempt.user_id)
        ).first()
        if participant is not None:
            participant.score += attempt.points_earned
            db.add(participant)

    now = _now()
    mq.locked_at = now
    mq.revealed_at = now
    db.add(mq)
    db.commit()

    event_log_service.log_event(
        db, match_id=match.id, actor_id=None, event_type="question_revealed",
        meta={"position": mq.position, "answered_count": len(attempts)},
    )

    # Phase 8 — spectator timeline: fire once, right here, where the actual
    # per-attempt result is known (never in the Celery tick wrapper, which
    # only ever sees "something changed", not what). Structured (not
    # pre-translated text) so the frontend renders/i18n's it; "kind" is the
    # discriminator.
    try:
        for attempt in attempts:
            publish_spectator_event(str(match.id), {
                "type": "timeline_event",
                "kind": "correct_answer" if attempt.is_correct else "wrong_answer",
                "position": mq.position, "user_id": str(attempt.user_id), "at": now.isoformat(),
            })
            if attempt.is_correct:
                streak = _current_streak(db, match, attempt.user_id)
                if streak >= _SPECTATOR_COMBO_THRESHOLD:
                    publish_spectator_event(str(match.id), {
                        "type": "timeline_event", "kind": "combo",
                        "user_id": str(attempt.user_id), "streak": streak, "at": now.isoformat(),
                    })
    except Exception:
        logger.debug("spectator timeline_event publish failed match=%s position=%s", match.id, mq.position, exc_info=True)

    # Phase 10, Part B — Battle Royale elimination engine: recomputes the
    # live leaderboard for this question_position and applies whatever
    # elimination_mode this match is configured with (score_elimination/
    # lives/survival), cascading phase progression (final_phase/final_duel)
    # and match completion as needed. Own independent try/except, mirroring
    # the Phase 8 spectator timeline block just above — a bug in the BR
    # elimination engine must never break core scoring, which is already
    # fully committed by this point regardless of match_type.
    if match.match_type == "battle_royale":
        try:
            from app.services.competitive import battle_royale_service
            battle_royale_service.process_elimination_tick(db, match, mq)
        except Exception:
            logger.exception("battle royale elimination tick failed match=%s position=%s", match.id, mq.position)


def ensure_progressed(db: Session, match: CompetitiveMatch) -> CompetitiveMatch:
    """Self-healing progression: catches the match up to where it should be
    right now, regardless of whether a scheduled Celery task already ran.
    Safe to call from every gameplay entry point (idempotent)."""
    settings_row = _settings(db)
    guard = 0
    while match.status == "in_progress" and guard < match.question_count + 1:
        guard += 1
        mq = get_current_match_question(db, match)
        if mq is None:
            break
        now = _now()

        if mq.revealed_at is None:
            if (mq.answer_ends_at and now >= mq.answer_ends_at) or _both_answered(db, match, mq):
                _lock_and_reveal(db, match, mq)
                db.refresh(match)
            else:
                break
        else:
            transition_deadline = mq.revealed_at + timedelta(seconds=settings_row.competitive_transition_time_seconds)
            if now >= transition_deadline:
                next_position = mq.position + 1
                if next_position >= match.question_count:
                    if match.match_type == "battle_royale":
                        # A sole survivor (remaining_players_count == 1)
                        # already completed the match earlier, from inside
                        # process_elimination_tick — by the time we get
                        # here that path is either already done (this call
                        # becomes a no-op, see below) or this is the "ran
                        # out of questions with >= 2 still active" end
                        # condition (Task 4), which the generic finish_match
                        # below does NOT know how to resolve for N players.
                        try:
                            from app.services.competitive import battle_royale_service
                            battle_royale_service.complete_from_exhausted_questions(db, match)
                        except Exception:
                            logger.exception(
                                "battle royale exhausted-questions completion failed match=%s — "
                                "falling back to generic finish_match so the match never gets stuck",
                                match.id,
                            )
                            finish_match(db, match)
                    elif match.match_type == "club_battle":
                        # Phase 11, Part C — a club_battle has no single
                        # "opponent" (up to 20 participants split into two
                        # teams), so the generic 2-player/N-player-draw
                        # finish_match branches below are meaningless for it
                        # — battle_service computes team totals, decides the
                        # winning team, and calls finish_match(forced_
                        # results=...) itself, mirroring
                        # battle_royale_service.complete_from_exhausted_
                        # questions' exact role.
                        try:
                            from app.services.club import battle_service
                            battle_service.finalize_club_battle(db, match)
                        except Exception:
                            logger.exception(
                                "club battle exhausted-questions completion failed match=%s — "
                                "falling back to generic finish_match so the match never gets stuck",
                                match.id,
                            )
                            finish_match(db, match)
                    else:
                        finish_match(db, match)
                    db.refresh(match)
                    break
                _begin_question(db, match, position=next_position)
                db.refresh(match)
            else:
                break
    return match


# ─── Answer submission ──────────────────────────────────────────────────────

def submit_answer(
    db: Session, match: CompetitiveMatch, *, user_id: UUID, match_question_id: UUID, selected_choice_ids: List[UUID],
) -> CompetitiveMatchQuestion:
    ensure_progressed(db, match)

    if match.status != "in_progress":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce match n'est pas en cours.")

    participants = match_service.get_participants(db, match.id)
    if user_id not in [p.user_id for p in participants]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tu ne participes pas à ce match.")

    mq = get_current_match_question(db, match)
    if mq is None or mq.id != match_question_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cette question n'est plus active.")

    now = _now()
    if mq.reading_ends_at and now < mq.reading_ends_at:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="La lecture n'est pas terminée.")
    if mq.locked_at is not None or (mq.answer_ends_at and now > mq.answer_ends_at):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Le temps de réponse est écoulé.")

    existing = db.exec(
        select(CompetitiveQuestionAttempt)
        .where(CompetitiveQuestionAttempt.match_question_id == mq.id)
        .where(CompetitiveQuestionAttempt.user_id == user_id)
    ).first()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tu as déjà répondu à cette question.")

    valid_choice_ids = {
        c.id for c in db.exec(select(QuestionChoice).where(QuestionChoice.question_id == mq.question_id)).all()
    }
    if len(selected_choice_ids) != 1 or not set(selected_choice_ids).issubset(valid_choice_ids):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Réponse invalide pour cette question.")

    response_time_ms = int((now - mq.reading_ends_at).total_seconds() * 1000) if mq.reading_ends_at else None
    attempt = CompetitiveQuestionAttempt(
        match_question_id=mq.id, user_id=user_id, selected_choice_ids=selected_choice_ids,
        response_time_ms=response_time_ms, submitted_at=now,
    )
    db.add(attempt)
    db.commit()

    event_log_service.log_event(
        db, match_id=match.id, actor_id=user_id, event_type="answer_submitted",
        meta={"position": mq.position, "response_time_ms": response_time_ms},
    )

    if _both_answered(db, match, mq):
        _lock_and_reveal(db, match, mq)
        db.refresh(match)
        schedule_next_tick(db, match)

    db.refresh(mq)
    return mq


# ─── Winner calculation & completion ────────────────────────────────────────

def _aggregate_player_stats(db: Session, match: CompetitiveMatch, user_id: UUID) -> Dict:
    rows = list(
        db.exec(
            select(CompetitiveQuestionAttempt, CompetitiveMatchQuestion.position)
            .join(CompetitiveMatchQuestion, CompetitiveMatchQuestion.id == CompetitiveQuestionAttempt.match_question_id)
            .where(CompetitiveMatchQuestion.match_id == match.id)
            .where(CompetitiveQuestionAttempt.user_id == user_id)
            .order_by(CompetitiveMatchQuestion.position)
        ).all()
    )
    correct = sum(1 for a, _ in rows if a.is_correct)
    wrong = sum(1 for a, _ in rows if not a.is_correct)
    unanswered = match.question_count - len(rows)
    response_times = [a.response_time_ms for a, _ in rows if a.response_time_ms is not None]
    avg_ms = round(sum(response_times) / len(response_times)) if response_times else None
    score = sum(a.points_earned for a, _ in rows)

    best_streak = 0
    current_run = 0
    last_correct_at: Optional[datetime] = None
    for a, _ in rows:
        if a.is_correct:
            current_run += 1
            best_streak = max(best_streak, current_run)
            last_correct_at = a.submitted_at
        else:
            current_run = 0

    accuracy_pct = round((correct / match.question_count) * 100, 2) if match.question_count else 0.0
    return {
        "score": score, "correct": correct, "wrong": wrong, "unanswered": unanswered,
        "accuracy_pct": accuracy_pct, "avg_response_time_ms": avg_ms, "best_streak": best_streak,
        "last_correct_at": last_correct_at,
    }


def _decide_result(a: Dict, b: Dict) -> str:
    """Returns 'win'|'loss'|'draw' for player A vs player B, per the spec's
    tie-break order: score -> accuracy -> avg response time -> fastest last
    correct answer -> draw."""
    if a["score"] != b["score"]:
        return "win" if a["score"] > b["score"] else "loss"
    if a["accuracy_pct"] != b["accuracy_pct"]:
        return "win" if a["accuracy_pct"] > b["accuracy_pct"] else "loss"
    a_time = a["avg_response_time_ms"]
    b_time = b["avg_response_time_ms"]
    if a_time is not None and b_time is not None and a_time != b_time:
        return "win" if a_time < b_time else "loss"
    a_last = a["last_correct_at"]
    b_last = b["last_correct_at"]
    if a_last and b_last and a_last != b_last:
        return "win" if a_last < b_last else "loss"
    return "draw"


def _grant_casual_reward(db: Session, user_id: UUID, result: str, settings_row: PlatformSettings) -> None:
    """Phase 13 — Casual queue consolation reward (spec: "Casual: ... EP
    rewards, XP rewards"). Best-effort — a reward failure must never break
    the already-final match result."""
    from app.models.enums import KpSource
    from app.services import kp as kp_service

    amount = settings_row.competitive_casual_ep_reward_winner if result == "win" else settings_row.competitive_casual_ep_reward_participation
    if amount <= 0:
        return
    try:
        kp_service.award_kp(user_id, amount, KpSource.competitive, "Match amical Arène", db)
    except Exception:
        logger.exception("Phase 13 casual reward grant failed user=%s", user_id)


def finish_match(
    db: Session, match: CompetitiveMatch, *,
    forced_results: Optional[Dict[UUID, str]] = None, terminal_status: str = "completed",
) -> CompetitiveMatch:
    """Normal end-of-questions completion (forced_results=None) AND forfeit
    paths (leave_match, in-game disconnect timeout) both funnel through
    here — a forfeited match still gets a full replay/AI analysis/stats
    pipeline (Phase 4), just with the result decided by forfeit rather than
    by score. `_aggregate_player_stats` already handles a match cut short
    correctly (remaining questions simply count as unanswered)."""
    if match.status != "in_progress":
        return match

    participants = match_service.get_participants(db, match.id)
    stats_by_user = {p.user_id: _aggregate_player_stats(db, match, p.user_id) for p in participants}

    if forced_results is not None:
        results = forced_results
    else:
        results = {}
        ids = list(stats_by_user.keys())
        if len(ids) == 2:
            a, b = ids
            outcome_for_a = _decide_result(stats_by_user[a], stats_by_user[b])
            results[a] = outcome_for_a
            results[b] = "draw" if outcome_for_a == "draw" else ("loss" if outcome_for_a == "win" else "win")
        else:
            for uid in ids:
                results[uid] = "draw"

    match.completed_at = _now()
    match_service.transition(match, terminal_status)
    db.add(match)

    for participant in participants:
        s = stats_by_user[participant.user_id]
        result = results[participant.user_id]
        participant.result = result
        participant.score = s["score"]
        db.add(participant)

        db.add(CompetitiveMatchPlayerStats(
            match_id=match.id, user_id=participant.user_id, score=s["score"],
            correct_count=s["correct"], wrong_count=s["wrong"], unanswered_count=s["unanswered"],
            accuracy_pct=s["accuracy_pct"], avg_response_time_ms=s["avg_response_time_ms"],
            best_streak=s["best_streak"], result=result,
        ))

        # Raw lifetime counters update now; rating/rank_tier stay untouched —
        # "Do not calculate ranking yet" (Phase 7 owns that).
        life_stats = db.get(CompetitiveStatistics, participant.user_id)
        if life_stats is None:
            life_stats = CompetitiveStatistics(user_id=participant.user_id)
        life_stats.matches_played += 1
        if result == "win":
            life_stats.wins += 1
            life_stats.current_streak = max(0, life_stats.current_streak) + 1
        elif result == "loss":
            life_stats.losses += 1
            life_stats.current_streak = 0
        else:
            life_stats.draws += 1
            life_stats.current_streak = 0
        life_stats.longest_streak = max(life_stats.longest_streak, life_stats.current_streak)
        if s["accuracy_pct"] == 100:
            life_stats.perfect_matches += 1
        prev_total = life_stats.matches_played - 1
        life_stats.accuracy_pct = round(
            ((life_stats.accuracy_pct * prev_total) + s["accuracy_pct"]) / life_stats.matches_played, 2
        )
        if s["avg_response_time_ms"] is not None:
            if life_stats.avg_response_time_ms is None:
                life_stats.avg_response_time_ms = s["avg_response_time_ms"]
            else:
                life_stats.avg_response_time_ms = round(
                    ((life_stats.avg_response_time_ms * prev_total) + s["avg_response_time_ms"]) / life_stats.matches_played
                )
        life_stats.updated_at = _now()
        db.add(life_stats)

        # Phase 13 — Fair Play signals, only for a match that actually ran
        # its course (not a forfeit/disconnect — that path already
        # penalizes separately in forfeit_by_disconnect, and double-
        # penalizing the same absence would be unfair). A player present
        # for every question but never once answering reads as AFK; a
        # player who answered every question (regardless of correctness)
        # is rewarded for simply playing the match out cleanly.
        if terminal_status == "completed" and match.question_count > 1:
            from app.services.competitive import fair_play_service
            if s["unanswered"] >= match.question_count:
                fair_play_service.penalize_afk(db, participant.user_id, match_id=match.id)
            elif s["unanswered"] == 0:
                fair_play_service.reward_clean_match(db, participant.user_id, match_id=match.id)

    db.commit()

    # Phase 14 — Arena Achievement Engine: runs for EVERY match_type/
    # ranked-or-casual (spec: "every completed activity should contribute
    # toward long-term progression") — deliberately its own pass, right
    # after lifetime counters commit, independent of the Ranking Engine
    # pass below (which casual/club_battle matches skip entirely).
    from app.services.competitive import arena_achievement_service
    for participant in participants:
        arena_achievement_service.check_and_unlock_arena_achievements(db, participant.user_id)

    # Phase 15 — Mission Engine: same "every match_type/ranked-or-casual"
    # posture as the achievement pass above — every completed activity
    # feeds daily/weekly/monthly/seasonal missions AND any active
    # Community Challenge/Limited-Time event sharing the same metric_key
    # (mission_service.record_event fans out to event_service internally).
    from app.services.competitive import mission_service
    for participant in participants:
        s = stats_by_user[participant.user_id]
        result = results[participant.user_id]
        life_stats = db.get(CompetitiveStatistics, participant.user_id)
        mission_service.record_event(db, user_id=participant.user_id, metric_key="arena_match_played", amount=1)
        if result == "win":
            mission_service.record_event(db, user_id=participant.user_id, metric_key="arena_win", amount=1)
        mission_service.record_event(
            db, user_id=participant.user_id, metric_key="arena_question_answered",
            amount=s["correct"] + s["wrong"],
        )
        mission_service.record_event(
            db, user_id=participant.user_id, metric_key="arena_accuracy_session", amount=s["accuracy_pct"],
        )
        if life_stats is not None:
            mission_service.record_event(
                db, user_id=participant.user_id, metric_key="arena_win_streak",
                amount=life_stats.current_streak, mode="set_max",
            )
        if s["accuracy_pct"] == 100:
            mission_service.record_event(db, user_id=participant.user_id, metric_key="arena_perfect_match", amount=1)
        if match.match_type == "club_battle":
            mission_service.record_event(db, user_id=participant.user_id, metric_key="arena_club_battle_played", amount=1)
        mission_service.check_state_missions(db, participant.user_id)

    # Phase 16 — Domain Event Bus: additive publish, doesn't replace any of
    # the direct calls above/below (see app/core/domain_events.py docstring).
    from app.core import domain_events
    domain_events.publish(
        domain_events.MATCH_ENDED, match_id=match.id, match_type=match.match_type,
        participant_ids=[p.user_id for p in participants],
    )

    # Phase 7 — Ranking Engine: the first phase to actually WRITE
    # rating/rank_tier (every phase since Phase 1 only ever read it). Run as
    # its own pass, after the raw-counters commit above, so ranking always
    # sees each player's fully-committed post-match lifetime stats (streak
    # included) rather than reasoning about partially-flushed session state.
    #
    # Phase 10 Part C — Battle Royale: a BR match has NO single "opponent"
    # (participants can number up to 100), so the duel-shaped
    # `other = next((p for p in participants if p.user_id != participant.
    # user_id), None)` pairing below is meaningless for it — it would just
    # pick whichever OTHER participant happens to be first in the list and
    # apply everyone's Elo delta against that one arbitrary "opponent",
    # regardless of their actual final standing. For match_type=
    # 'battle_royale' we instead compute each participant's delta as the
    # AVERAGE of pairwise Elo deltas (via ranking_service._compute_delta)
    # against every OTHER participant, derived from RELATIVE final_rank
    # (lower number = better placement) — see is_battle_royale branch below.
    # Every non-battle_royale match type keeps the original `other = next(
    # ...)` logic completely unchanged.
    season = season_service.get_active_season(db)
    settings_row = _settings(db)
    is_battle_royale = match.match_type == "battle_royale"
    is_club_battle = match.match_type == "club_battle"
    br_ranking_impact = True
    br_ratings_by_user: Dict[UUID, int] = {}
    br_streaks_by_user: Dict[UUID, int] = {}
    if is_battle_royale:
        from app.models.competitive import CompetitiveBattleRoyaleMatch
        br_match_row = db.get(CompetitiveBattleRoyaleMatch, match.id)
        # "Ranking Impact" per spec only toggles whether Arena Rating itself
        # moves — it does NOT toggle whether the match counts at all (the
        # unconditional lifetime-counter updates above already always run).
        br_ranking_impact = br_match_row.ranking_impact if br_match_row is not None else True
        if br_ranking_impact:
            from app.services.competitive import statistics_service
            for p in participants:
                p_stats = db.get(CompetitiveStatistics, p.user_id)
                if p_stats is None:
                    p_stats = statistics_service.get_or_create_statistics(db, p.user_id)
                br_ratings_by_user[p.user_id] = p_stats.rating
                br_streaks_by_user[p.user_id] = p_stats.current_streak

    for participant in participants:
        result = results[participant.user_id]
        s = stats_by_user[participant.user_id]

        if not match.is_ranked and not is_club_battle:
            # Phase 13 — Casual queue: lifetime counters already updated
            # unconditionally above (Statistics still recorded — spec), but
            # Arena Rating/league/season/leaderboard/anti-abuse are all
            # skipped, exactly like club_battle's own pre-existing carve-out
            # just below — the ranking engine treats "casual" and "club
            # battle" identically for this purpose (individual rating is
            # simply not this match's concern). A configured EP consolation
            # reward replaces the rating movement casual players don't get.
            _grant_casual_reward(db, participant.user_id, result, settings_row)
            continue

        if is_club_battle:
            # Phase 11, Part C — individual Arena Rating/league/season-
            # leaderboard stay untouched by a club battle (spec: "Club
            # Rating... Independent from player rating"). The lifetime
            # counters (matches_played/wins/accuracy/streak) already updated
            # unconditionally above still apply — only the Ranking Engine
            # pass below (Elo, season MMR, leaderboard cache, rank-milestone
            # notifications, anti-abuse) is skipped; battle_service runs the
            # CLUB-level rating engine separately, after finish_match returns.
            continue

        if is_battle_royale:
            if not br_ranking_impact:
                # Ranking Impact is OFF for this Battle Royale — Arena
                # Rating/league never move for anyone in this match. Every
                # downstream ranking-only side effect (season stats, the
                # leaderboard cache, rank-milestone notifications, the
                # anti-abuse pass) is skipped too, since all of it exists to
                # describe/react to a rating change that never happened
                # here; the lifetime counters committed above are the only
                # thing this match still contributes.
                continue

            my_rank = participant.final_rank
            my_rating = br_ratings_by_user.get(participant.user_id, settings_row.competitive_mmr_initial_rating)
            my_streak = br_streaks_by_user.get(participant.user_id, 0)
            others = [p for p in participants if p.user_id != participant.user_id]
            pairwise_deltas: List[int] = []
            for other_p in others:
                other_rank = other_p.final_rank
                other_rating = br_ratings_by_user.get(other_p.user_id, settings_row.competitive_mmr_initial_rating)
                if my_rank is None or other_rank is None or my_rank == other_rank:
                    # Ties shouldn't occur — final_rank is meant to be a
                    # strict total order (see battle_royale_service.
                    # _apply_eliminations/_finalize_battle_royale) — but
                    # handle one gracefully rather than crash if it ever
                    # happens (e.g. a simultaneous double-KO edge case).
                    pairwise_result = "draw"
                elif my_rank < other_rank:
                    pairwise_result = "win"
                else:
                    pairwise_result = "loss"
                pairwise_deltas.append(ranking_service._compute_delta(
                    rating_a=my_rating, rating_b=other_rating, result=pairwise_result,
                    streak_after=my_streak, settings_row=settings_row,
                ))
            # Average, not sum — keeps a Battle Royale's overall rating
            # movement in a magnitude comparable to a normal 1v1 duel rather
            # than compounding into an enormous swing purely because there
            # were N-1 pairwise comparisons.
            avg_delta = round(sum(pairwise_deltas) / len(pairwise_deltas)) if pairwise_deltas else 0

            rating_change = ranking_service.apply_battle_royale_match_result(
                db, match_id=match.id, user_id=participant.user_id, result=result,
                season_id=season.id if season else None, delta=avg_delta,
            )
            opponent_id = None
        else:
            other = next((p for p in participants if p.user_id != participant.user_id), None)
            opponent_id = other.user_id if other else None

            rating_change = ranking_service.apply_match_result(
                db, match_id=match.id, user_id=participant.user_id, opponent_id=opponent_id,
                result=result, season_id=season.id if season else None,
            )

        season_stats_row = None
        if season is not None:
            stats_after = db.get(CompetitiveStatistics, participant.user_id)
            season_stats_row = season_service.record_match_for_season(
                db, season_id=season.id, user_id=participant.user_id,
                rating_after=rating_change.rating_after,
                league_id=stats_after.league_id if stats_after else None,
                result=result, accuracy_pct=s["accuracy_pct"], response_time_ms=s["avg_response_time_ms"],
            )

        leaderboard_service.touch_user(
            user_id=participant.user_id, rating=rating_change.rating_after,
            season_id=season.id if season else None,
            season_mmr=season_stats_row.mmr if season_stats_row else None,
        )
        leaderboard_service.check_and_notify_rank_milestones(db, participant.user_id)

        anti_abuse_service.analyze_after_match(
            db, match_id=match.id, user_id=participant.user_id, opponent_id=opponent_id,
            result=result, player_stats=s, stats_after=db.get(CompetitiveStatistics, participant.user_id),
        )

    for event_type in ("match_finished", "rewards_pending", "statistics_ready", "replay_ready"):
        event_log_service.log_event(db, match_id=match.id, actor_id=None, event_type=event_type, meta={})

    for participant in participants:
        result = results[participant.user_id]
        result_label = {"win": "Victoire", "loss": "Défaite", "draw": "Match nul"}[result]
        emit(
            db, event_type="competitive_match_result", user_id=participant.user_id,
            context={"result": result_label},
            data={"match_id": str(match.id), "result": result},
            dedup_key=f"competitive_match_result:{match.id}:{participant.user_id}",
        )

    # Phase 8 — spectators: resolve open predictions (Arena XP/Spectator XP
    # only, never Arena Rating/MMR, never EP — see spectator_service's
    # docstring) and broadcast the final "victory" timeline event. Both are
    # best-effort: a spectator-facing side effect must never break the
    # already-final match result computed above.
    winner_id = next((uid for uid, r in results.items() if r == "win"), None)
    is_draw_result = all(r == "draw" for r in results.values())
    try:
        from app.services.competitive import spectator_service
        spectator_service.resolve_predictions_for_match(
            db, match_id=match.id, winner_id=winner_id, is_draw=is_draw_result,
        )
        publish_spectator_event(str(match.id), {
            "type": "timeline_event", "kind": "victory",
            "winner_id": str(winner_id) if winner_id else None, "is_draw": is_draw_result,
            "at": _now().isoformat(),
        })
    except Exception:
        logger.exception("Phase 8 prediction resolution / victory broadcast failed for match=%s", match.id)

    # Phase 9 — Tournament progression: if this match is the real duel
    # behind a bracket slot (most matches aren't — one indexed lookup, a
    # no-op for every non-tournament match), resolve its winner into
    # resolve_tournament_match() so the bracket advances automatically.
    # Reuses the SAME winner_id/is_draw_result already derived above for
    # Phase 8, rather than recomputing it. Independent try/except, same
    # defensive posture as every other hook here — a bug in tournament
    # progression must never break the already-committed core match result.
    try:
        tournament_match = db.exec(
            select(CompetitiveTournamentMatch).where(CompetitiveTournamentMatch.match_id == match.id)
        ).first()
        if tournament_match is not None and tournament_match.status != "completed":
            tm_winner_id = winner_id
            if tm_winner_id is None and is_draw_result:
                from app.models.competitive import CompetitiveTournament
                tournament = db.get(CompetitiveTournament, tournament_match.tournament_id)
                if tournament is not None:
                    tm_winner_id = tournament_service.resolve_draw_winner(db, tournament, tournament_match, results)
            if tm_winner_id is not None:
                tournament_service.resolve_tournament_match(db, tournament_match, tm_winner_id)
            else:
                logger.warning(
                    "tournament progression: no winner determinable for tournament_match=%s match=%s",
                    tournament_match.id, match.id,
                )
    except Exception:
        logger.exception("Phase 9 tournament progression hook failed for match=%s", match.id)

    db.refresh(match)

    # Phase 4 — fire-and-forget: the match result above is already final and
    # visible to players; replay/AI-analysis/recommendations processing
    # continues in the background (spec: "Replay generation must be
    # asynchronous. The match result must appear immediately.").
    try:
        from app.workers.competitive_tasks import task_process_match_completion
        task_process_match_completion.apply_async(args=[str(match.id)])
    except Exception:
        logger.exception("failed to dispatch task_process_match_completion for match=%s", match.id)

    return match


def leave_match(db: Session, match: CompetitiveMatch, *, user_id: UUID) -> CompetitiveMatch:
    """Voluntary quit during gameplay — immediate forfeit, no grace period
    (distinct from the disconnect-timeout forfeit in
    app/workers/competitive_tasks.py's task_check_ingame_disconnect, which
    gives a reconnect window first)."""
    if match.status != "in_progress":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce match n'est pas en cours.")
    participants = match_service.get_participants(db, match.id)
    if user_id not in [p.user_id for p in participants]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tu ne participes pas à ce match.")

    forced_results = {p.user_id: ("loss" if p.user_id == user_id else "win") for p in participants}
    match = finish_match(db, match, forced_results=forced_results, terminal_status="abandoned")

    event_log_service.log_event(db, match_id=match.id, actor_id=user_id, event_type="match_left_voluntarily", meta={})
    return match


def forfeit_by_disconnect(db: Session, match: CompetitiveMatch, *, disconnected_user_id: UUID) -> Optional[CompetitiveMatch]:
    """Called by task_check_ingame_disconnect once the in-game reconnect
    grace period has elapsed with no reconnection. Returns None (no-op) if
    the match already moved on for any other reason."""
    if match.status != "in_progress":
        return None
    participants = match_service.get_participants(db, match.id)
    if disconnected_user_id not in [p.user_id for p in participants]:
        return None

    forced_results = {p.user_id: ("loss" if p.user_id == disconnected_user_id else "win") for p in participants}
    match = finish_match(db, match, forced_results=forced_results, terminal_status="abandoned")

    event_log_service.log_event(
        db, match_id=match.id, actor_id=None, event_type="match_forfeited_disconnect",
        meta={"disconnected_user_id": str(disconnected_user_id)},
    )

    from app.services.competitive import fair_play_service
    fair_play_service.penalize_disconnect(db, disconnected_user_id, match_id=match.id)

    return match


# ─── State computation ──────────────────────────────────────────────────────

def _scoreboard(db: Session, match: CompetitiveMatch) -> List[ScoreboardEntryOut]:
    entries = []
    for p in match_service.get_participants(db, match.id):
        profile = db.get(Profile, p.user_id)
        rows = list(
            db.exec(
                select(CompetitiveQuestionAttempt, CompetitiveMatchQuestion.position)
                .join(CompetitiveMatchQuestion, CompetitiveMatchQuestion.id == CompetitiveQuestionAttempt.match_question_id)
                .where(CompetitiveMatchQuestion.match_id == match.id)
                .where(CompetitiveQuestionAttempt.user_id == p.user_id)
                .order_by(CompetitiveMatchQuestion.position)
            ).all()
        )
        correct = sum(1 for a, _ in rows if a.is_correct)
        wrong = sum(1 for a, _ in rows if not a.is_correct)
        current_run = 0
        for a, _ in rows:
            current_run = current_run + 1 if a.is_correct else 0
        answered = correct + wrong
        entries.append(ScoreboardEntryOut(
            user_id=p.user_id, full_name=profile.full_name if profile else None,
            score=p.score, correct_count=correct, wrong_count=wrong, current_streak=current_run,
            accuracy_pct=round((correct / answered) * 100, 1) if answered else 0.0,
            team_side=p.team_side, club_id=p.club_id,
        ))
    return entries


def compute_state(db: Session, match: CompetitiveMatch, *, viewer_id: UUID) -> MatchStateResponse:
    settings_row = _settings(db)
    now = _now()

    if match.status == "countdown":
        deadline = match.updated_at + timedelta(seconds=settings_row.competitive_match_countdown_seconds)
        if now >= deadline:
            # Self-heal: the scheduled task_begin_match_after_countdown may
            # have been dropped (broker hiccup) — never leave a match
            # permanently stuck once its countdown has genuinely elapsed.
            start_match(db, match)
        else:
            return MatchStateResponse(
                match_id=match.id, status=match.status, phase="countdown", phase_ends_at=deadline,
                server_time=now, question_count=match.question_count,
                scoreboard=_scoreboard(db, match),
            )

    ensure_progressed(db, match)

    if match.status == "completed":
        player_stats = list(
            db.exec(select(CompetitiveMatchPlayerStats).where(CompetitiveMatchPlayerStats.match_id == match.id)).all()
        )
        winner_id = next((s.user_id for s in player_stats if s.result == "win"), None)
        is_draw = all(s.result == "draw" for s in player_stats) if player_stats else False
        duration = int((match.completed_at - match.started_at).total_seconds()) if match.started_at and match.completed_at else None
        return MatchStateResponse(
            match_id=match.id, status=match.status, phase="finished", server_time=now,
            question_count=match.question_count,
            final_result=FinalResultOut(winner_id=winner_id, is_draw=is_draw, duration_sec=duration, stats=_scoreboard(db, match)),
            scoreboard=_scoreboard(db, match),
        )

    if match.status != "in_progress":
        # waiting_room/countdown handled above/by lobby endpoints; anything
        # else (cancelled/abandoned/expired) has no gameplay state.
        return MatchStateResponse(
            match_id=match.id, status=match.status, phase=match.status, server_time=now,
            question_count=match.question_count, scoreboard=_scoreboard(db, match),
        )

    mq = get_current_match_question(db, match)
    if mq is None:
        return MatchStateResponse(
            match_id=match.id, status=match.status, phase="in_progress", server_time=now,
            question_count=match.question_count, scoreboard=_scoreboard(db, match),
        )

    question = db.get(Question, mq.question_id)
    choices = sorted(
        db.exec(select(QuestionChoice).where(QuestionChoice.question_id == mq.question_id)).all(),
        key=lambda c: c.label,
    )

    if mq.revealed_at is not None:
        correct_ids = [c.id for c in choices if c.is_correct]
        attempts = {a.user_id: a for a in _attempts_for(db, mq.id)}
        my_attempt = attempts.get(viewer_id)
        other_attempt = next((a for uid, a in attempts.items() if uid != viewer_id), None)
        return MatchStateResponse(
            match_id=match.id, status=match.status, phase="reveal",
            phase_ends_at=mq.revealed_at + timedelta(seconds=settings_row.competitive_transition_time_seconds),
            server_time=now, question_count=match.question_count, current_position=mq.position,
            reveal=RevealOut(
                match_question_id=mq.id, correct_choice_ids=correct_ids,
                my_selected_choice_ids=list(my_attempt.selected_choice_ids) if my_attempt else [],
                opponent_selected_choice_ids=list(other_attempt.selected_choice_ids) if other_attempt else [],
                my_is_correct=bool(my_attempt.is_correct) if my_attempt else False,
                my_points_earned=my_attempt.points_earned if my_attempt else 0,
                my_response_time_ms=my_attempt.response_time_ms if my_attempt else None,
                explanation=question.explanation if question else "",
            ),
            scoreboard=_scoreboard(db, match),
        )

    current_question_out = CurrentQuestionOut(
        match_question_id=mq.id, position=mq.position,
        statement=question.statement if question else "",
        is_multi_choice=False,
        choices=[ChoiceOut(id=c.id, label=c.label, text=c.text) for c in choices],
    )
    my_attempt = db.exec(
        select(CompetitiveQuestionAttempt)
        .where(CompetitiveQuestionAttempt.match_question_id == mq.id)
        .where(CompetitiveQuestionAttempt.user_id == viewer_id)
    ).first()
    opponent_answered = _both_answered_partial(db, match, mq, exclude=viewer_id)

    phase = "reading" if (mq.reading_ends_at and now < mq.reading_ends_at) else "answering"
    phase_ends_at = mq.reading_ends_at if phase == "reading" else mq.answer_ends_at

    return MatchStateResponse(
        match_id=match.id, status=match.status, phase=phase, phase_ends_at=phase_ends_at,
        server_time=now, question_count=match.question_count, current_position=mq.position,
        current_question=current_question_out,
        my_selected_choice_ids=list(my_attempt.selected_choice_ids) if my_attempt else None,
        opponent_has_answered=opponent_answered,
        scoreboard=_scoreboard(db, match),
    )


def _both_answered_partial(db: Session, match: CompetitiveMatch, mq: CompetitiveMatchQuestion, *, exclude: UUID) -> bool:
    attempts = _attempts_for(db, mq.id)
    return any(a.user_id != exclude for a in attempts)


# ─── Phase 8 — Spectator state computation ─────────────────────────────────

def compute_spectator_state(db: Session, match: CompetitiveMatch, *, spectator_count: int = 0) -> SpectatorStateResponse:
    """Mirrors compute_state() closely but is VIEWER-AGNOSTIC — every
    connected spectator gets the exact same payload (unlike MatchStateResponse,
    which is computed per-viewer for the two players). The critical
    invariant this function must preserve: no path may leak a player's
    pre-reveal selection or correctness, or even whether they've answered
    yet ("Hidden Information... Player selected answer" must never leak
    pre-reveal per the Phase 8 spec) — so, unlike compute_state(), this
    never populates anything resembling my_selected_choice_ids/
    opponent_has_answered at all; only once mq.revealed_at is set (the
    `reveal`/`finished` phases) does per-player detail appear, via
    SpectatorRevealOut / FinalResultOut, both of which are exactly as safe
    for players themselves to see at that point."""
    settings_row = _settings(db)
    now = _now()

    if match.status == "countdown":
        deadline = match.updated_at + timedelta(seconds=settings_row.competitive_match_countdown_seconds)
        if now >= deadline:
            start_match(db, match)
        else:
            return SpectatorStateResponse(
                match_id=match.id, status=match.status, phase="countdown", phase_ends_at=deadline,
                server_time=now, question_count=match.question_count,
                scoreboard=_scoreboard(db, match), spectator_count=spectator_count,
            )

    ensure_progressed(db, match)

    if match.status == "completed":
        player_stats = list(
            db.exec(select(CompetitiveMatchPlayerStats).where(CompetitiveMatchPlayerStats.match_id == match.id)).all()
        )
        winner_id = next((s.user_id for s in player_stats if s.result == "win"), None)
        is_draw = all(s.result == "draw" for s in player_stats) if player_stats else False
        duration = int((match.completed_at - match.started_at).total_seconds()) if match.started_at and match.completed_at else None
        return SpectatorStateResponse(
            match_id=match.id, status=match.status, phase="finished", server_time=now,
            question_count=match.question_count,
            final_result=FinalResultOut(winner_id=winner_id, is_draw=is_draw, duration_sec=duration, stats=_scoreboard(db, match)),
            scoreboard=_scoreboard(db, match), spectator_count=spectator_count,
        )

    if match.status != "in_progress":
        return SpectatorStateResponse(
            match_id=match.id, status=match.status, phase=match.status, server_time=now,
            question_count=match.question_count, scoreboard=_scoreboard(db, match), spectator_count=spectator_count,
        )

    mq = get_current_match_question(db, match)
    if mq is None:
        return SpectatorStateResponse(
            match_id=match.id, status=match.status, phase="in_progress", server_time=now,
            question_count=match.question_count, scoreboard=_scoreboard(db, match), spectator_count=spectator_count,
        )

    question = db.get(Question, mq.question_id)
    choices = sorted(
        db.exec(select(QuestionChoice).where(QuestionChoice.question_id == mq.question_id)).all(),
        key=lambda c: c.label,
    )

    if mq.revealed_at is not None:
        correct_ids = [c.id for c in choices if c.is_correct]
        attempts = {a.user_id: a for a in _attempts_for(db, mq.id)}
        participants = match_service.get_participants(db, match.id)
        # Stable player_a/player_b assignment across every tick: player_a is
        # always the match creator, never re-derived from attempt order.
        player_a_id = match.creator_id
        other = next((p.user_id for p in participants if p.user_id != player_a_id), None)
        a_attempt = attempts.get(player_a_id)
        b_attempt = attempts.get(other) if other else None
        return SpectatorStateResponse(
            match_id=match.id, status=match.status, phase="reveal",
            phase_ends_at=mq.revealed_at + timedelta(seconds=settings_row.competitive_transition_time_seconds),
            server_time=now, question_count=match.question_count, current_position=mq.position,
            reveal=SpectatorRevealOut(
                match_question_id=mq.id, correct_choice_ids=correct_ids,
                player_a_id=player_a_id,
                player_a_selected_choice_ids=list(a_attempt.selected_choice_ids) if a_attempt else [],
                player_a_is_correct=bool(a_attempt.is_correct) if a_attempt else False,
                player_b_id=other,
                player_b_selected_choice_ids=list(b_attempt.selected_choice_ids) if b_attempt else [],
                player_b_is_correct=bool(b_attempt.is_correct) if b_attempt else False,
                explanation=question.explanation if question else "",
            ),
            scoreboard=_scoreboard(db, match), spectator_count=spectator_count,
        )

    current_question_out = CurrentQuestionOut(
        match_question_id=mq.id, position=mq.position,
        statement=question.statement if question else "",
        is_multi_choice=False,
        choices=[ChoiceOut(id=c.id, label=c.label, text=c.text) for c in choices],
    )
    phase = "reading" if (mq.reading_ends_at and now < mq.reading_ends_at) else "answering"
    phase_ends_at = mq.reading_ends_at if phase == "reading" else mq.answer_ends_at

    return SpectatorStateResponse(
        match_id=match.id, status=match.status, phase=phase, phase_ends_at=phase_ends_at,
        server_time=now, question_count=match.question_count, current_position=mq.position,
        current_question=current_question_out,
        scoreboard=_scoreboard(db, match), spectator_count=spectator_count,
    )


# ─── Phase 10, Part B — Battle Royale live state computation ──────────────

#: Top-N cap for the live leaderboard push — full standings are a separate
#: concern (REST endpoint / replay leaderboard-evolution, Part C), not this
#: live per-tick WS payload.
_BR_LEADERBOARD_TOP_N = 20


def _battle_royale_leaderboard(
    db: Session, match: CompetitiveMatch, *, top_n: int = _BR_LEADERBOARD_TOP_N,
) -> List["BattleRoyaleLeaderboardEntryOut"]:
    from app.services.competitive import battle_royale_service

    participants = match_service.get_participants(db, match.id)
    if not participants:
        return []
    profiles = {
        pr.id: pr for pr in db.exec(select(Profile).where(Profile.id.in_({p.user_id for p in participants}))).all()
    }
    stats = battle_royale_service.bulk_attempt_stats(db, match.id)
    ranked = sorted(participants, key=lambda p: battle_royale_service.rank_key(p, stats))

    out: List[BattleRoyaleLeaderboardEntryOut] = []
    for idx, p in enumerate(ranked[:top_n], start=1):
        profile = profiles.get(p.user_id)
        out.append(BattleRoyaleLeaderboardEntryOut(
            rank=idx, user_id=p.user_id,
            full_name=profile.full_name if profile else None,
            avatar_url=profile.avatar_url if profile else None,
            score=p.score, is_eliminated=p.elimination_round is not None,
        ))
    return out


def compute_battle_royale_state(db: Session, match: CompetitiveMatch, *, viewer_id: UUID) -> BattleRoyaleStateResponse:
    """The Battle Royale counterpart of compute_state() — computed per-
    viewer (own answer visible pre-reveal, mirrors compute_state's own-
    visibility principle) but N-player-shaped: a capped leaderboard instead
    of a 2-entry scoreboard, my_status/my_rank instead of an implicit
    winner/loser, and a reveal that never leaks every other player's pick
    (see BattleRoyaleRevealOut's docstring)."""
    from app.models.competitive import CompetitiveBattleRoyaleMatch

    settings_row = _settings(db)
    now = _now()
    br = db.get(CompetitiveBattleRoyaleMatch, match.id)
    current_phase = br.current_phase if br else "waiting_room"
    remaining_players_count = br.remaining_players_count if br else None

    if match.status == "countdown":
        deadline = match.updated_at + timedelta(seconds=settings_row.competitive_match_countdown_seconds)
        if now >= deadline:
            # Self-heal: same idiom as compute_state — never leave a match
            # permanently stuck once its countdown has genuinely elapsed.
            start_match(db, match)
            if br is not None:
                db.refresh(br)
                current_phase = br.current_phase
                remaining_players_count = br.remaining_players_count
        else:
            return BattleRoyaleStateResponse(
                match_id=match.id, status=match.status, current_phase=current_phase, phase="countdown",
                phase_ends_at=deadline, server_time=now, question_count=match.question_count,
                remaining_players_count=remaining_players_count,
            )

    ensure_progressed(db, match)
    if br is not None:
        db.refresh(br)
        current_phase = br.current_phase
        remaining_players_count = br.remaining_players_count

    viewer_participant = db.exec(
        select(CompetitiveMatchParticipant)
        .where(CompetitiveMatchParticipant.match_id == match.id)
        .where(CompetitiveMatchParticipant.user_id == viewer_id)
    ).first()
    my_status = "active"
    my_rank = viewer_participant.final_rank if viewer_participant else None
    my_score = viewer_participant.score if viewer_participant else 0
    if viewer_participant is not None:
        if viewer_participant.elimination_round is not None:
            my_status = "eliminated"
        elif match.status == "completed" and viewer_participant.final_rank == 1:
            my_status = "winner"

    leaderboard = _battle_royale_leaderboard(db, match)

    if match.status == "completed":
        player_stats = list(
            db.exec(select(CompetitiveMatchPlayerStats).where(CompetitiveMatchPlayerStats.match_id == match.id)).all()
        )
        winner_id = next((s.user_id for s in player_stats if s.result == "win"), None)
        is_draw = all(s.result == "draw" for s in player_stats) if player_stats else False
        duration = int((match.completed_at - match.started_at).total_seconds()) if match.started_at and match.completed_at else None
        return BattleRoyaleStateResponse(
            match_id=match.id, status=match.status, current_phase=current_phase, phase="finished",
            server_time=now, question_count=match.question_count, my_status=my_status, my_rank=my_rank,
            my_score=my_score, remaining_players_count=remaining_players_count, leaderboard=leaderboard,
            final_result=FinalResultOut(winner_id=winner_id, is_draw=is_draw, duration_sec=duration, stats=_scoreboard(db, match)),
        )

    if match.status != "in_progress":
        return BattleRoyaleStateResponse(
            match_id=match.id, status=match.status, current_phase=current_phase, phase=match.status,
            server_time=now, question_count=match.question_count, my_status=my_status, my_rank=my_rank,
            my_score=my_score, remaining_players_count=remaining_players_count, leaderboard=leaderboard,
        )

    mq = get_current_match_question(db, match)
    if mq is None:
        return BattleRoyaleStateResponse(
            match_id=match.id, status=match.status, current_phase=current_phase, phase="in_progress",
            server_time=now, question_count=match.question_count, my_status=my_status, my_rank=my_rank,
            my_score=my_score, remaining_players_count=remaining_players_count, leaderboard=leaderboard,
        )

    question = db.get(Question, mq.question_id)
    choices = sorted(
        db.exec(select(QuestionChoice).where(QuestionChoice.question_id == mq.question_id)).all(),
        key=lambda c: c.label,
    )

    if mq.revealed_at is not None:
        correct_ids = [c.id for c in choices if c.is_correct]
        my_attempt = db.exec(
            select(CompetitiveQuestionAttempt)
            .where(CompetitiveQuestionAttempt.match_question_id == mq.id)
            .where(CompetitiveQuestionAttempt.user_id == viewer_id)
        ).first()
        return BattleRoyaleStateResponse(
            match_id=match.id, status=match.status, current_phase=current_phase, phase="reveal",
            phase_ends_at=mq.revealed_at + timedelta(seconds=settings_row.competitive_transition_time_seconds),
            server_time=now, question_count=match.question_count, current_position=mq.position,
            my_status=my_status, my_rank=my_rank, my_score=my_score,
            remaining_players_count=remaining_players_count, leaderboard=leaderboard,
            reveal=BattleRoyaleRevealOut(
                match_question_id=mq.id, correct_choice_ids=correct_ids,
                my_selected_choice_ids=list(my_attempt.selected_choice_ids) if my_attempt else [],
                my_is_correct=bool(my_attempt.is_correct) if my_attempt else False,
                my_points_earned=my_attempt.points_earned if my_attempt else 0,
                my_response_time_ms=my_attempt.response_time_ms if my_attempt else None,
                explanation=question.explanation if question else "",
            ),
        )

    current_question_out = CurrentQuestionOut(
        match_question_id=mq.id, position=mq.position,
        statement=question.statement if question else "",
        is_multi_choice=False,
        choices=[ChoiceOut(id=c.id, label=c.label, text=c.text) for c in choices],
    )
    my_attempt = db.exec(
        select(CompetitiveQuestionAttempt)
        .where(CompetitiveQuestionAttempt.match_question_id == mq.id)
        .where(CompetitiveQuestionAttempt.user_id == viewer_id)
    ).first()
    phase = "reading" if (mq.reading_ends_at and now < mq.reading_ends_at) else "answering"
    phase_ends_at = mq.reading_ends_at if phase == "reading" else mq.answer_ends_at

    return BattleRoyaleStateResponse(
        match_id=match.id, status=match.status, current_phase=current_phase, phase=phase,
        phase_ends_at=phase_ends_at, server_time=now, question_count=match.question_count,
        current_position=mq.position, current_question=current_question_out, my_status=my_status,
        my_selected_choice_ids=list(my_attempt.selected_choice_ids) if my_attempt else None,
        my_rank=my_rank, my_score=my_score, remaining_players_count=remaining_players_count,
        leaderboard=leaderboard,
    )
