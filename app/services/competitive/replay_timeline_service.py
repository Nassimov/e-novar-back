from __future__ import annotations

"""
Replay Timeline Builder — Competitive Arena Phase 12.

Builds the chronological, per-event "Replay Timeline Panel" the spec calls
for (spec examples: "00:00 Match Started", "00:15 Question 1", "00:28
Player A answered"...). Deliberately NOT a new stored-events table —
competitive_match_events (Phase 2's event_log_service, already logged from
every phase: match_started/question_started/question_revealed/
answer_submitted/battle_royale_eliminated/tournament progression/club
battle events/achievement grants/predictions...) is already exactly the
rich, timestamped, append-only log the spec describes ("store structured
events... do NOT store video"). This service only MERGES it with the one
source that genuinely isn't event-logged today (competitive_match_reactions
— spectator emoji reactions), sorts everything chronologically, and attaches
a human (French) label + light enrichment so the frontend never has to
interpret raw event_type/meta itself.

Works for every match_type unmodified — event_log_service.log_event is
already called from every phase's service module with the same (match_id,
actor_id, event_type, meta, created_at) shape, so no match_type branching
is needed here at all.
"""

from typing import Any, Dict, List
from uuid import UUID

from sqlmodel import Session, select

from app.models.competitive import (
    CompetitiveMatch,
    CompetitiveMatchQuestion,
    CompetitiveMatchReaction,
    CompetitiveQuestionAttempt,
)
from app.models.profile import Profile
from app.services.competitive import event_log_service

#: event_type -> French label template. {actor} is substituted with the
#: actor's full name (or "Un joueur" if unresolved); unmapped event types
#: fall back to a readable version of the raw event_type (see _fallback_label).
_LABELS: Dict[str, str] = {
    "match_started": "Le match a commencé",
    "question_started": "Nouvelle question",
    "question_revealed": "Réponse correcte révélée",
    "answer_submitted": "{actor} a répondu",
    "match_forfeited_disconnect": "{actor} a été déconnecté(e) — forfait",
    "match_left_voluntarily": "{actor} a quitté le match",
    "battle_royale_eliminated": "{actor} a été éliminé(e)",
    "battle_royale_created": "Battle Royale créé",
    "_gameplay_started": "La partie commence",
    "_final_phase_started": "Phase finale !",
    "_final_duel_started": "Duel final !",
    "_completed": "Battle Royale terminé",
    "club_battle_created": "Bataille de club créée",
    "tournament_match_duel_created": "Match de bracket lancé",
    "tournament_match_duel_resolved": "Match de bracket résolu",
    "tournament_round_started": "Nouveau round",
    "tournament_round_completed": "Round terminé",
    "prediction_made": "{actor} a fait un pronostic",
    "spectator_join": "{actor} regarde le match",
    "reaction": "{actor} a réagi",
    "achievement_perfect_match": "{actor} a débloqué : Match parfait",
    "achievement_win_streak": "{actor} a débloqué : Série de victoires",
    "competitive_replay_ready": "Replay prêt",
    "competitive_ai_analysis_ready": "Analyse IA prête",
}


def _fallback_label(event_type: str) -> str:
    return event_type.replace("_", " ").capitalize()


def _label_for(event_type: str, actor_name: str) -> str:
    template = _LABELS.get(event_type)
    if template is None:
        return _fallback_label(event_type)
    return template.format(actor=actor_name)


def build_timeline(db: Session, match: CompetitiveMatch) -> List[Dict[str, Any]]:
    raw_events = list(reversed(event_log_service.list_events(db, match.id)))  # list_events is DESC; we want chronological

    actor_ids = {e.actor_id for e in raw_events if e.actor_id is not None}
    reactions = list(db.exec(select(CompetitiveMatchReaction).where(CompetitiveMatchReaction.match_id == match.id)).all())
    actor_ids |= {r.user_id for r in reactions}
    profiles = {p.id: p for p in db.exec(select(Profile).where(Profile.id.in_(actor_ids))).all()} if actor_ids else {}

    # Enrichment lookups for answer_submitted (needs is_correct/points_earned,
    # not stored on the event itself — only response_time_ms is).
    attempts_by_key: Dict[tuple, CompetitiveQuestionAttempt] = {}
    mq_by_position: Dict[int, UUID] = {}
    needs_attempt_lookup = any(e.event_type == "answer_submitted" for e in raw_events)
    if needs_attempt_lookup:
        mqs = db.exec(select(CompetitiveMatchQuestion).where(CompetitiveMatchQuestion.match_id == match.id)).all()
        mq_by_position = {mq.position: mq.id for mq in mqs}
        attempts = db.exec(
            select(CompetitiveQuestionAttempt)
            .join(CompetitiveMatchQuestion, CompetitiveMatchQuestion.id == CompetitiveQuestionAttempt.match_question_id)
            .where(CompetitiveMatchQuestion.match_id == match.id)
        ).all()
        attempts_by_key = {(a.match_question_id, a.user_id): a for a in attempts}

    timeline: List[Dict[str, Any]] = []
    for e in raw_events:
        actor = profiles.get(e.actor_id) if e.actor_id else None
        actor_name = actor.full_name if actor else "Un joueur"
        entry: Dict[str, Any] = {
            "id": str(e.id), "event_type": e.event_type, "label": _label_for(e.event_type, actor_name),
            "actor_id": str(e.actor_id) if e.actor_id else None, "actor_name": actor.full_name if actor else None,
            "at": e.created_at.isoformat(), "data": e.meta or {},
        }
        if e.event_type == "answer_submitted" and e.actor_id is not None:
            position = (e.meta or {}).get("position")
            mq_id = mq_by_position.get(position) if position is not None else None
            attempt = attempts_by_key.get((mq_id, e.actor_id)) if mq_id else None
            if attempt is not None:
                entry["data"] = {
                    **entry["data"], "is_correct": attempt.is_correct, "points_earned": attempt.points_earned,
                }
                entry["label"] = f"{actor_name} a répondu {'correctement' if attempt.is_correct else 'incorrectement'}"
        timeline.append(entry)

    for r in reactions:
        actor = profiles.get(r.user_id)
        timeline.append({
            "id": str(r.id), "event_type": "reaction", "label": _label_for("reaction", actor.full_name if actor else "Un joueur"),
            "actor_id": str(r.user_id), "actor_name": actor.full_name if actor else None,
            "at": r.created_at.isoformat(), "data": {"emoji": r.emoji},
        })

    timeline.sort(key=lambda x: x["at"])
    return timeline
