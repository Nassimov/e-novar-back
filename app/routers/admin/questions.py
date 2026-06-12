from __future__ import annotations

import csv
import io
import json
import math
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from pydantic import BaseModel, field_validator
from sqlmodel import Session, func, select

from app.dependencies import get_admin_user, get_db
from app.models.catalog import Subject, Level
from app.models.practice import (
    Question, QuestionChoice, QuizAnswer, QuizAttempt,
    StudentSubjectMastery, QuestionImportBatch,
)

router = APIRouter(tags=["Admin — Questions"])


VALID_DIFFICULTIES = {"easy", "medium", "hard", "expert"}
VALID_STATUSES     = {"draft", "pending_review", "approved", "rejected"}
VALID_LABELS       = {"A", "B", "C", "D", "E"}


# ─── Pydantic schemas ──────────────────────────────────────────────────────────

class ChoiceIn(BaseModel):
    label:      str
    text:       str
    is_correct: bool = False

    @field_validator("label")
    @classmethod
    def label_valid(cls, v: str) -> str:
        if v.upper() not in VALID_LABELS:
            raise ValueError(f"label must be one of {VALID_LABELS}")
        return v.upper()


class QuestionCreateIn(BaseModel):
    subject_id:         str
    school_level_id:    str
    chapter_id:         Optional[str] = None
    difficulty:         str
    type:               str = "mcq"
    statement:          str
    explanation:        str = ""
    estimated_time_sec: int = 30
    is_multi_choice:    bool = False
    tags:               List[str] = []
    quality_score:      Optional[int] = None
    choices:            List[ChoiceIn]

    @field_validator("difficulty")
    @classmethod
    def diff_valid(cls, v: str) -> str:
        if v not in VALID_DIFFICULTIES:
            raise ValueError(f"difficulty must be one of {VALID_DIFFICULTIES}")
        return v

    @field_validator("choices")
    @classmethod
    def choices_valid(cls, v: List[ChoiceIn]) -> List[ChoiceIn]:
        if len(v) < 2:
            raise ValueError("A question must have at least 2 choices")
        if not any(c.is_correct for c in v):
            raise ValueError("At least one choice must be marked correct")
        return v


class QuestionUpdateIn(QuestionCreateIn):
    pass


class RejectIn(BaseModel):
    reason: str


class ChoiceOut(BaseModel):
    id:         str
    label:      str
    text:       str
    is_correct: bool


class QuestionOut(BaseModel):
    id:                 str
    subject_id:         str
    subject_name:       str
    school_level_id:    str
    level_label:        str
    chapter_id:         Optional[str]
    difficulty:         str
    type:               str
    statement:          str
    explanation:        str
    estimated_time_sec: int
    is_multi_choice:    bool
    quality_score:      Optional[int]
    tags:               List[str]
    source:             str
    validation_status:  str
    validated:          bool
    active:             bool
    rejection_reason:   Optional[str]
    created_at:         str
    updated_at:         str
    deleted_at:         Optional[str]
    choices:            List[ChoiceOut]
    usage_count:        int = 0


class QuestionListResponse(BaseModel):
    items:      List[QuestionOut]
    total:      int
    page:       int
    page_size:  int
    total_pages: int


class AnalyticsResponse(BaseModel):
    total_questions:    int
    by_status:          Dict[str, int]
    by_difficulty:      Dict[str, int]
    by_subject:         List[Dict[str, Any]]
    top_wrong_questions: List[Dict[str, Any]]
    recent_imports:     int


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _resolve_catalogs(db: Session) -> tuple[dict[UUID, str], dict[UUID, str]]:
    subjects = db.exec(select(Subject)).all()
    levels   = db.exec(select(Level)).all()
    return (
        {s.id: s.name for s in subjects},
        {l.id: l.label for l in levels},
    )


def _question_to_out(
    q: Question,
    choices: list[QuestionChoice],
    sub_map: dict[UUID, str],
    lev_map: dict[UUID, str],
    usage_map: dict[UUID, int],
) -> QuestionOut:
    return QuestionOut(
        id                 = str(q.id),
        subject_id         = str(q.subject_id),
        subject_name       = sub_map.get(q.subject_id, "—"),
        school_level_id    = str(q.school_level_id),
        level_label        = lev_map.get(q.school_level_id, "—"),
        chapter_id         = str(q.chapter_id) if q.chapter_id else None,
        difficulty         = q.difficulty,
        type               = q.type,
        statement          = q.statement,
        explanation        = q.explanation,
        estimated_time_sec = q.estimated_time_sec,
        is_multi_choice    = q.is_multi_choice,
        quality_score      = q.quality_score,
        tags               = list(q.tags or []),
        source             = q.source,
        validation_status  = q.validation_status,
        validated          = q.validated,
        active             = q.active,
        rejection_reason   = q.rejection_reason,
        created_at         = q.created_at.isoformat(),
        updated_at         = q.updated_at.isoformat(),
        deleted_at         = q.deleted_at.isoformat() if q.deleted_at else None,
        choices            = [
            ChoiceOut(id=str(c.id), label=c.label, text=c.text, is_correct=c.is_correct)
            for c in sorted(choices, key=lambda c: c.label)
        ],
        usage_count        = usage_map.get(q.id, 0),
    )


def _set_choices(question_id: UUID, choices_in: list[ChoiceIn], db: Session) -> None:
    existing = db.exec(
        select(QuestionChoice).where(QuestionChoice.question_id == question_id)
    ).all()
    for c in existing:
        db.delete(c)
    for c in choices_in:
        db.add(QuestionChoice(
            question_id=question_id,
            label=c.label,
            text=c.text,
            is_correct=c.is_correct,
        ))


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/", response_model=QuestionListResponse)
async def list_questions(
    page:       int = Query(1, ge=1),
    size:       int = Query(25, ge=1, le=100),
    search:     Optional[str]  = Query(None),
    subject_id: Optional[str]  = Query(None),
    level_id:   Optional[str]  = Query(None),
    difficulty: Optional[str]  = Query(None),
    status:     Optional[str]  = Query(None),
    deleted:    bool           = Query(False),
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Paginated list of questions with full filter support."""
    stmt = select(Question)

    if deleted:
        stmt = stmt.where(Question.deleted_at.is_not(None))
    else:
        stmt = stmt.where(Question.deleted_at.is_(None))

    if subject_id:
        try:
            stmt = stmt.where(Question.subject_id == UUID(subject_id))
        except ValueError:
            pass
    if level_id:
        try:
            stmt = stmt.where(Question.school_level_id == UUID(level_id))
        except ValueError:
            pass
    if difficulty and difficulty in VALID_DIFFICULTIES:
        stmt = stmt.where(Question.difficulty == difficulty)
    if status and status in VALID_STATUSES:
        stmt = stmt.where(Question.validation_status == status)

    all_qs = db.exec(stmt.order_by(Question.created_at.desc())).all()

    if search:
        s = search.lower()
        all_qs = [q for q in all_qs if s in q.statement.lower()]

    total = len(all_qs)
    offset = (page - 1) * size
    page_qs = all_qs[offset: offset + size]

    q_ids = [q.id for q in page_qs]
    choices_all = db.exec(
        select(QuestionChoice).where(QuestionChoice.question_id.in_(q_ids))
    ).all()
    choices_by_q: dict[UUID, list[QuestionChoice]] = {}
    for c in choices_all:
        choices_by_q.setdefault(c.question_id, []).append(c)

    # Usage count: how many times each question appeared in an attempt
    usage_map: dict[UUID, int] = {}
    if q_ids:
        answers = db.exec(
            select(QuizAnswer.question_id, func.count(QuizAnswer.id).label("cnt"))
            .where(QuizAnswer.question_id.in_(q_ids))
            .group_by(QuizAnswer.question_id)
        ).all()
        for row in answers:
            usage_map[row[0]] = row[1]

    sub_map, lev_map = _resolve_catalogs(db)

    return QuestionListResponse(
        items=[
            _question_to_out(q, choices_by_q.get(q.id, []), sub_map, lev_map, usage_map)
            for q in page_qs
        ],
        total       = total,
        page        = page,
        page_size   = size,
        total_pages = math.ceil(total / size) if total else 1,
    )


@router.get("/analytics", response_model=AnalyticsResponse)
async def get_analytics(
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Platform-wide question bank analytics."""
    all_qs = db.exec(select(Question).where(Question.deleted_at.is_(None))).all()

    by_status: dict[str, int] = {}
    by_difficulty: dict[str, int] = {}
    for q in all_qs:
        by_status[q.validation_status] = by_status.get(q.validation_status, 0) + 1
        by_difficulty[q.difficulty]    = by_difficulty.get(q.difficulty, 0) + 1

    # Usage per subject
    subjects = db.exec(select(Subject)).all()
    sub_map  = {s.id: s.name for s in subjects}
    by_subject_map: dict[UUID, int] = {}
    for q in all_qs:
        by_subject_map[q.subject_id] = by_subject_map.get(q.subject_id, 0) + 1
    by_subject = [
        {"subject": sub_map.get(sid, str(sid)), "count": cnt}
        for sid, cnt in sorted(by_subject_map.items(), key=lambda x: -x[1])
    ]

    # Top wrong questions (most answered incorrectly)
    wrong_rows = db.exec(
        select(QuizAnswer.question_id, func.count(QuizAnswer.id).label("cnt"))
        .where(QuizAnswer.is_correct == False)  # noqa: E712
        .group_by(QuizAnswer.question_id)
        .order_by(func.count(QuizAnswer.id).desc())
        .limit(10)
    ).all()
    top_wrong = []
    for qid, cnt in wrong_rows:
        q = db.exec(select(Question).where(Question.id == qid)).first()
        if q:
            top_wrong.append({
                "question_id": str(qid),
                "statement": q.statement[:80],
                "difficulty": q.difficulty,
                "wrong_count": cnt,
            })

    # Recent imports (last 30 days)
    from datetime import timedelta
    since = datetime.utcnow() - timedelta(days=30)
    recent_imports = int(db.exec(
        select(func.count(QuestionImportBatch.id)).where(
            QuestionImportBatch.created_at >= since
        )
    ).one() or 0)

    return AnalyticsResponse(
        total_questions    = len(all_qs),
        by_status          = by_status,
        by_difficulty      = by_difficulty,
        by_subject         = by_subject,
        top_wrong_questions= top_wrong,
        recent_imports     = recent_imports,
    )


@router.get("/import-batches")
async def list_import_batches(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=50),
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """List all past CSV/JSON import jobs."""
    batches = db.exec(
        select(QuestionImportBatch)
        .order_by(QuestionImportBatch.created_at.desc())
    ).all()
    total = len(batches)
    offset = (page - 1) * size
    return {
        "items": [
            {
                "id":             str(b.id),
                "filename":       b.filename,
                "source_format":  b.source_format,
                "status":         b.status,
                "total_rows":     b.total_rows,
                "imported_count": b.imported_count,
                "error_count":    b.error_count,
                "created_at":     b.created_at.isoformat(),
                "completed_at":   b.completed_at.isoformat() if b.completed_at else None,
            }
            for b in batches[offset: offset + size]
        ],
        "total": total,
        "page": page,
    }


@router.get("/{question_id}", response_model=QuestionOut)
async def get_question(
    question_id: str,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    try:
        qid = UUID(question_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="ID invalide.")

    q = db.exec(select(Question).where(Question.id == qid)).first()
    if q is None:
        raise HTTPException(status_code=404, detail="Question introuvable.")

    choices = db.exec(select(QuestionChoice).where(QuestionChoice.question_id == qid)).all()
    sub_map, lev_map = _resolve_catalogs(db)

    usage = int(db.exec(
        select(func.count(QuizAnswer.id)).where(QuizAnswer.question_id == qid)
    ).one() or 0)

    return _question_to_out(q, list(choices), sub_map, lev_map, {qid: usage})


@router.post("/", response_model=QuestionOut, status_code=201)
async def create_question(
    payload: QuestionCreateIn,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"]) if current_user.get("id") else None
    now = datetime.utcnow()

    q = Question(
        subject_id         = UUID(payload.subject_id),
        school_level_id    = UUID(payload.school_level_id),
        chapter_id         = UUID(payload.chapter_id) if payload.chapter_id else None,
        difficulty         = payload.difficulty,
        type               = payload.type,
        statement          = payload.statement,
        explanation        = payload.explanation,
        estimated_time_sec = payload.estimated_time_sec,
        is_multi_choice    = payload.is_multi_choice,
        quality_score      = payload.quality_score,
        tags               = payload.tags or None,
        source             = "manual",
        validated          = False,
        active             = True,
        validation_status  = "pending_review",
        created_by         = uid,
        created_at         = now,
        updated_at         = now,
    )
    db.add(q)
    db.flush()   # get q.id without committing

    _set_choices(q.id, payload.choices, db)
    db.commit()
    db.refresh(q)

    choices = db.exec(select(QuestionChoice).where(QuestionChoice.question_id == q.id)).all()
    sub_map, lev_map = _resolve_catalogs(db)
    return _question_to_out(q, list(choices), sub_map, lev_map, {})


@router.put("/{question_id}", response_model=QuestionOut)
async def update_question(
    question_id: str,
    payload: QuestionUpdateIn,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    try:
        qid = UUID(question_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="ID invalide.")

    q = db.exec(select(Question).where(Question.id == qid)).first()
    if q is None:
        raise HTTPException(status_code=404, detail="Question introuvable.")
    if q.deleted_at is not None:
        raise HTTPException(status_code=422, detail="Impossible de modifier une question supprimée.")

    q.subject_id         = UUID(payload.subject_id)
    q.school_level_id    = UUID(payload.school_level_id)
    q.chapter_id         = UUID(payload.chapter_id) if payload.chapter_id else None
    q.difficulty         = payload.difficulty
    q.type               = payload.type
    q.statement          = payload.statement
    q.explanation        = payload.explanation
    q.estimated_time_sec = payload.estimated_time_sec
    q.is_multi_choice    = payload.is_multi_choice
    q.quality_score      = payload.quality_score
    q.tags               = payload.tags or None
    q.updated_at         = datetime.utcnow()
    # Editing resets validation: a previously-approved question needs re-review
    if q.validation_status == "approved":
        q.validation_status = "pending_review"
        q.validated         = False

    db.add(q)
    _set_choices(q.id, payload.choices, db)
    db.commit()
    db.refresh(q)

    choices = db.exec(select(QuestionChoice).where(QuestionChoice.question_id == qid)).all()
    sub_map, lev_map = _resolve_catalogs(db)
    usage = int(db.exec(
        select(func.count(QuizAnswer.id)).where(QuizAnswer.question_id == qid)
    ).one() or 0)
    return _question_to_out(q, list(choices), sub_map, lev_map, {qid: usage})


@router.delete("/{question_id}", status_code=204)
async def soft_delete_question(
    question_id: str,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    try:
        qid = UUID(question_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="ID invalide.")

    q = db.exec(select(Question).where(Question.id == qid)).first()
    if q is None:
        raise HTTPException(status_code=404, detail="Question introuvable.")

    q.deleted_at = datetime.utcnow()
    q.deleted_by = UUID(current_user["id"]) if current_user.get("id") else None
    q.active     = False
    q.validated  = False
    db.add(q)
    db.commit()


@router.post("/{question_id}/restore", response_model=QuestionOut)
async def restore_question(
    question_id: str,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    try:
        qid = UUID(question_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="ID invalide.")

    q = db.exec(select(Question).where(Question.id == qid)).first()
    if q is None:
        raise HTTPException(status_code=404, detail="Question introuvable.")
    if q.deleted_at is None:
        raise HTTPException(status_code=422, detail="Cette question n'est pas supprimée.")

    q.deleted_at        = None
    q.deleted_by        = None
    q.active            = True
    q.validation_status = "pending_review"
    q.updated_at        = datetime.utcnow()
    db.add(q)
    db.commit()
    db.refresh(q)

    choices = db.exec(select(QuestionChoice).where(QuestionChoice.question_id == qid)).all()
    sub_map, lev_map = _resolve_catalogs(db)
    return _question_to_out(q, list(choices), sub_map, lev_map, {})


@router.post("/{question_id}/validate", response_model=QuestionOut)
async def validate_question(
    question_id: str,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    try:
        qid = UUID(question_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="ID invalide.")

    q = db.exec(select(Question).where(Question.id == qid)).first()
    if q is None:
        raise HTTPException(status_code=404, detail="Question introuvable.")
    if q.deleted_at is not None:
        raise HTTPException(status_code=422, detail="Impossible de valider une question supprimée.")

    now = datetime.utcnow()
    q.validation_status = "approved"
    q.validated         = True
    q.active            = True
    q.reviewed_by       = UUID(current_user["id"]) if current_user.get("id") else None
    q.reviewed_at       = now
    q.rejection_reason  = None
    q.updated_at        = now
    db.add(q)
    db.commit()
    db.refresh(q)

    choices = db.exec(select(QuestionChoice).where(QuestionChoice.question_id == qid)).all()
    sub_map, lev_map = _resolve_catalogs(db)
    return _question_to_out(q, list(choices), sub_map, lev_map, {})


@router.post("/{question_id}/reject", response_model=QuestionOut)
async def reject_question(
    question_id: str,
    payload: RejectIn,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    try:
        qid = UUID(question_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="ID invalide.")

    q = db.exec(select(Question).where(Question.id == qid)).first()
    if q is None:
        raise HTTPException(status_code=404, detail="Question introuvable.")

    now = datetime.utcnow()
    q.validation_status = "rejected"
    q.validated         = False
    q.active            = False
    q.reviewed_by       = UUID(current_user["id"]) if current_user.get("id") else None
    q.reviewed_at       = now
    q.rejection_reason  = payload.reason
    q.updated_at        = now
    db.add(q)
    db.commit()
    db.refresh(q)

    choices = db.exec(select(QuestionChoice).where(QuestionChoice.question_id == qid)).all()
    sub_map, lev_map = _resolve_catalogs(db)
    return _question_to_out(q, list(choices), sub_map, lev_map, {})


# ─── Bulk import ──────────────────────────────────────────────────────────────

_REQUIRED_CSV_COLS = {"statement", "subject_id", "school_level_id", "difficulty",
                      "choice_a", "choice_b", "correct"}


@router.post("/bulk-import")
async def bulk_import(
    file: UploadFile = File(...),
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """
    Import questions from a CSV file.

    Required columns:
      statement, subject_id, school_level_id, difficulty, choice_a, choice_b, correct
    Optional columns:
      choice_c, choice_d, choice_e, explanation, estimated_time_sec,
      is_multi_choice, tags (semicolon-separated), quality_score
    `correct` = comma-separated labels of correct choices, e.g. "A" or "A,C"
    """
    if not file.filename:
        raise HTTPException(status_code=422, detail="Fichier manquant.")
    fname = file.filename.lower()
    fmt = "csv" if fname.endswith(".csv") else "json" if fname.endswith(".json") else "xlsx"

    raw = await file.read()
    uid = UUID(current_user["id"]) if current_user.get("id") else None
    now = datetime.utcnow()

    errors: list[dict] = []
    imported = 0

    try:
        text = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Impossible de lire le fichier : {exc}")

    # Validate headers
    if rows and not _REQUIRED_CSV_COLS.issubset(set(rows[0].keys())):
        missing = _REQUIRED_CSV_COLS - set(rows[0].keys())
        raise HTTPException(
            status_code=422,
            detail=f"Colonnes manquantes : {', '.join(sorted(missing))}",
        )

    batch = QuestionImportBatch(
        created_by    = uid,
        filename      = file.filename or "import.csv",
        source_format = fmt,
        total_rows    = len(rows),
        status        = "processing",
        created_at    = now,
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)

    for i, row in enumerate(rows, start=1):
        try:
            diff = row.get("difficulty", "").strip().lower()
            if diff not in VALID_DIFFICULTIES:
                raise ValueError(f"difficulty invalide : {diff!r}")

            correct_labels = {c.strip().upper() for c in row.get("correct", "").split(",") if c.strip()}
            if not correct_labels:
                raise ValueError("La colonne `correct` est vide")

            choices_in = []
            for label in ["A", "B", "C", "D", "E"]:
                col = f"choice_{label.lower()}"
                text = row.get(col, "").strip()
                if not text:
                    continue
                choices_in.append(ChoiceIn(
                    label=label,
                    text=text,
                    is_correct=(label in correct_labels),
                ))

            if len(choices_in) < 2:
                raise ValueError("Au moins 2 choix requis")

            tags_raw = row.get("tags", "").strip()
            tags = [t.strip() for t in tags_raw.split(";") if t.strip()] if tags_raw else []

            q = Question(
                subject_id         = UUID(row["subject_id"].strip()),
                school_level_id    = UUID(row["school_level_id"].strip()),
                difficulty         = diff,
                statement          = row["statement"].strip(),
                explanation        = row.get("explanation", "").strip(),
                estimated_time_sec = int(row.get("estimated_time_sec", 30) or 30),
                is_multi_choice    = row.get("is_multi_choice", "").strip().lower() in ("true", "1", "yes", "oui"),
                quality_score      = int(row["quality_score"]) if row.get("quality_score", "").strip().isdigit() else None,
                tags               = tags or None,
                source             = "imported",
                validated          = False,
                active             = False,
                validation_status  = "pending_review",
                created_by         = uid,
                created_at         = now,
                updated_at         = now,
            )
            db.add(q)
            db.flush()
            _set_choices(q.id, choices_in, db)
            imported += 1

        except Exception as exc:
            errors.append({"row": i, "reason": str(exc)})

    batch.imported_count = imported
    batch.error_count    = len(errors)
    batch.errors         = errors
    batch.status         = "done" if not errors else ("partial" if imported > 0 else "failed")
    batch.completed_at   = datetime.utcnow()
    db.add(batch)
    db.commit()

    return {
        "batch_id":       str(batch.id),
        "total_rows":     len(rows),
        "imported_count": imported,
        "error_count":    len(errors),
        "status":         batch.status,
        "errors":         errors[:20],  # cap response size
    }
