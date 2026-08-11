from __future__ import annotations

"""
Analysis Engine — Competitive Arena Phase 4.

Every number here is computed from real match/attempt data — nothing is
hardcoded or mocked. The LLM-facing layer (app/services/ai.py's
generate_match_analysis, called from gameplay_analysis_service.py) only
turns these already-real numbers into encouraging prose; it never invents
statistics itself.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from sqlmodel import Session, func, select

from app.models.competitive import (
    CompetitiveMatch,
    CompetitiveMatchPlayerStats,
    CompetitiveMatchQuestion,
    CompetitiveQuestionAttempt,
)
from app.models.practice import Chapter, Question

#: Thresholds are deliberately simple/explainable — "future AI improvements
#: must be possible without redesign", not a machine-learned classifier today.
_GUESS_RATIO = 0.15
_TIME_PRESSURE_RATIO = 0.85
_WEAK_ACCURACY_PCT = 60.0
#: Response-time ratio (of the question's own answer window) below which a
#: CORRECT answer counts toward "Fast Thinker" — distinct from (lower than)
#: the "guess" threshold, since a correct-but-fast answer is a genuine
#: strength signal, not a mistake category.
_FAST_THINKER_RATIO = 0.35
_STRONG_ACCURACY_PCT = 85.0
_MIN_ATTEMPTS_FOR_SIGNAL = 2


def categorize_mistake(response_time_ms: Optional[int], answer_window_ms: Optional[int]) -> str:
    """guess (answered almost instantly — likely a random pick) | time_pressure
    (used nearly the whole window and still got it wrong) | knowledge_gap
    (the honest default — no calculation/concept/reading-mistake distinction
    is possible without free-text answers, which single-choice questions
    don't have yet)."""
    if not response_time_ms or not answer_window_ms:
        return "knowledge_gap"
    ratio = response_time_ms / answer_window_ms
    if ratio < _GUESS_RATIO:
        return "guess"
    if ratio > _TIME_PRESSURE_RATIO:
        return "time_pressure"
    return "knowledge_gap"


def apply_mistake_categorization(db: Session, match: CompetitiveMatch) -> None:
    mqs = db.exec(select(CompetitiveMatchQuestion).where(CompetitiveMatchQuestion.match_id == match.id)).all()
    for mq in mqs:
        window_ms = None
        if mq.reading_ends_at and mq.answer_ends_at:
            window_ms = int((mq.answer_ends_at - mq.reading_ends_at).total_seconds() * 1000)
        attempts = db.exec(
            select(CompetitiveQuestionAttempt)
            .where(CompetitiveQuestionAttempt.match_question_id == mq.id)
            .where(CompetitiveQuestionAttempt.is_correct.is_(False))
            .where(CompetitiveQuestionAttempt.mistake_category.is_(None))
        ).all()
        for attempt in attempts:
            attempt.mistake_category = categorize_mistake(attempt.response_time_ms, window_ms)
            db.add(attempt)
    db.commit()


def compute_fast_correct_ratio(db: Session, match: CompetitiveMatch, user_id: UUID) -> float:
    """Share of THIS match's correct answers this player answered fast
    (response_time_ms below _FAST_THINKER_RATIO of the question's own
    answer window) — feeds the 'Fast Thinker' achievement hook."""
    mqs = db.exec(select(CompetitiveMatchQuestion).where(CompetitiveMatchQuestion.match_id == match.id)).all()
    correct = 0
    fast = 0
    for mq in mqs:
        window_ms = None
        if mq.reading_ends_at and mq.answer_ends_at:
            window_ms = int((mq.answer_ends_at - mq.reading_ends_at).total_seconds() * 1000)
        attempt = db.exec(
            select(CompetitiveQuestionAttempt)
            .where(CompetitiveQuestionAttempt.match_question_id == mq.id)
            .where(CompetitiveQuestionAttempt.user_id == user_id)
        ).first()
        if attempt is None or not attempt.is_correct:
            continue
        correct += 1
        if attempt.response_time_ms is not None and window_ms:
            if (attempt.response_time_ms / window_ms) < _FAST_THINKER_RATIO:
                fast += 1
    return round(fast / correct, 2) if correct else 0.0


def _user_attempt_rows(db: Session, user_id: UUID) -> List[Tuple[CompetitiveQuestionAttempt, Question]]:
    return list(
        db.exec(
            select(CompetitiveQuestionAttempt, Question)
            .join(CompetitiveMatchQuestion, CompetitiveMatchQuestion.id == CompetitiveQuestionAttempt.match_question_id)
            .join(Question, Question.id == CompetitiveMatchQuestion.question_id)
            .where(CompetitiveQuestionAttempt.user_id == user_id)
        ).all()
    )


def compute_breakdowns(db: Session, user_id: UUID) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Full recompute from all of this user's competitive attempts (never
    thousands of rows per student — cheap, and avoids incremental-update
    drift risk). Returns (subject_breakdown, difficulty_breakdown)."""
    rows = _user_attempt_rows(db, user_id)

    def _bucket(key_fn) -> Dict[str, Any]:
        agg: Dict[str, Dict[str, Any]] = {}
        for attempt, question in rows:
            key = str(key_fn(question))
            if key == "None":
                continue
            b = agg.setdefault(key, {"correct": 0, "wrong": 0, "response_times": []})
            if attempt.is_correct:
                b["correct"] += 1
            else:
                b["wrong"] += 1
            if attempt.response_time_ms is not None:
                b["response_times"].append(attempt.response_time_ms)
        out: Dict[str, Any] = {}
        for key, b in agg.items():
            total = b["correct"] + b["wrong"]
            out[key] = {
                "attempts": total,
                "correct": b["correct"],
                "wrong": b["wrong"],
                "accuracy_pct": round((b["correct"] / total) * 100, 1) if total else 0.0,
                "avg_response_time_ms": round(sum(b["response_times"]) / len(b["response_times"])) if b["response_times"] else None,
            }
        return out

    subject_breakdown = _bucket(lambda q: q.subject_id)
    difficulty_breakdown = _bucket(lambda q: q.difficulty)
    return subject_breakdown, difficulty_breakdown


def compute_chapter_weakness(db: Session, user_id: UUID, *, limit: int = 3) -> List[Dict[str, Any]]:
    rows = _user_attempt_rows(db, user_id)
    chapter_ids = {q.chapter_id for _, q in rows if q.chapter_id}
    if not chapter_ids:
        return []
    chapters = {c.id: c for c in db.exec(select(Chapter).where(Chapter.id.in_(chapter_ids))).all()}

    agg: Dict[UUID, Dict[str, int]] = {}
    for attempt, question in rows:
        if not question.chapter_id:
            continue
        b = agg.setdefault(question.chapter_id, {"wrong": 0, "total": 0})
        b["total"] += 1
        if not attempt.is_correct:
            b["wrong"] += 1

    ranked = sorted(
        (
            {"chapter_id": str(cid), "chapter_name": chapters[cid].name if cid in chapters else "?",
             "wrong": b["wrong"], "total": b["total"], "miss_rate_pct": round((b["wrong"] / b["total"]) * 100, 1)}
            for cid, b in agg.items() if b["total"] >= _MIN_ATTEMPTS_FOR_SIGNAL and b["wrong"] > 0
        ),
        key=lambda r: (r["wrong"], r["miss_rate_pct"]), reverse=True,
    )
    return ranked[:limit]


def detect_weaknesses_and_strengths(
    subject_breakdown: Dict[str, Any], difficulty_breakdown: Dict[str, Any],
    chapter_weakness: List[Dict[str, Any]], subject_names: Dict[str, str],
) -> Tuple[List[str], List[str]]:
    weaknesses: List[str] = []
    strengths: List[str] = []

    for subject_id, stats in subject_breakdown.items():
        if stats["attempts"] < _MIN_ATTEMPTS_FOR_SIGNAL:
            continue
        name = subject_names.get(subject_id, "cette matière")
        if stats["accuracy_pct"] < _WEAK_ACCURACY_PCT:
            weaknesses.append(f"Difficultés en {name} ({stats['accuracy_pct']}% de bonnes réponses)")
        elif stats["accuracy_pct"] >= _STRONG_ACCURACY_PCT:
            strengths.append(f"Très bonne maîtrise en {name} ({stats['accuracy_pct']}% de bonnes réponses)")

    for difficulty, stats in difficulty_breakdown.items():
        if stats["attempts"] < _MIN_ATTEMPTS_FOR_SIGNAL:
            continue
        if stats["accuracy_pct"] < _WEAK_ACCURACY_PCT:
            weaknesses.append(f"Niveau {difficulty} à retravailler ({stats['accuracy_pct']}% de réussite)")
        elif stats["accuracy_pct"] >= _STRONG_ACCURACY_PCT:
            strengths.append(f"Très à l'aise en difficulté {difficulty}")

    for chap in chapter_weakness:
        weaknesses.append(f"Chapitre à revoir : {chap['chapter_name']} ({chap['wrong']} erreurs)")

    return strengths[:5], weaknesses[:5]


def compare_progress(db: Session, user_id: UUID, *, current_match_id: UUID) -> Dict[str, Any]:
    """vs previous match / last 5 / last 10 — accuracy + speed only (the
    fields the spec asks for that we can honestly compute; 'ranking' is
    excluded since rating/rank_tier calculation is explicitly Phase 7)."""
    history = list(
        db.exec(
            select(CompetitiveMatchPlayerStats)
            .where(CompetitiveMatchPlayerStats.user_id == user_id)
            .order_by(CompetitiveMatchPlayerStats.created_at.desc())
        ).all()
    )
    current = next((s for s in history if s.match_id == current_match_id), None)
    prior = [s for s in history if s.match_id != current_match_id]
    if current is None:
        return {}

    def _trend(current_val: Optional[float], baseline_val: Optional[float]) -> str:
        if current_val is None or baseline_val is None:
            return "stable"
        if current_val > baseline_val * 1.05:
            return "improved"
        if current_val < baseline_val * 0.95:
            return "declined"
        return "stable"

    def _speed_trend(current_ms: Optional[int], baseline_ms: Optional[int]) -> str:
        # Lower response time = improved speed.
        if current_ms is None or baseline_ms is None:
            return "stable"
        if current_ms < baseline_ms * 0.95:
            return "improved"
        if current_ms > baseline_ms * 1.05:
            return "declined"
        return "stable"

    def _window(n: Optional[int]) -> Dict[str, str]:
        window = prior[:n] if n else prior[:1]
        if not window:
            return {"accuracy": "stable", "speed": "stable"}
        avg_acc = sum(s.accuracy_pct for s in window) / len(window)
        times = [s.avg_response_time_ms for s in window if s.avg_response_time_ms is not None]
        avg_speed = sum(times) / len(times) if times else None
        return {
            "accuracy": _trend(current.accuracy_pct, avg_acc),
            "speed": _speed_trend(current.avg_response_time_ms, avg_speed),
        }

    result = {
        "vs_previous": _window(1),
        "vs_last_5": _window(5),
        "vs_last_10": _window(10),
    }

    # Phase 12 — "vs Current Season" (season_stats.accuracy_pct is already
    # the continuously-updated running average — see CompetitiveSeasonStats'
    # own docstring) and "vs Personal Best" (max accuracy_pct across this
    # user's entire match history, self-inclusive so a record-setting match
    # correctly shows "stable", never "declined" against itself).
    from app.services.competitive import season_service
    season = season_service.get_active_season(db)
    if season is not None:
        from app.models.competitive import CompetitiveSeasonStats
        season_stats = db.exec(
            select(CompetitiveSeasonStats).where(CompetitiveSeasonStats.season_id == season.id).where(CompetitiveSeasonStats.user_id == user_id)
        ).first()
        if season_stats is not None and season_stats.matches_played > 0:
            result["vs_season"] = {"accuracy": _trend(current.accuracy_pct, season_stats.accuracy_pct), "speed": "stable"}

    if history:
        best_accuracy = max(s.accuracy_pct for s in history)
        result["vs_personal_best"] = {
            "accuracy": _trend(current.accuracy_pct, best_accuracy) if current.accuracy_pct < best_accuracy else "stable",
            "is_personal_best": current.accuracy_pct >= best_accuracy,
        }

    return result


def is_new_personal_best(db: Session, user_id: UUID, current_match_id: UUID) -> bool:
    """True iff this match's accuracy is the highest this user has EVER
    recorded (ties count — the first time a peak is reached is the record).
    Used to trigger the 'competitive_personal_best' notification (Phase 12)."""
    history = list(db.exec(select(CompetitiveMatchPlayerStats).where(CompetitiveMatchPlayerStats.user_id == user_id)).all())
    current = next((s for s in history if s.match_id == current_match_id), None)
    if current is None or len(history) < 2:
        return False
    others = [s.accuracy_pct for s in history if s.match_id != current_match_id]
    return bool(others) and current.accuracy_pct >= max(others) and current.accuracy_pct > 0


# ─── Phase 12: Match Rating (grade) ──────────────────────────────────────────

_GRADES = ["A+", "A", "B+", "B", "C", "D", "F"]


def compute_match_grade(stats: CompetitiveMatchPlayerStats, settings_row: Any) -> str:
    """Deterministic — NEVER AI-generated (spec: 'AI must NOT invent
    information'; a grade is exactly the kind of claim that must be
    reproducible). Primary signal is accuracy (admin-configurable cutoffs,
    PlatformSettings.competitive_grade_*); a clean forfeit-free win nudges
    a borderline grade up half a tier, a loss with very low accuracy is
    capped at 'F' regardless of speed."""
    acc = stats.accuracy_pct
    if acc >= settings_row.competitive_grade_a_plus_min_accuracy:
        grade = "A+"
    elif acc >= settings_row.competitive_grade_a_min_accuracy:
        grade = "A"
    elif acc >= settings_row.competitive_grade_b_plus_min_accuracy:
        grade = "B+"
    elif acc >= settings_row.competitive_grade_b_min_accuracy:
        grade = "B"
    elif acc >= settings_row.competitive_grade_c_min_accuracy:
        grade = "C"
    elif acc >= settings_row.competitive_grade_d_min_accuracy:
        grade = "D"
    else:
        grade = "F"

    if stats.result == "win" and grade in ("B", "C", "D") and stats.wrong_count == 0:
        grade = _GRADES[max(0, _GRADES.index(grade) - 1)]
    return grade


# ─── Phase 12: Performance Analyzer (graphs across recent matches) ──────────

def get_performance_graphs(db: Session, user_id: UUID, *, limit: int = 20) -> Dict[str, Any]:
    """Score/accuracy/response-time/streak/ranking evolution across this
    user's last `limit` matches, oldest first (chart-ready series) —
    spec's 'Performance Graph' section. Ranking evolution reuses
    CompetitiveRatingHistory (already the per-match rating ledger, Phase 7)
    rather than recomputing anything."""
    from app.models.competitive import CompetitiveRatingHistory

    stats_rows = list(
        db.exec(
            select(CompetitiveMatchPlayerStats)
            .where(CompetitiveMatchPlayerStats.user_id == user_id)
            .order_by(CompetitiveMatchPlayerStats.created_at.desc())
            .limit(limit)
        ).all()
    )
    stats_rows.reverse()  # oldest first for charting

    rating_by_match = {
        r.match_id: r.rating_after
        for r in db.exec(
            select(CompetitiveRatingHistory)
            .where(CompetitiveRatingHistory.user_id == user_id)
            .where(CompetitiveRatingHistory.match_id.in_([s.match_id for s in stats_rows]))
        ).all()
    } if stats_rows else {}

    return {
        "score_evolution": [{"match_id": str(s.match_id), "value": s.score, "at": s.created_at.isoformat()} for s in stats_rows],
        "accuracy_evolution": [{"match_id": str(s.match_id), "value": s.accuracy_pct, "at": s.created_at.isoformat()} for s in stats_rows],
        "response_time_evolution": [{"match_id": str(s.match_id), "value": s.avg_response_time_ms, "at": s.created_at.isoformat()} for s in stats_rows],
        "streak_evolution": [{"match_id": str(s.match_id), "value": s.best_streak, "at": s.created_at.isoformat()} for s in stats_rows],
        "ranking_evolution": [
            {"match_id": str(s.match_id), "value": rating_by_match.get(s.match_id), "at": s.created_at.isoformat()}
            for s in stats_rows if s.match_id in rating_by_match
        ],
    }


# ─── Phase 12: Trend Analyzer (long-term progress) ──────────────────────────

def analyze_long_term_trends(db: Session, user_id: UUID, *, limit: int = 20) -> Dict[str, Any]:
    """Recurring mistakes/strengths + consistency, over the last `limit`
    matches' worth of question attempts — spec's 'Long-Term Progress'
    section ('recurring mistakes', 'recurring strengths', 'consistency').
    Reuses categorize_mistake's already-stored mistake_category (Phase 4),
    never recomputed."""
    recent_match_ids = list(
        db.exec(
            select(CompetitiveMatchPlayerStats.match_id)
            .where(CompetitiveMatchPlayerStats.user_id == user_id)
            .order_by(CompetitiveMatchPlayerStats.created_at.desc())
            .limit(limit)
        ).all()
    )
    if not recent_match_ids:
        return {"recurring_mistakes": [], "recurring_strengths": [], "consistency_pct": None, "speed_trend": "stable", "accuracy_trend": "stable"}

    mistake_rows = db.exec(
        select(CompetitiveQuestionAttempt.mistake_category, func.count())
        .join(CompetitiveMatchQuestion, CompetitiveMatchQuestion.id == CompetitiveQuestionAttempt.match_question_id)
        .where(CompetitiveMatchQuestion.match_id.in_(recent_match_ids))
        .where(CompetitiveQuestionAttempt.user_id == user_id)
        .where(CompetitiveQuestionAttempt.is_correct.is_(False))
        .where(CompetitiveQuestionAttempt.mistake_category.is_not(None))
        .group_by(CompetitiveQuestionAttempt.mistake_category)
    ).all()
    recurring_mistakes = [{"category": cat, "count": count} for cat, count in sorted(mistake_rows, key=lambda r: -r[1])]

    stats_rows = list(
        db.exec(
            select(CompetitiveMatchPlayerStats)
            .where(CompetitiveMatchPlayerStats.match_id.in_(recent_match_ids))
            .where(CompetitiveMatchPlayerStats.user_id == user_id)
            .order_by(CompetitiveMatchPlayerStats.created_at.asc())
        ).all()
    )
    recurring_strengths = []
    if stats_rows and sum(1 for s in stats_rows if s.accuracy_pct >= _STRONG_ACCURACY_PCT) >= max(2, len(stats_rows) // 2):
        recurring_strengths.append({"trait": "high_accuracy_consistency", "matches": len(stats_rows)})
    if stats_rows and sum(1 for s in stats_rows if s.best_streak >= 3) >= max(2, len(stats_rows) // 2):
        recurring_strengths.append({"trait": "streak_builder", "matches": len(stats_rows)})

    consistency_pct = None
    accuracy_trend = "stable"
    speed_trend = "stable"
    if len(stats_rows) >= 2:
        accs = [s.accuracy_pct for s in stats_rows]
        mean_acc = sum(accs) / len(accs)
        variance = sum((a - mean_acc) ** 2 for a in accs) / len(accs)
        std_dev = variance ** 0.5
        consistency_pct = round(max(0.0, 100 - std_dev), 1)

        half = len(stats_rows) // 2
        first_half_acc = sum(accs[:half]) / half if half else mean_acc
        second_half_acc = sum(accs[half:]) / (len(accs) - half)
        if second_half_acc > first_half_acc * 1.05:
            accuracy_trend = "improved"
        elif second_half_acc < first_half_acc * 0.95:
            accuracy_trend = "declined"

        times = [s.avg_response_time_ms for s in stats_rows if s.avg_response_time_ms is not None]
        if len(times) >= 2:
            half_t = len(times) // 2
            first_half_t = sum(times[:half_t]) / half_t if half_t else times[0]
            second_half_t = sum(times[half_t:]) / (len(times) - half_t)
            if second_half_t < first_half_t * 0.95:
                speed_trend = "improved"
            elif second_half_t > first_half_t * 1.05:
                speed_trend = "declined"

    return {
        "recurring_mistakes": recurring_mistakes, "recurring_strengths": recurring_strengths,
        "consistency_pct": consistency_pct, "accuracy_trend": accuracy_trend, "speed_trend": speed_trend,
    }


def mastery_level_from_accuracy(accuracy_pct: float) -> str:
    if accuracy_pct >= 90:
        return "expert"
    if accuracy_pct >= 70:
        return "avance"
    if accuracy_pct >= 45:
        return "intermediaire"
    return "debutant"


def detect_achievements(
    *, player_result: str, accuracy_pct: float, best_streak: int, current_lifetime_streak: int,
    fast_correct_ratio: float, score_timeline: List[Dict[str, Any]], user_id: str,
) -> List[str]:
    """Event-only hooks (spec: 'No badge calculation yet. Only generate
    events.') — logged to competitive_match_events for a future Badge Engine
    to consume, mirroring how app/services/badge_engine.py already works for
    the rest of the platform."""
    events: List[str] = []
    if accuracy_pct == 100:
        events.append("achievement_perfect_match")
    if fast_correct_ratio >= 0.5:
        events.append("achievement_fast_thinker")
    if accuracy_pct >= 90:
        events.append("achievement_accuracy_master")
    if current_lifetime_streak >= 3:
        events.append("achievement_winning_streak")

    # Comeback: behind at some point, still won.
    if player_result == "win" and len(score_timeline) >= 2:
        was_behind = False
        for snapshot in score_timeline[:-1]:
            scores = snapshot.get("scores", {})
            mine = scores.get(user_id, 0)
            others = [v for k, v in scores.items() if k != user_id]
            if others and mine < max(others):
                was_behind = True
                break
        if was_behind:
            events.append("achievement_comeback")

    return events
