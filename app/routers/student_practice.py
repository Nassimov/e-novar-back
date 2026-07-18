from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, func, select

from app.dependencies import get_current_user, get_db
from app.models.catalog import Subject, Level
from app.models.kp import KpBalance, KpTransaction
from app.models.practice import (
    Question, QuestionChoice,
    QuizAttempt, QuizAnswer,
    StudentSubjectMastery,
)
from app.models.profile import StudentProfile

router = APIRouter(tags=["Student"])

# ─── EP constants ─────────────────────────────────────────────────────────────

BASE_EP_PER_CORRECT = 3.0   # base EP awarded per correct answer at easiest tier

DIFFICULTY_MULTIPLIER: dict[str, float] = {
    "easy":   1.0,
    "medium": 1.5,
    "hard":   2.2,
    "expert": 3.0,
}
PERFECT_BONUS_MULTIPLIER    = 1.25  # +25 % on a perfect score
PERFECT_BONUS_MIN_QUESTIONS = 10    # perfect bonus requires at least 10 questions
MIN_SCORE_FOR_EP            = 0.50  # student must score ≥ 50 % to earn any EP
DAILY_QUIZ_EP_CAP           = 200   # hard cap per calendar day

MIN_QUESTIONS_FLOOR = 5    # absolute minimum quiz length

# ─── Anti-abuse thresholds ────────────────────────────────────────────────────

VELOCITY_SOFT_LIMIT    = 5     # ≥ N quizzes/hour → EP × 0.5
VELOCITY_HARD_LIMIT    = 10    # ≥ N quizzes/hour → EP = 0 + fraud flag
SAME_COMBO_LIMIT       = 3     # ≥ N same (subject+level+diff) in 2 h → EP = 0
QUESTION_REPEAT_THRESH = 0.60  # ≥ 60 % questions seen in last 24 h → EP × 0.5
SPEED_RUN_SEC_PER_Q    = 5     # minimum seconds per question (bot/cheat detection)
DAILY_FRAUD_LIMIT      = 3     # ≥ N hard fraud flags today → full cooldown

# ─── Selection constants ──────────────────────────────────────────────────────

HISTORY_THRESHOLD    = 3    # distinct questions with history before intelligent selection
RECENT_DAYS          = 30   # rolling window treated as "recently seen"
MAX_HISTORY_ATTEMPTS = 200


# ─── Pydantic schemas ─────────────────────────────────────────────────────────

class SubjectItem(BaseModel):
    id: str
    slug: str
    name: str

class LevelItem(BaseModel):
    id: str
    code: str
    label: str
    position: int

class PracticeConfigResponse(BaseModel):
    studied_subjects: List[SubjectItem]
    other_subjects: List[SubjectItem]
    all_levels: List[LevelItem]


class StartQuizRequest(BaseModel):
    subject_id: str
    level_id: str
    difficulty: str       # easy | medium | hard | expert
    question_count: int   # 5 | 10 | 20 | 40


class ChoiceOut(BaseModel):
    id: str
    label: str
    text: str

class QuizQuestionOut(BaseModel):
    id: str
    position: int
    statement: str
    choices: List[ChoiceOut]
    time_limit_sec: int
    is_multi_choice: bool

class StartQuizResponse(BaseModel):
    attempt_id: str
    questions: List[QuizQuestionOut]


class AnswerIn(BaseModel):
    question_id: str
    selected_choice_ids: List[str] = []   # empty = skipped / timed-out


class SubmitQuizRequest(BaseModel):
    answers: List[AnswerIn]


class ChoiceResult(BaseModel):
    id: str
    label: str
    text: str
    is_correct: bool

class QuestionResult(BaseModel):
    question_id: str
    statement: str
    choices: List[ChoiceResult]
    selected_choice_ids: List[str]
    is_correct: bool
    explanation: str

class SubmitQuizResponse(BaseModel):
    attempt_id: str
    score: int
    question_count: int
    score_pct: float
    ep_awarded: int
    ep_note: Optional[str] = None   # human-readable reason when EP is reduced / blocked
    results: List[QuestionResult]


class HistoryItem(BaseModel):
    id: str
    subject_name: str
    level_label: str
    difficulty: str
    question_count: int
    correct_answers: int
    score_percentage: Optional[float]
    ep_earned: int
    started_at: str
    completed_at: Optional[str]

class HistoryResponse(BaseModel):
    attempts: List[HistoryItem]


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _calc_ep(difficulty: str, correct: int, total: int) -> int:
    """Raw EP before anti-abuse adjustments.

    Formula: BASE_EP_PER_CORRECT × difficulty_multiplier × correct_answers
    + 25 % perfect-score bonus when ≥ 10 questions and score = 100 %.
    Returns 0 if score < 50 % or quiz is too short.
    """
    if total < MIN_QUESTIONS_FLOOR or correct == 0:
        return 0
    pct = correct / total
    if pct < MIN_SCORE_FOR_EP:
        return 0
    base = BASE_EP_PER_CORRECT * DIFFICULTY_MULTIPLIER.get(difficulty, 1.0) * correct
    if pct == 1.0 and total >= PERFECT_BONUS_MIN_QUESTIONS:
        base *= PERFECT_BONUS_MULTIPLIER
    return max(1, round(base))


# ─── Anti-abuse ───────────────────────────────────────────────────────────────

@dataclass
class _FraudResult:
    ep_multiplier: float          # 0.0 = no EP; 0.5 = halved; 1.0 = full
    fraud_flag:    bool           # True = hard abuse; stored on attempt for audit
    fraud_reason:  Optional[str]  # machine-readable codes, stored in DB
    ep_note:       Optional[str]  # French human-readable note, returned in API


_FRAUD_NOTES: dict[str, str] = {
    "cooldown_journalier": "Cooldown actif — trop de tentatives suspectes aujourd'hui",
    "speed_run":           "Quiz complété trop rapidement",
    "velocity_excessive":  "Activité excessive détectée",
    "farming_meme_combo":  "Récompense bloquée — même configuration répétée",
    "velocity_elevee":     "Récompense réduite — activité élevée",
    "repetition_questions":"Récompense réduite — questions récemment vues",
}


def _reason_to_note(reason: Optional[str]) -> Optional[str]:
    if not reason:
        return None
    notes = [_FRAUD_NOTES.get(r.strip(), r.strip()) for r in reason.split(";")]
    return " · ".join(n for n in notes if n) or None


def _assess_fraud(
    student_id:      UUID,
    subject_id:      Optional[UUID],
    level_id:        Optional[UUID],
    difficulty:      str,
    question_ids:    list[UUID],
    started_at:      datetime,
    total_questions: int,
    db:              Session,
) -> _FraudResult:
    now = datetime.now(timezone.utc)

    # ── 1. Daily cooldown: already accumulated too many hard flags today ──────
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    daily_flags = int(db.exec(
        select(func.count(QuizAttempt.id)).where(
            QuizAttempt.student_id == student_id,
            QuizAttempt.fraud_flag == True,   # noqa: E712
            QuizAttempt.completed_at >= today_start,
        )
    ).one() or 0)
    if daily_flags >= DAILY_FRAUD_LIMIT:
        return _FraudResult(0.0, True, "cooldown_journalier",
                            _FRAUD_NOTES["cooldown_journalier"])

    # ── 2. Speed-run detection ────────────────────────────────────────────────
    elapsed = (now - started_at).total_seconds()
    if elapsed < total_questions * SPEED_RUN_SEC_PER_Q:
        return _FraudResult(0.0, True, "speed_run", _FRAUD_NOTES["speed_run"])

    # ── 3. Velocity — hard stop ───────────────────────────────────────────────
    one_hour_ago = now - timedelta(hours=1)
    recent_count = int(db.exec(
        select(func.count(QuizAttempt.id)).where(
            QuizAttempt.student_id == student_id,
            QuizAttempt.completed  == True,   # noqa: E712
            QuizAttempt.completed_at >= one_hour_ago,
        )
    ).one() or 0)
    if recent_count >= VELOCITY_HARD_LIMIT:
        return _FraudResult(0.0, True, "velocity_excessive",
                            _FRAUD_NOTES["velocity_excessive"])

    # ── 4. Same-combo farming ─────────────────────────────────────────────────
    if subject_id and level_id:
        two_hours_ago = now - timedelta(hours=2)
        combo_count = int(db.exec(
            select(func.count(QuizAttempt.id)).where(
                QuizAttempt.student_id      == student_id,
                QuizAttempt.subject_id      == subject_id,
                QuizAttempt.school_level_id == level_id,
                QuizAttempt.difficulty      == difficulty,
                QuizAttempt.completed       == True,   # noqa: E712
                QuizAttempt.completed_at    >= two_hours_ago,
            )
        ).one() or 0)
        if combo_count >= SAME_COMBO_LIMIT:
            return _FraudResult(0.0, True, "farming_meme_combo",
                                _FRAUD_NOTES["farming_meme_combo"])

    # ── Soft penalties (accumulate) ───────────────────────────────────────────
    multiplier = 1.0
    soft_reasons: list[str] = []

    # 5. Velocity — soft penalty
    if recent_count >= VELOCITY_SOFT_LIMIT:
        multiplier *= 0.5
        soft_reasons.append("velocity_elevee")

    # 6. Question-repetition penalty
    if question_ids:
        one_day_ago = now - timedelta(hours=24)
        recent_attempts = db.exec(
            select(QuizAttempt).where(
                QuizAttempt.student_id == student_id,
                QuizAttempt.completed  == True,   # noqa: E712
                QuizAttempt.started_at >= one_day_ago,
            )
        ).all()
        seen_24h: set[UUID] = set()
        for a in recent_attempts:
            if a.question_ids:
                seen_24h.update(a.question_ids)
        overlap_pct = sum(1 for q in question_ids if q in seen_24h) / len(question_ids)
        if overlap_pct >= QUESTION_REPEAT_THRESH:
            multiplier *= 0.5
            soft_reasons.append("repetition_questions")

    reason_str = "; ".join(soft_reasons) if soft_reasons else None
    return _FraudResult(
        ep_multiplier=multiplier,
        fraud_flag=False,
        fraud_reason=reason_str,
        ep_note=_reason_to_note(reason_str),
    )


def _daily_ep_used(student_id: UUID, db: Session) -> int:
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    result = db.exec(
        select(func.coalesce(func.sum(QuizAttempt.ep_earned), 0)).where(
            QuizAttempt.student_id == student_id,
            QuizAttempt.completed == True,   # noqa: E712
            QuizAttempt.completed_at >= today,
        )
    ).one()
    return int(result or 0)


def _select_questions(
    student_id: UUID,
    subject_id: UUID,
    level_id: UUID,
    all_qs: list[Question],
    count: int,
    db: Session,
) -> list[Question]:
    """Return `count` questions using intelligent slot-based selection.

    With sufficient history (>= HISTORY_THRESHOLD distinct questions):
      50 % normal    – not recently seen, random order
      30 % weak      – answered incorrectly before, sorted by error frequency
      20 % challenge – never seen, sorted by quality_score desc

    Falls back to fresh-first random when history is thin.
    """
    if not all_qs:
        return []

    eligible_ids = {q.id for q in all_qs}

    # ── past attempt history for this subject + level ─────────────────────────
    cutoff_naive = datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)

    past_attempts = db.exec(
        select(QuizAttempt).where(
            QuizAttempt.student_id      == student_id,
            QuizAttempt.subject_id      == subject_id,
            QuizAttempt.school_level_id == level_id,
        ).order_by(QuizAttempt.started_at.desc()).limit(MAX_HISTORY_ATTEMPTS)
    ).all()

    recently_seen: set[UUID] = set()
    ever_seen_ids: set[UUID] = set()
    attempt_ids: list[UUID] = []

    for a in past_attempts:
        if a.question_ids:
            relevant = [i for i in a.question_ids if i in eligible_ids]
            ever_seen_ids.update(relevant)
            if a.started_at >= cutoff_naive:
                recently_seen.update(relevant)
        attempt_ids.append(a.id)

    # ── per-question answer stats ─────────────────────────────────────────────
    per_q_stats: dict[UUID, dict[str, int]] = {}
    if attempt_ids:
        answers = db.exec(
            select(QuizAnswer).where(
                QuizAnswer.attempt_id.in_(attempt_ids),
                QuizAnswer.question_id.in_(list(eligible_ids)),
            )
        ).all()
        for ans in answers:
            s = per_q_stats.setdefault(ans.question_id, {"correct": 0, "wrong": 0})
            if ans.is_correct:
                s["correct"] += 1
            else:
                s["wrong"] += 1

    if len(per_q_stats) < HISTORY_THRESHOLD:
        return _fallback_select(all_qs, recently_seen, count)

    return _intelligent_select(all_qs, recently_seen, ever_seen_ids, per_q_stats, count)


def _intelligent_select(
    all_qs: list[Question],
    recently_seen: set[UUID],
    ever_seen_ids: set[UUID],
    per_q_stats: dict[UUID, dict[str, int]],
    count: int,
) -> list[Question]:
    n_weak      = round(count * 0.30)
    n_challenge = round(count * 0.20)
    n_normal    = count - n_weak - n_challenge

    used: set[UUID] = set()
    selected: list[Question] = []

    # 30 % weak – seen incorrectly, not recently seen, most-wrong first
    weak_pool = [
        q for q in all_qs
        if q.id not in recently_seen and per_q_stats.get(q.id, {}).get("wrong", 0) > 0
    ]
    weak_pool.sort(key=lambda q: per_q_stats[q.id]["wrong"], reverse=True)
    picked = weak_pool[:n_weak]
    selected.extend(picked)
    used.update(q.id for q in picked)

    # 20 % challenge – never seen, highest quality first
    challenge_pool = [q for q in all_qs if q.id not in ever_seen_ids and q.id not in used]
    challenge_pool.sort(
        key=lambda q: q.quality_score if q.quality_score is not None else -1,
        reverse=True,
    )
    picked = challenge_pool[:n_challenge]
    selected.extend(picked)
    used.update(q.id for q in picked)

    # 50 % normal – not recently seen, random order
    normal_pool = [q for q in all_qs if q.id not in recently_seen and q.id not in used]
    random.shuffle(normal_pool)
    picked = normal_pool[:n_normal]
    selected.extend(picked)
    used.update(q.id for q in picked)

    # Fill any remaining slots from any eligible question not yet picked
    if len(selected) < count:
        remaining = [q for q in all_qs if q.id not in used]
        random.shuffle(remaining)
        selected.extend(remaining[:count - len(selected)])

    random.shuffle(selected)
    return selected[:count]


def _fallback_select(
    all_qs: list[Question],
    recently_seen: set[UUID],
    count: int,
) -> list[Question]:
    fresh = [q for q in all_qs if q.id not in recently_seen]
    stale = [q for q in all_qs if q.id in recently_seen]
    random.shuffle(fresh)
    random.shuffle(stale)
    pool = fresh + stale
    return pool[:count]


def _award_ep(student_id: UUID, ep: int, label: str, attempt_id: UUID, db: Session) -> None:
    if ep <= 0:
        return

    # Apply active ep_boost multiplier (e.g. ×2, ×4) — non-blocking on failure
    actual_ep = ep
    try:
        from app.services.effects import get_active_ep_boost
        boost = get_active_ep_boost(student_id, db)
        if boost:
            multiplier = float((boost.effect_config or {}).get("multiplier", 2.0))
            actual_ep = max(1, int(ep * multiplier))
    except Exception:
        pass

    # Balance/XP maintained by apply_kp_transaction() trigger — insert only.
    db.add(KpTransaction(
        user_id=student_id,
        amount=actual_ep,
        source="quiz",
        label=label if actual_ep == ep else f"{label} (×{actual_ep // ep if ep else 1} boost)",
        ref_type="quiz_attempt",
        ref_id=attempt_id,
    ))


def _upsert_mastery(
    student_id: UUID,
    subject_id: UUID,
    school_level_id: Optional[UUID],
    correct: int,
    total: int,
    db: Session,
) -> None:
    row = db.exec(
        select(StudentSubjectMastery).where(
            StudentSubjectMastery.student_id == student_id,
            StudentSubjectMastery.subject_id == subject_id,
            StudentSubjectMastery.school_level_id == school_level_id,
            StudentSubjectMastery.chapter_id.is_(None),
        )
    ).first()

    now = datetime.now(timezone.utc)
    if row is None:
        pct = round((correct / total) * 100, 2) if total else 0
        db.add(StudentSubjectMastery(
            student_id=student_id,
            subject_id=subject_id,
            school_level_id=school_level_id,
            mastery_pct=pct,
            attempts_count=1,
            correct_count=correct,
            last_attempted=now,
            updated_at=now,
        ))
    else:
        row.attempts_count += 1
        row.correct_count  += correct
        total_q = row.attempts_count * total if total else 1
        row.mastery_pct  = round((row.correct_count / max(row.attempts_count * (total or 1), 1)) * 100, 2)
        row.last_attempted = now
        row.updated_at     = now
        db.add(row)


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/practice/config", response_model=PracticeConfigResponse)
async def get_practice_config(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])

    all_subjects = db.exec(select(Subject).order_by(Subject.name)).all()
    all_levels   = db.exec(select(Level).order_by(Level.position)).all()

    studied_ids: set[UUID] = set()
    try:
        from app.models.booking import TutoringSession
        rows = db.exec(
            select(TutoringSession.subject_id).where(
                TutoringSession.student_id == uid,
                TutoringSession.subject_id.is_not(None),
            ).distinct()
        ).all()
        studied_ids = {r for r in rows if r is not None}
    except Exception:
        pass

    studied = [s for s in all_subjects if s.id in studied_ids]
    other   = [s for s in all_subjects if s.id not in studied_ids]

    return PracticeConfigResponse(
        studied_subjects=[SubjectItem(id=str(s.id), slug=s.slug, name=s.name) for s in studied],
        other_subjects  =[SubjectItem(id=str(s.id), slug=s.slug, name=s.name) for s in other],
        all_levels=[
            LevelItem(id=str(l.id), code=l.code, label=l.label, position=l.position)
            for l in all_levels
        ],
    )


@router.post("/practice/start", response_model=StartQuizResponse)
async def start_quiz(
    payload: StartQuizRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])

    if payload.difficulty not in DIFFICULTY_MULTIPLIER:
        raise HTTPException(status_code=422, detail="Difficulté invalide.")
    if payload.question_count not in (5, 10, 20, 40):
        raise HTTPException(status_code=422, detail="Nombre de questions invalide.")

    try:
        subject_id = UUID(payload.subject_id)
        level_id   = UUID(payload.level_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="IDs invalides.")

    # Abandon any active incomplete attempts
    stale = db.exec(
        select(QuizAttempt).where(
            QuizAttempt.student_id == uid,
            QuizAttempt.completed  == False,   # noqa: E712
        )
    ).all()
    for a in stale:
        a.completed    = False   # stays incomplete; just mark completed_at
        a.completed_at = datetime.now(timezone.utc)
        db.add(a)

    # Fetch eligible questions (active + validated + not soft-deleted)
    all_qs = db.exec(
        select(Question).where(
            Question.subject_id      == subject_id,
            Question.school_level_id == level_id,
            Question.difficulty      == payload.difficulty,
            Question.active          == True,    # noqa: E712
            Question.validated       == True,    # noqa: E712
            Question.deleted_at.is_(None),
        )
    ).all()

    if len(all_qs) < MIN_QUESTIONS_FLOOR or len(all_qs) < payload.question_count:
        raise HTTPException(status_code=422, detail="not_enough_questions")

    selected = _select_questions(uid, subject_id, level_id, all_qs, payload.question_count, db)

    # Batch-load choices for selected questions
    sel_ids = [q.id for q in selected]
    all_choices = db.exec(
        select(QuestionChoice)
        .where(QuestionChoice.question_id.in_(sel_ids))
        .order_by(QuestionChoice.label)
    ).all()
    choices_by_q: dict[UUID, list[QuestionChoice]] = {}
    for c in all_choices:
        choices_by_q.setdefault(c.question_id, []).append(c)

    # Persist attempt
    attempt = QuizAttempt(
        student_id      = uid,
        subject_id      = subject_id,
        school_level_id = level_id,
        difficulty      = payload.difficulty,
        total_questions = payload.question_count,
        question_ids    = sel_ids,
    )
    db.add(attempt)
    db.commit()
    db.refresh(attempt)

    return StartQuizResponse(
        attempt_id=str(attempt.id),
        questions=[
            QuizQuestionOut(
                id=str(q.id),
                position=i,
                statement=q.statement,
                choices=[
                    ChoiceOut(id=str(c.id), label=c.label, text=c.text)
                    for c in choices_by_q.get(q.id, [])
                ],
                time_limit_sec=q.estimated_time_sec,
                is_multi_choice=q.is_multi_choice,
            )
            for i, q in enumerate(selected)
        ],
    )


@router.post("/practice/attempts/{attempt_id}/submit", response_model=SubmitQuizResponse)
async def submit_quiz(
    attempt_id: str,
    payload: SubmitQuizRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])
    try:
        att_uuid = UUID(attempt_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="ID invalide.")

    attempt = db.exec(
        select(QuizAttempt).where(
            QuizAttempt.id         == att_uuid,
            QuizAttempt.student_id == uid,
        )
    ).first()
    if attempt is None:
        raise HTTPException(status_code=404, detail="Tentative introuvable.")

    # Idempotent: already completed → return cached result
    if attempt.completed:
        existing = db.exec(
            select(QuizAnswer).where(QuizAnswer.attempt_id == att_uuid)
        ).all()
        q_ids = list(attempt.question_ids or [])
        qs    = db.exec(select(Question).where(Question.id.in_(q_ids))).all()
        q_map = {q.id: q for q in qs}
        all_c = db.exec(
            select(QuestionChoice).where(QuestionChoice.question_id.in_(q_ids))
        ).all()
        c_by_q: dict[UUID, list[QuestionChoice]] = {}
        for c in all_c:
            c_by_q.setdefault(c.question_id, []).append(c)

        results = []
        for ans in existing:
            q = q_map.get(ans.question_id)
            if q:
                results.append(QuestionResult(
                    question_id=str(ans.question_id),
                    statement=q.statement,
                    choices=[
                        ChoiceResult(id=str(c.id), label=c.label, text=c.text, is_correct=c.is_correct)
                        for c in sorted(c_by_q.get(q.id, []), key=lambda c: c.label)
                    ],
                    selected_choice_ids=[str(u) for u in (ans.selected_choice_ids or [])],
                    is_correct=ans.is_correct,
                    explanation=q.explanation,
                ))
        score = attempt.correct_answers
        total = attempt.total_questions
        return SubmitQuizResponse(
            attempt_id=attempt_id,
            score=score,
            question_count=total,
            score_pct=round(score / total, 3) if total else 0,
            ep_awarded=attempt.ep_earned,
            ep_note=_reason_to_note(attempt.fraud_reason),
            results=results,
        )

    # Grade
    ordered_ids: list[UUID] = list(attempt.question_ids or [])
    if not ordered_ids:
        raise HTTPException(status_code=422, detail="Aucune question dans cette tentative.")

    qs     = db.exec(select(Question).where(Question.id.in_(ordered_ids))).all()
    q_map  = {q.id: q for q in qs}
    all_c  = db.exec(
        select(QuestionChoice).where(QuestionChoice.question_id.in_(ordered_ids))
    ).all()
    c_by_q: dict[UUID, list[QuestionChoice]] = {}
    c_by_id: dict[UUID, QuestionChoice] = {}
    for c in all_c:
        c_by_q.setdefault(c.question_id, []).append(c)
        c_by_id[c.id] = c

    # Build answer lookup: question_id → list of selected choice ID strings
    ans_lookup: dict[str, List[str]] = {
        a.question_id: a.selected_choice_ids for a in payload.answers
    }

    correct_count = 0
    answer_rows: list[QuizAnswer] = []
    results: list[QuestionResult] = []

    for qid in ordered_ids:
        q = q_map.get(qid)
        if q is None:
            continue

        raw_ids = ans_lookup.get(str(qid), [])
        sel_uuids: list[UUID] = []
        for raw in raw_ids:
            try:
                sel_uuids.append(UUID(raw))
            except ValueError:
                pass

        choices_for_q = c_by_q.get(qid, [])
        correct_ids   = {c.id for c in choices_for_q if c.is_correct}
        selected_set  = set(sel_uuids)

        if q.is_multi_choice:
            # All correct choices must be selected, and nothing else
            is_correct = bool(selected_set) and selected_set == correct_ids
        else:
            # Exactly one selection, and it must be correct
            is_correct = len(sel_uuids) == 1 and sel_uuids[0] in correct_ids

        if is_correct:
            correct_count += 1

        answer_rows.append(QuizAnswer(
            attempt_id=att_uuid,
            question_id=qid,
            selected_choice_ids=sel_uuids,
            is_correct=is_correct,
        ))
        results.append(QuestionResult(
            question_id=str(qid),
            statement=q.statement,
            choices=[
                ChoiceResult(id=str(c.id), label=c.label, text=c.text, is_correct=c.is_correct)
                for c in sorted(choices_for_q, key=lambda c: c.label)
            ],
            selected_choice_ids=[str(u) for u in sel_uuids],
            is_correct=is_correct,
            explanation=q.explanation,
        ))

    # ── EP calculation ────────────────────────────────────────────────────────
    raw_ep     = _calc_ep(attempt.difficulty, correct_count, attempt.total_questions)
    ep_awarded = 0
    ep_note: Optional[str] = None
    ep_multiplier  = 1.0
    fraud_flag     = False
    fraud_reason:  Optional[str] = None

    if raw_ep == 0:
        pct_tmp = correct_count / attempt.total_questions if attempt.total_questions else 0
        if pct_tmp < MIN_SCORE_FOR_EP and correct_count > 0:
            ep_note = "Score insuffisant — 50 % minimum requis"
    else:
        fraud = _assess_fraud(
            student_id      = uid,
            subject_id      = attempt.subject_id,
            level_id        = attempt.school_level_id,
            difficulty      = attempt.difficulty,
            question_ids    = list(attempt.question_ids or []),
            started_at      = attempt.started_at,
            total_questions = attempt.total_questions,
            db              = db,
        )
        ep_multiplier = fraud.ep_multiplier
        fraud_flag    = fraud.fraud_flag
        fraud_reason  = fraud.fraud_reason
        ep_note       = fraud.ep_note

        adjusted = round(raw_ep * ep_multiplier)
        if adjusted > 0:
            used       = _daily_ep_used(uid, db)
            ep_awarded = min(adjusted, max(0, DAILY_QUIZ_EP_CAP - used))
            if ep_awarded < adjusted:
                cap_note = "Limite journalière atteinte"
                ep_note  = f"{ep_note} · {cap_note}" if ep_note else cap_note

    # ── Persist ───────────────────────────────────────────────────────────────
    for row in answer_rows:
        db.add(row)

    score_pct = round((correct_count / attempt.total_questions) * 100, 2) if attempt.total_questions else 0
    attempt.correct_answers  = correct_count
    attempt.score_percentage = score_pct
    attempt.ep_earned        = ep_awarded
    attempt.ep_multiplier    = ep_multiplier
    attempt.fraud_flag       = fraud_flag
    attempt.fraud_reason     = fraud_reason
    attempt.completed        = True
    attempt.completed_at     = datetime.now(timezone.utc)
    db.add(attempt)

    if ep_awarded > 0:
        pct_int = int(score_pct)
        _award_ep(
            uid, ep_awarded,
            f"Quiz {attempt.difficulty} — {correct_count}/{attempt.total_questions} ({pct_int}%)",
            att_uuid, db,
        )

    if attempt.subject_id:
        _upsert_mastery(uid, attempt.subject_id, attempt.school_level_id, correct_count, attempt.total_questions, db)

    # Check badge conditions after each quiz submission
    try:
        from app.services.badge_engine import check_and_unlock_badges
        check_and_unlock_badges(uid, db)
    except Exception:
        pass  # Never break quiz submission for badge errors

    db.commit()

    return SubmitQuizResponse(
        attempt_id=attempt_id,
        score=correct_count,
        question_count=attempt.total_questions,
        score_pct=round(correct_count / attempt.total_questions, 3) if attempt.total_questions else 0,
        ep_awarded=ep_awarded,
        ep_note=ep_note,
        results=results,
    )


@router.get("/practice/history", response_model=HistoryResponse)
async def get_practice_history(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])

    attempts = db.exec(
        select(QuizAttempt).where(
            QuizAttempt.student_id == uid,
            QuizAttempt.completed  == True,   # noqa: E712
        ).order_by(QuizAttempt.started_at.desc()).limit(20)
    ).all()

    sub_ids = {a.subject_id for a in attempts if a.subject_id}
    lev_ids = {a.school_level_id for a in attempts if a.school_level_id}
    sub_map: dict[UUID, str] = {}
    lev_map: dict[UUID, str] = {}
    if sub_ids:
        subs = db.exec(select(Subject).where(Subject.id.in_(sub_ids))).all()
        sub_map = {s.id: s.name for s in subs}
    if lev_ids:
        levs = db.exec(select(Level).where(Level.id.in_(lev_ids))).all()
        lev_map = {l.id: l.label for l in levs}

    return HistoryResponse(
        attempts=[
            HistoryItem(
                id=str(a.id),
                subject_name=sub_map.get(a.subject_id, "—") if a.subject_id else "—",
                level_label=lev_map.get(a.school_level_id, "—") if a.school_level_id else "—",
                difficulty=a.difficulty,
                question_count=a.total_questions,
                correct_answers=a.correct_answers,
                score_percentage=a.score_percentage,
                ep_earned=a.ep_earned,
                started_at=a.started_at.isoformat(),
                completed_at=a.completed_at.isoformat() if a.completed_at else None,
            )
            for a in attempts
        ]
    )
