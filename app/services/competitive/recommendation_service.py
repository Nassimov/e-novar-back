from __future__ import annotations

"""
Recommendation & Roadmap Engine — Competitive Arena Phase 4.

Persisted (not ephemeral) student-level recommendations: every new match
supersedes (never deletes) the student's previous active recommendations —
"Only show active recommendations. Automatically replace obsolete ones."

Teacher recommendations reuse app/services/recommendation.py's
rank_teachers() verbatim (already implements the budget/language/rating/
format/availability scoring this phase's spec asks for) — nothing here
duplicates that logic.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlmodel import Session, select

from app.models.catalog import Subject
from app.models.competitive import (
    CompetitiveLearningRoadmapStep,
    CompetitiveMatch,
    CompetitiveRecommendation,
)
from app.services import recommendation as teacher_recommendation
from app.services.notification_engine import emit


def _supersede_active(db: Session, student_id: UUID, category: Optional[str] = None) -> None:
    query = select(CompetitiveRecommendation).where(
        CompetitiveRecommendation.student_id == student_id,
        CompetitiveRecommendation.status == "active",
    )
    if category:
        query = query.where(CompetitiveRecommendation.category == category)
    for rec in db.exec(query).all():
        rec.status = "obsolete"
        rec.superseded_at = datetime.now(timezone.utc)
        db.add(rec)


def _weakest_subject(subject_breakdown: Dict[str, Any]) -> Optional[str]:
    eligible = {sid: s for sid, s in subject_breakdown.items() if s["attempts"] >= 2}
    if not eligible:
        return None
    return min(eligible, key=lambda sid: eligible[sid]["accuracy_pct"])


#: Phase 12 — spec: "Maximum: 3 recommendations per match. Prioritize the
#: highest-impact improvements." Candidates are built in priority order
#: (weakest-subject practice > teacher session > generic teacher match >
#: generic challenge) and truncated — never randomly, never all 4 at once.
_MAX_RECOMMENDATIONS = 3


def generate_recommendations(
    db: Session, match: CompetitiveMatch, user_id: UUID, *,
    weaknesses: List[str], subject_breakdown: Dict[str, Any],
) -> List[CompetitiveRecommendation]:
    _supersede_active(db, user_id)
    db.commit()

    subject_names = {
        str(s.id): s.name for s in db.exec(select(Subject)).all()
    } if subject_breakdown else {}

    recs: List[CompetitiveRecommendation] = []
    weakest_subject_id = _weakest_subject(subject_breakdown)

    if weakest_subject_id:
        name = subject_names.get(weakest_subject_id, "cette matière")
        recs.append(CompetitiveRecommendation(
            student_id=user_id, source_match_id=match.id, category="arena",
            title=f"Rejoue un duel en {name}",
            description=f"Ta précision en {name} peut encore progresser — un nouveau duel ciblé t'aidera à consolider.",
            confidence="high" if subject_breakdown[weakest_subject_id]["attempts"] >= 5 else "medium",
            action_type="practice_arena",
            action_payload={"subject_id": weakest_subject_id},
        ))

    if weaknesses:
        recs.append(CompetitiveRecommendation(
            student_id=user_id, source_match_id=match.id, category="session",
            title="Réserve une séance ciblée",
            description="Un enseignant peut t'aider à travailler précisément les points identifiés dans ce match.",
            confidence="medium",
            action_type="book_session",
            action_payload={},
        ))

    try:
        teachers = teacher_recommendation.rank_teachers(db, user_id, limit=3)
    except Exception:
        teachers = []
    teacher_rec: Optional[CompetitiveRecommendation] = None
    if teachers:
        teacher_rec = CompetitiveRecommendation(
            student_id=user_id, source_match_id=match.id, category="teacher",
            title="Un enseignant pour toi",
            description="Ces enseignants correspondent bien à ton profil et à tes besoins actuels.",
            confidence="medium",
            action_type="view_teacher",
            action_payload={"teacher_ids": [str(t.user_id) for t in teachers]},
        )
        recs.append(teacher_rec)

    recs.append(CompetitiveRecommendation(
        student_id=user_id, source_match_id=match.id, category="challenge",
        title="Tente un nouveau défi",
        description="Défie un autre élève pour continuer à progresser en t'amusant.",
        confidence="low",
        action_type="practice_arena",
        action_payload={},
    ))

    # Cap at _MAX_RECOMMENDATIONS — recs was built in priority order
    # (weakest-subject practice > targeted session > teacher match >
    # generic challenge), so a plain truncation already keeps the
    # highest-impact ones.
    recs = recs[:_MAX_RECOMMENDATIONS]

    for rec in recs:
        db.add(rec)
    db.commit()

    for rec in recs:
        db.refresh(rec)
        emit(
            db, event_type="competitive_new_recommendation", user_id=user_id,
            context={"title": rec.title},
            data={"recommendation_id": str(rec.id)},
            dedup_key=f"competitive_new_recommendation:{rec.id}",
        )
        if rec is teacher_rec:
            emit(
                db, event_type="competitive_teacher_recommended", user_id=user_id,
                dedup_key=f"competitive_teacher_recommended:{match.id}",
            )
    return recs


def generate_roadmap(
    db: Session, match: CompetitiveMatch, user_id: UUID, *,
    weaknesses: List[str], weakest_subject_name: Optional[str],
) -> List[CompetitiveLearningRoadmapStep]:
    # Supersede (mark done→keep, pending→drop) previous pending steps —
    # "Roadmap updates after every match."
    old_pending = db.exec(
        select(CompetitiveLearningRoadmapStep)
        .where(CompetitiveLearningRoadmapStep.student_id == user_id)
        .where(CompetitiveLearningRoadmapStep.status == "pending")
    ).all()
    for step in old_pending:
        db.delete(step)
    db.commit()

    steps: List[CompetitiveLearningRoadmapStep] = []
    position = 1
    if weakest_subject_name:
        steps.append(CompetitiveLearningRoadmapStep(
            student_id=user_id, source_match_id=match.id, position=position,
            title=f"Revoir {weakest_subject_name}",
            description="Reprends les bases avant ton prochain duel.",
        ))
        position += 1
    steps.append(CompetitiveLearningRoadmapStep(
        student_id=user_id, source_match_id=match.id, position=position,
        title="Rejouer un duel compétitif",
        description="Applique ce que tu viens de revoir dans un nouveau match.",
    ))
    position += 1
    if weaknesses:
        steps.append(CompetitiveLearningRoadmapStep(
            student_id=user_id, source_match_id=match.id, position=position,
            title="Réserver une séance avec un enseignant",
            description="Pour consolider durablement tes points faibles.",
        ))
        position += 1
    steps.append(CompetitiveLearningRoadmapStep(
        student_id=user_id, source_match_id=match.id, position=position,
        title="Retenter l'Arène Compétitive",
        description="Mesure ta progression sur un nouveau duel.",
    ))

    for step in steps:
        db.add(step)
    db.commit()
    for step in steps:
        db.refresh(step)
    return steps
