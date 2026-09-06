from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session, func, select

from app.dependencies import get_current_user, get_db
from app.models.booking import TutoringSession
from app.models.catalog import Subject
from app.models.evaluation import Evaluation
from app.models.homework import Homework, HomeworkGrade
from app.models.practice import QuizAttempt, StudentSubjectMastery
from app.models.progress import StudentMonthlySnapshot

router = APIRouter(tags=["Student"])

# ─── French month names ───────────────────────────────────────────────────────

_MONTHS_FR = {
    1: "Janvier", 2: "Février", 3: "Mars", 4: "Avril", 5: "Mai",
    6: "Juin", 7: "Juillet", 8: "Août", 9: "Septembre",
    10: "Octobre", 11: "Novembre", 12: "Décembre",
}

# ─── Response schemas ─────────────────────────────────────────────────────────

class SubjectProgress(BaseModel):
    subject_id: str
    subject_name: str
    score_before: float   # /20
    score_now: float      # /20
    delta: float
    trend: str            # "up" | "down" | "flat"

class RecommendationItem(BaseModel):
    title: str
    description: str
    action: str           # "session" | "practice" | "homework" | "continue"

class ProgressReportResponse(BaseModel):
    period_label: str
    global_score_label: str   # "+18%", "0%", "-5%"
    global_trend: str         # "up" | "down" | "flat"
    global_delta: float
    sessions_count: int
    homework_count: int
    subjects: List[SubjectProgress]
    strengths: List[str]
    weaknesses: List[str]
    recommendations: List[RecommendationItem]
    has_data: bool


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _month_first(d: date) -> date:
    return d.replace(day=1)


def _prev_month(d: date) -> date:
    first = d.replace(day=1)
    return (first - timedelta(days=1)).replace(day=1)


def _subject_mastery_pct(student_id: UUID, subject_id: UUID, db: Session) -> float:
    """
    Weighted mastery score 0–100 for a subject.
      60 % quiz/practice (StudentSubjectMastery.mastery_pct, already 0–100)
      30 % homework grades (HomeworkGrade.score, 0–20 → normalised to 0–100)
      10 % evaluation scores (Evaluation.score_global, 0–20 → normalised to 0–100)
    Weights rescaled when a source is missing.
    """
    # ── 1. Quiz mastery (0–100) ───────────────────────────────────────────────
    mastery_row = db.exec(
        select(StudentSubjectMastery).where(
            StudentSubjectMastery.student_id == student_id,
            StudentSubjectMastery.subject_id == subject_id,
        )
    ).first()
    quiz_pct: Optional[float] = (
        float(mastery_row.mastery_pct)
        if mastery_row and mastery_row.mastery_pct is not None
        else None
    )

    # ── 2. Homework avg (0–20 → 0–100) ───────────────────────────────────────
    hw_scores = db.exec(
        select(HomeworkGrade.score)
        .join(Homework, Homework.id == HomeworkGrade.homework_id)
        .where(
            Homework.student_id == student_id,
            Homework.subject_id == subject_id,
        )
    ).all()
    hw_pct: Optional[float] = (
        (sum(float(s) for s in hw_scores) / len(hw_scores)) / 20.0 * 100.0
        if hw_scores else None
    )

    # ── 3. Evaluation avg (0–20 → 0–100) ─────────────────────────────────────
    eval_scores = db.exec(
        select(Evaluation.score_global)
        .join(TutoringSession, TutoringSession.id == Evaluation.session_id)
        .where(
            Evaluation.student_id == student_id,
            TutoringSession.subject_id == subject_id,
            Evaluation.score_global.isnot(None),
        )
    ).all()
    eval_pct: Optional[float] = (
        (sum(float(s) for s in eval_scores) / len(eval_scores)) / 20.0 * 100.0
        if eval_scores else None
    )

    # ── Weighted combination (rescale if sources missing) ─────────────────────
    sources: list[tuple[float, float]] = []  # (value, weight)
    if quiz_pct is not None:
        sources.append((quiz_pct, 0.60))
    if hw_pct is not None:
        sources.append((hw_pct, 0.30))
    if eval_pct is not None:
        sources.append((eval_pct, 0.10))

    if not sources:
        return 0.0

    total_w = sum(w for _, w in sources)
    return round(sum(v * w for v, w in sources) / total_w, 1)


def _mastery_to_score20(pct: float) -> float:
    """Convert 0–100 mastery percentage to 0–20 score."""
    return round(pct / 5.0, 1)


def _get_active_subject_ids(student_id: UUID, db: Session) -> set[UUID]:
    """All subject UUIDs the student has been active in (quiz, homework, sessions)."""
    ids: set[UUID] = set()

    # Quiz/practice
    for sid in db.exec(
        select(StudentSubjectMastery.subject_id).where(
            StudentSubjectMastery.student_id == student_id
        )
    ).all():
        if sid:
            ids.add(sid)

    # Homework
    for sid in db.exec(
        select(Homework.subject_id).where(
            Homework.student_id == student_id,
            Homework.subject_id.isnot(None),
        ).distinct()
    ).all():
        if sid:
            ids.add(sid)

    # Completed sessions
    for sid in db.exec(
        select(TutoringSession.subject_id).where(
            TutoringSession.student_id == student_id,
            TutoringSession.subject_id.isnot(None),
            TutoringSession.status == "completed",
        ).distinct()
    ).all():
        if sid:
            ids.add(sid)

    return ids


def _generate_strengths(
    sessions_month: int,
    hw_submitted: int,
    quiz_count: int,
    subjects: list[SubjectProgress],
) -> list[str]:
    items: list[str] = []

    if sessions_month >= 5:
        items.append(f"Régularité exceptionnelle ({sessions_month} séances ce mois)")
    elif sessions_month >= 3:
        items.append(f"Bonne régularité ({sessions_month} séances ce mois)")
    elif sessions_month == 1:
        items.append("Présence active (1 séance ce mois)")
    elif sessions_month == 2:
        items.append("Présence active (2 séances ce mois)")

    if hw_submitted >= 5:
        items.append("Assidu dans les devoirs — excellent suivi")
    elif hw_submitted >= 3:
        items.append("Bonne régularité dans les devoirs")

    if quiz_count >= 10:
        items.append(f"Pratique intensive ({quiz_count} quiz réalisés)")
    elif quiz_count >= 5:
        items.append(f"Bonne pratique avec les quiz ({quiz_count} réalisés)")

    strong = [s for s in subjects if s.score_now >= 14.0]
    if strong:
        best = max(strong, key=lambda s: s.score_now)
        items.append(f"Bonne maîtrise des concepts en {best.subject_name}")

    improved = [s for s in subjects if s.delta >= 2.0]
    if improved:
        best_imp = max(improved, key=lambda s: s.delta)
        items.append(f"Forte progression en {best_imp.subject_name} (+{best_imp.delta:.1f} pts)")

    return items[:4]


def _generate_weaknesses(subjects: list[SubjectProgress]) -> list[str]:
    items: list[str] = []

    weak = [s for s in subjects if 0 < s.score_now < 10.0]
    if weak:
        worst = min(weak, key=lambda s: s.score_now)
        items.append(f"Bases à renforcer en {worst.subject_name} ({worst.score_now:.1f}/20)")

    declined = [s for s in subjects if s.trend == "down" and abs(s.delta) >= 1.0]
    if declined:
        most = min(declined, key=lambda s: s.delta)
        items.append(f"Régression en {most.subject_name} ({most.delta:.1f} pts)")

    if len(weak) > 1:
        sorted_weak = sorted(weak, key=lambda s: s.score_now)
        second = sorted_weak[1]
        if second.subject_name not in " ".join(items):
            items.append(f"À approfondir : {second.subject_name}")

    if not items and subjects:
        flat = [s for s in subjects if s.trend == "flat"]
        if flat:
            items.append(f"Peu de progression en {flat[0].subject_name} ce mois")

    return items[:3]


def _generate_recommendations(
    subjects: list[SubjectProgress],
    sessions_month: int,
    hw_submitted: int,
    quiz_count: int,
    has_data: bool,
) -> list[RecommendationItem]:
    recs: list[RecommendationItem] = []

    if not has_data:
        return [
            RecommendationItem(
                title="Réserve ta première séance",
                description="Commence ton parcours avec un enseignant pour démarrer ton suivi.",
                action="session",
            ),
            RecommendationItem(
                title="Lance-toi avec le Mode Practice",
                description="Teste tes connaissances et découvre tes points forts.",
                action="practice",
            ),
        ]

    # 1. Weakest subject → session recommendation
    weak = [s for s in subjects if 0 < s.score_now < 10.0]
    if weak:
        worst = min(weak, key=lambda s: s.score_now)
        recs.append(RecommendationItem(
            title=f"Réserve une séance de {worst.subject_name} ciblée",
            description="Pour travailler les points faibles avec un enseignant spécialisé.",
            action="session",
        ))

    # 2. Low quiz activity
    if quiz_count == 0:
        recs.append(RecommendationItem(
            title="Lance-toi avec le Mode Practice",
            description="Entraîne-toi avec des quiz adaptatifs pour booster ta progression.",
            action="practice",
        ))
    elif quiz_count < 3:
        recs.append(RecommendationItem(
            title="Pratique plus régulièrement",
            description="Vise au moins 3 quiz par semaine pour des progrès durables.",
            action="practice",
        ))

    # 3. No homework submitted
    if hw_submitted == 0:
        recs.append(RecommendationItem(
            title="Rends tes devoirs régulièrement",
            description="Le suivi de tes devoirs améliore significativement ta progression globale.",
            action="homework",
        ))

    # 4. Best improving subject → encourage
    strong_imp = [s for s in subjects if s.delta >= 2.0]
    if strong_imp:
        best = max(strong_imp, key=lambda s: s.delta)
        recs.append(RecommendationItem(
            title=f"Continue ta progression en {best.subject_name}",
            description=f"Tu es en excellente voie avec +{best.delta:.1f} pts — garde ce rythme !",
            action="continue",
        ))

    # 5. No session this month
    if sessions_month == 0 and not any(r.action == "session" for r in recs):
        recs.append(RecommendationItem(
            title="Reprends tes séances ce mois",
            description="La régularité est la clé d'une progression durable.",
            action="session",
        ))

    return recs[:3]


# ─── Endpoint ─────────────────────────────────────────────────────────────────

@router.get("/progress", response_model=ProgressReportResponse)
def get_student_progress(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])
    now = datetime.now(timezone.utc)
    this_month = _month_first(now.date())
    last_month = _prev_month(this_month)
    month_start_dt = datetime(this_month.year, this_month.month, 1)

    # ── Aggregate counts ──────────────────────────────────────────────────────
    sessions_month = int(db.exec(
        select(func.count(TutoringSession.id)).where(
            TutoringSession.student_id == uid,
            TutoringSession.status == "completed",
            TutoringSession.scheduled_at >= month_start_dt,
        )
    ).one() or 0)

    sessions_total = int(db.exec(
        select(func.count(TutoringSession.id)).where(
            TutoringSession.student_id == uid,
            TutoringSession.status == "completed",
        )
    ).one() or 0)

    hw_submitted = int(db.exec(
        select(func.count(Homework.id)).where(
            Homework.student_id == uid,
            Homework.status.in_(["submitted", "graded"]),
        )
    ).one() or 0)

    hw_graded = int(db.exec(
        select(func.count(Homework.id)).where(
            Homework.student_id == uid,
            Homework.status == "graded",
        )
    ).one() or 0)

    quiz_count = int(db.exec(
        select(func.count(QuizAttempt.id)).where(
            QuizAttempt.student_id == uid,
            QuizAttempt.completed == True,  # noqa: E712
        )
    ).one() or 0)

    ep_month = int(db.exec(
        select(func.coalesce(func.sum(QuizAttempt.ep_earned), 0)).where(
            QuizAttempt.student_id == uid,
            QuizAttempt.completed == True,  # noqa: E712
            QuizAttempt.completed_at.isnot(None),
            QuizAttempt.completed_at >= month_start_dt,
        )
    ).one() or 0)

    has_data = (sessions_total + hw_submitted + quiz_count) > 0

    # ── Subject mastery ───────────────────────────────────────────────────────
    active_ids = _get_active_subject_ids(uid, db)
    subjects_meta: list[tuple[UUID, str]] = []
    if active_ids:
        rows = db.exec(select(Subject).where(Subject.id.in_(list(active_ids)))).all()
        subjects_meta = [(s.id, s.name) for s in rows]

    # Load last month's snapshot for baseline comparison
    last_snap = db.exec(
        select(StudentMonthlySnapshot).where(
            StudentMonthlySnapshot.student_id == uid,
            StudentMonthlySnapshot.snapshot_month == last_month,
        )
    ).first()

    last_mastery: dict[str, float] = {}
    if last_snap and last_snap.subject_mastery:
        for entry in (last_snap.subject_mastery or []):
            if isinstance(entry, dict):
                last_mastery[entry.get("subject_id", "")] = float(entry.get("mastery_pct", 0.0))

    subject_progresses: list[SubjectProgress] = []
    current_mastery_snapshot: list[dict] = []

    for subj_id, subj_name in subjects_meta:
        pct_now = _subject_mastery_pct(uid, subj_id, db)
        score_now = _mastery_to_score20(pct_now)
        pct_before = last_mastery.get(str(subj_id), 0.0)
        score_before = _mastery_to_score20(pct_before)

        delta = round(score_now - score_before, 1)
        trend = "up" if delta > 0.3 else ("down" if delta < -0.3 else "flat")

        subject_progresses.append(SubjectProgress(
            subject_id=str(subj_id),
            subject_name=subj_name,
            score_before=score_before,
            score_now=score_now,
            delta=delta,
            trend=trend,
        ))
        current_mastery_snapshot.append({
            "subject_id": str(subj_id),
            "subject_name": subj_name,
            "mastery_pct": pct_now,
        })

    subject_progresses.sort(key=lambda s: s.score_now, reverse=True)

    # ── Global score ──────────────────────────────────────────────────────────
    if subject_progresses:
        avg_now = sum(s.score_now for s in subject_progresses) / len(subject_progresses)
        avg_before = sum(s.score_before for s in subject_progresses) / len(subject_progresses)
        if avg_before > 0:
            global_delta = round(((avg_now - avg_before) / avg_before) * 100, 1)
        else:
            global_delta = 0.0
    else:
        global_delta = 0.0

    if global_delta > 0.5:
        global_trend = "up"
        global_label = f"+{global_delta:.0f}%"
    elif global_delta < -0.5:
        global_trend = "down"
        global_label = f"{global_delta:.0f}%"
    else:
        global_trend = "flat"
        global_label = "0%"

    # ── Strengths / Weaknesses / Recommendations ──────────────────────────────
    strengths = _generate_strengths(sessions_month, hw_submitted, quiz_count, subject_progresses)
    weaknesses = _generate_weaknesses(subject_progresses)
    recommendations = _generate_recommendations(
        subject_progresses, sessions_month, hw_submitted, quiz_count, has_data
    )

    period_label = f"{_MONTHS_FR[now.month]} {now.year}"

    # ── Persist this month's snapshot (upsert) ────────────────────────────────
    snap = db.exec(
        select(StudentMonthlySnapshot).where(
            StudentMonthlySnapshot.student_id == uid,
            StudentMonthlySnapshot.snapshot_month == this_month,
        )
    ).first()

    if snap is None:
        snap = StudentMonthlySnapshot(
            student_id=uid,
            snapshot_month=this_month,
            sessions_count=sessions_month,
            homework_submitted_count=hw_submitted,
            homework_graded_count=hw_graded,
            quiz_count=quiz_count,
            ep_earned=ep_month,
            subject_mastery=current_mastery_snapshot,
        )
        db.add(snap)
    else:
        snap.sessions_count = sessions_month
        snap.homework_submitted_count = hw_submitted
        snap.homework_graded_count = hw_graded
        snap.quiz_count = quiz_count
        snap.ep_earned = ep_month
        snap.subject_mastery = current_mastery_snapshot
        snap.updated_at = datetime.now(timezone.utc)
        db.add(snap)

    db.commit()

    return ProgressReportResponse(
        period_label=period_label,
        global_score_label=global_label,
        global_trend=global_trend,
        global_delta=global_delta,
        sessions_count=sessions_total,
        homework_count=hw_submitted,
        subjects=subject_progresses,
        strengths=strengths,
        weaknesses=weaknesses,
        recommendations=recommendations,
        has_data=has_data,
    )
