from __future__ import annotations

"""
AI Report Service — Competitive Arena Phase 4.

Wraps app/services/ai.py's real Claude call with a deterministic,
data-driven fallback (never a hardcoded generic message) so the feature
keeps working even if the Anthropic API key is missing or the call fails —
same defensive posture as notification_engine.emit(): a side-effect must
never crash the pipeline that triggered it.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict
from uuid import UUID

from sqlmodel import Session, select

from app.config import get_settings
from app.models.admin import PlatformSettings
from app.models.catalog import Subject
from app.models.competitive import CompetitiveMatch, CompetitiveMatchAiAnalysis, CompetitiveMatchPlayerStats
from app.services import ai
from app.services.competitive import analysis_service
from app.services.notification_engine import emit

logger = logging.getLogger(__name__)


def _fallback_report(
    *, accuracy_pct: float, result: str, strengths: list, weaknesses: list, mastery_level: str,
) -> Dict[str, Any]:
    result_phrase = {"win": "Victoire", "loss": "Défaite", "draw": "Match nul"}.get(result, "Match")
    summary = (
        f"{result_phrase} avec {accuracy_pct}% de bonnes réponses. "
        + ("Continue sur cette lancée !" if accuracy_pct >= 70 else "Chaque match est une occasion de progresser.")
    )
    behavior = "Analyse basée sur tes statistiques réelles de ce match et de ton historique compétitif."
    return {
        "overall_summary": summary,
        "strengths": strengths or ["Participation active à l'Arène Compétitive"],
        "weaknesses": weaknesses or ["Continue à jouer pour affiner ton profil de progression"],
        "behavior_notes": behavior,
        "mastery_level": mastery_level,
        "confidence": "medium",
    }


def generate_and_store_analysis(db: Session, match: CompetitiveMatch, user_id: UUID) -> CompetitiveMatchAiAnalysis:
    row = db.get(CompetitiveMatchAiAnalysis, (match.id, user_id))
    if row is None:
        row = CompetitiveMatchAiAnalysis(match_id=match.id, user_id=user_id)
    if row.status == "ready":
        return row  # never regenerate — immutable once produced
    row.status = "processing"
    db.add(row)
    db.commit()

    try:
        player_stats = db.get(CompetitiveMatchPlayerStats, (match.id, user_id))
        if player_stats is None:
            raise ValueError("no player stats yet — match not actually completed")

        subject_breakdown, difficulty_breakdown = analysis_service.compute_breakdowns(db, user_id)
        chapter_weakness = analysis_service.compute_chapter_weakness(db, user_id)
        subject_names = {
            str(s.id): s.name
            for s in db.exec(select(Subject).where(Subject.id.in_(match.subject_ids))).all()
        } if match.subject_ids else {}
        strengths, weaknesses = analysis_service.detect_weaknesses_and_strengths(
            subject_breakdown, difficulty_breakdown, chapter_weakness, subject_names,
        )
        progress_comparison = analysis_service.compare_progress(db, user_id, current_match_id=match.id)
        mastery_level = analysis_service.mastery_level_from_accuracy(player_stats.accuracy_pct)

        match_data = {
            "result": player_stats.result,
            "score": player_stats.score,
            "accuracy_pct": player_stats.accuracy_pct,
            "correct_count": player_stats.correct_count,
            "wrong_count": player_stats.wrong_count,
            "unanswered_count": player_stats.unanswered_count,
            "avg_response_time_ms": player_stats.avg_response_time_ms,
            "best_streak": player_stats.best_streak,
            "subject_breakdown": subject_breakdown,
            "difficulty_breakdown": difficulty_breakdown,
            "weak_chapters": chapter_weakness,
            "progress_comparison": progress_comparison,
        }

        settings = get_settings()
        ai_result: Dict[str, Any]
        try:
            if not settings.anthropic_api_key:
                raise RuntimeError("anthropic_api_key not configured")
            ai_result = ai.generate_match_analysis(match_data)
        except Exception:
            logger.info("AI analysis falling back to deterministic template for match=%s user=%s", match.id, user_id)
            ai_result = _fallback_report(
                accuracy_pct=player_stats.accuracy_pct, result=player_stats.result,
                strengths=strengths, weaknesses=weaknesses, mastery_level=mastery_level,
            )

        row.overall_summary = ai_result.get("overall_summary")
        row.strengths = ai_result.get("strengths") or strengths
        row.weaknesses = ai_result.get("weaknesses") or weaknesses
        row.behavior_notes = ai_result.get("behavior_notes")
        row.mastery_level = ai_result.get("mastery_level") or mastery_level
        row.confidence = ai_result.get("confidence") or "medium"
        row.progress_comparison = progress_comparison
        row.status = "ready"
        row.generated_at = datetime.now(timezone.utc)

        # Phase 12 — Match Rating: deterministic, computed here (never by
        # the LLM call above) from the same real player_stats.
        settings_row = db.get(PlatformSettings, True) or PlatformSettings()
        row.grade = analysis_service.compute_match_grade(player_stats, settings_row)

        db.add(row)
        db.commit()

        # Phase 12 — notification triggers, all evidence-based (derived
        # from the exact numbers just computed above, never invented).
        # Best-effort: a notification failure must never fail the already-
        # persisted analysis.
        try:
            if analysis_service.is_new_personal_best(db, user_id, match.id):
                emit(
                    db, event_type="competitive_personal_best", user_id=user_id,
                    context={"accuracy_pct": player_stats.accuracy_pct},
                    data={"match_id": str(match.id)}, dedup_key=f"competitive_personal_best:{match.id}:{user_id}",
                )
            vs_last_5 = progress_comparison.get("vs_last_5", {})
            if vs_last_5.get("accuracy") == "improved":
                emit(
                    db, event_type="competitive_improvement_detected", user_id=user_id,
                    data={"match_id": str(match.id)}, dedup_key=f"competitive_improvement_detected:{match.id}:{user_id}",
                )
            elif weaknesses:
                emit(
                    db, event_type="competitive_weakness_detected", user_id=user_id,
                    context={"weakness": weaknesses[0]},
                    data={"match_id": str(match.id)}, dedup_key=f"competitive_weakness_detected:{match.id}:{user_id}",
                )
            vs_season = progress_comparison.get("vs_season")
            if vs_season and vs_season.get("accuracy") == "improved":
                emit(
                    db, event_type="competitive_season_improvement", user_id=user_id,
                    data={"match_id": str(match.id)}, dedup_key=f"competitive_season_improvement:{match.id}:{user_id}:{datetime.now(timezone.utc).date().isoformat()}",
                )
        except Exception:
            logger.exception("Phase 12 notification triggers failed for match=%s user=%s", match.id, user_id)
    except Exception:
        db.rollback()
        row = db.get(CompetitiveMatchAiAnalysis, (match.id, user_id)) or CompetitiveMatchAiAnalysis(match_id=match.id, user_id=user_id)
        row.status = "failed"
        row.error = "generation_failed"
        db.add(row)
        db.commit()
        logger.exception("AI analysis generation failed for match=%s user=%s", match.id, user_id)

    db.refresh(row)
    return row
