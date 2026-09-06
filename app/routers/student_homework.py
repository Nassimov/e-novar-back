from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.catalog import Subject
from app.models.homework import Homework, HomeworkGrade, HomeworkSubmission
from app.models.profile import Profile

router = APIRouter(tags=["Student"])

MONTHS_FR = ["Jan", "Fév", "Mars", "Avr", "Mai", "Juin",
             "Juil", "Août", "Sept", "Oct", "Nov", "Déc"]


# ── Response schemas ──────────────────────────────────────────────────────────

class HwFile(BaseModel):
    name: str
    size: int = 0
    type: str = ""
    url: Optional[str] = None


class HwSubmission(BaseModel):
    text: Optional[str] = None
    files: List[HwFile] = []
    submitted_at: str


class HwGrade(BaseModel):
    score: float
    feedback: Optional[str] = None
    files: List[HwFile] = []
    kp_awarded: int = 0
    graded_at: str


class HomeworkListItem(BaseModel):
    id: str
    title: str
    subject: str
    teacher_name: str
    teacher_id: str
    statement: str
    hints: List[str] = []
    hints_checked: List[bool] = []
    attachments: List[HwFile] = []
    due_at: Optional[str] = None
    due_label: Optional[str] = None
    kp_reward: int = 50
    status: str
    created_at: str
    submission: Optional[HwSubmission] = None
    grade: Optional[HwGrade] = None


class HomeworkListResponse(BaseModel):
    items: List[HomeworkListItem]
    todo_count: int
    submitted_count: int
    graded_count: int


class ChartPoint(BaseModel):
    date: str
    teacher: str
    score: float


class HomeworkStatsResponse(BaseModel):
    has_data: bool
    monthly_avg: List[ChartPoint]
    hw_grades: List[ChartPoint]
    teachers: List[str]


class SubmitRequest(BaseModel):
    text: Optional[str] = None
    files: List[HwFile] = []


class HintsRequest(BaseModel):
    hints_checked: List[bool]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_files(raw: Any) -> List[HwFile]:
    if not raw:
        return []
    try:
        return [HwFile(**f) if isinstance(f, dict) else f for f in raw]
    except Exception:
        return []


def _due_label(due_at: Optional[datetime]) -> Optional[str]:
    if not due_at:
        return None
    now = datetime.utcnow()
    delta = (due_at.date() - now.date()).days
    if delta < 0:
        return "En retard"
    if delta == 0:
        return "À rendre aujourd'hui"
    if delta == 1:
        return "À rendre demain"
    return f"Dans {delta} jours"


# ── List homework ─────────────────────────────────────────────────────────────

@router.get("/homework", response_model=HomeworkListResponse)
def student_homework_list(
    status: Optional[str] = Query(None, pattern="^(todo|submitted|graded)$"),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])

    # All homework for this student, newest first
    base_query = (
        select(Homework)
        .where(Homework.student_id == uid)
        .order_by(Homework.created_at.desc())
    )
    if status:
        base_query = base_query.where(Homework.status == status)

    all_hw = db.exec(base_query).all()

    # Counts (always across all statuses)
    if status:
        counts_query = select(Homework).where(Homework.student_id == uid)
        all_for_counts = db.exec(counts_query).all()
    else:
        all_for_counts = all_hw

    todo_count = sum(1 for h in all_for_counts if h.status == "todo")
    submitted_count = sum(1 for h in all_for_counts if h.status == "submitted")
    graded_count = sum(1 for h in all_for_counts if h.status == "graded")

    if not all_hw:
        return HomeworkListResponse(
            items=[], todo_count=todo_count,
            submitted_count=submitted_count, graded_count=graded_count,
        )

    # Batch-load teacher profiles
    teacher_ids = list({h.teacher_id for h in all_hw})
    teachers_map: dict[UUID, Profile] = {}
    if teacher_ids:
        tps = db.exec(select(Profile).where(Profile.id.in_(teacher_ids))).all()
        teachers_map = {p.id: p for p in tps}

    # Batch-load subject names from catalog (for items that have subject_id)
    sub_ids = list({h.subject_id for h in all_hw if h.subject_id})
    subjects_map: dict[UUID, str] = {}
    if sub_ids:
        subs = db.exec(select(Subject).where(Subject.id.in_(sub_ids))).all()
        subjects_map = {s.id: s.name for s in subs}

    # Batch-load submissions
    hw_ids = [h.id for h in all_hw]
    submissions_map: dict[UUID, HomeworkSubmission] = {}
    if hw_ids:
        subs_q = db.exec(select(HomeworkSubmission).where(HomeworkSubmission.homework_id.in_(hw_ids))).all()
        submissions_map = {s.homework_id: s for s in subs_q}

    # Batch-load grades
    grades_map: dict[UUID, HomeworkGrade] = {}
    if hw_ids:
        grades_q = db.exec(select(HomeworkGrade).where(HomeworkGrade.homework_id.in_(hw_ids))).all()
        grades_map = {g.homework_id: g for g in grades_q}

    items: list[HomeworkListItem] = []
    for h in all_hw:
        tp = teachers_map.get(h.teacher_id)
        teacher_name = (tp.full_name or "Enseignant") if tp else "Enseignant"

        subject = (
            h.subject_name
            or (subjects_map.get(h.subject_id) if h.subject_id else None)
            or "Matière"
        )

        hints = h.hints or []
        hints_checked = h.hints_checked or [False] * len(hints)

        submission_data: Optional[HwSubmission] = None
        sub = submissions_map.get(h.id)
        if sub:
            submission_data = HwSubmission(
                text=sub.text,
                files=_parse_files(sub.files),
                submitted_at=sub.submitted_at.isoformat(),
            )

        grade_data: Optional[HwGrade] = None
        grade = grades_map.get(h.id)
        if grade:
            grade_data = HwGrade(
                score=grade.score,
                feedback=grade.feedback,
                files=_parse_files(grade.files),
                kp_awarded=grade.kp_awarded,
                graded_at=grade.graded_at.isoformat(),
            )

        due_label = h.due_label or _due_label(h.due_at)

        items.append(HomeworkListItem(
            id=str(h.id),
            title=h.title,
            subject=subject,
            teacher_name=teacher_name,
            teacher_id=str(h.teacher_id),
            statement=h.statement,
            hints=hints,
            hints_checked=hints_checked,
            attachments=_parse_files(h.attachments),
            due_at=h.due_at.isoformat() if h.due_at else None,
            due_label=due_label,
            kp_reward=h.kp_reward,
            status=h.status,
            created_at=h.created_at.isoformat(),
            submission=submission_data,
            grade=grade_data,
        ))

    return HomeworkListResponse(
        items=items,
        todo_count=todo_count,
        submitted_count=submitted_count,
        graded_count=graded_count,
    )


# ── Stats / chart data ────────────────────────────────────────────────────────

@router.get("/homework/stats", response_model=HomeworkStatsResponse)
def student_homework_stats(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])

    # All graded homework for this student
    graded_hw = db.exec(
        select(Homework)
        .where(Homework.student_id == uid)
        .where(Homework.status == "graded")
        .order_by(Homework.created_at)
    ).all()

    if not graded_hw:
        return HomeworkStatsResponse(has_data=False, monthly_avg=[], hw_grades=[], teachers=[])

    hw_ids = [h.id for h in graded_hw]
    grades_q = db.exec(select(HomeworkGrade).where(HomeworkGrade.homework_id.in_(hw_ids))).all()
    grades_map: dict[UUID, HomeworkGrade] = {g.homework_id: g for g in grades_q}

    teacher_ids = list({h.teacher_id for h in graded_hw})
    tps = db.exec(select(Profile).where(Profile.id.in_(teacher_ids))).all()
    teachers_map: dict[UUID, str] = {p.id: (p.full_name or "Enseignant") for p in tps}

    # hw_grades — one point per graded homework, chronologically
    hw_grades: list[ChartPoint] = []
    for i, h in enumerate(graded_hw, 1):
        g = grades_map.get(h.id)
        if g:
            hw_grades.append(ChartPoint(
                date=f"DV{i}",
                teacher=teachers_map.get(h.teacher_id, "Enseignant"),
                score=round(g.score, 1),
            ))

    # monthly_avg — average score per calendar month
    monthly_buckets: dict[str, list[float]] = defaultdict(list)
    monthly_teacher: dict[str, str] = {}
    for h in graded_hw:
        g = grades_map.get(h.id)
        if not g:
            continue
        ref_date = g.graded_at
        month_key = f"{ref_date.year}-{ref_date.month:02d}"
        month_label = f"{MONTHS_FR[ref_date.month - 1]} {ref_date.year % 100:02d}"
        monthly_buckets[month_key].append(g.score)
        monthly_teacher.setdefault(month_key, teachers_map.get(h.teacher_id, "Enseignant"))

    monthly_avg: list[ChartPoint] = []
    for key in sorted(monthly_buckets.keys()):
        scores = monthly_buckets[key]
        ref_month = int(key.split("-")[1])
        ref_year = int(key.split("-")[0])
        monthly_avg.append(ChartPoint(
            date=f"{MONTHS_FR[ref_month - 1]} {ref_year % 100:02d}",
            teacher=monthly_teacher[key],
            score=round(sum(scores) / len(scores), 1),
        ))

    teachers = sorted({t for t in teachers_map.values()})

    return HomeworkStatsResponse(
        has_data=True,
        monthly_avg=monthly_avg,
        hw_grades=hw_grades,
        teachers=teachers,
    )


# ── Submit homework ───────────────────────────────────────────────────────────

@router.post("/homework/{homework_id}/submit")
def student_submit_homework(
    homework_id: UUID,
    payload: SubmitRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])

    hw = db.get(Homework, homework_id)
    if hw is None:
        raise HTTPException(status_code=404, detail="Devoir introuvable")
    if hw.student_id != uid:
        raise HTTPException(status_code=403, detail="Accès refusé")
    if hw.status != "todo":
        raise HTTPException(status_code=400, detail="Ce devoir a déjà été soumis")

    if not payload.text and not payload.files:
        raise HTTPException(status_code=422, detail="Ajoute du texte ou un fichier")

    files_json = [f.model_dump(exclude_none=True) for f in payload.files]

    existing_sub = db.exec(
        select(HomeworkSubmission).where(HomeworkSubmission.homework_id == homework_id)
    ).first()
    if existing_sub:
        raise HTTPException(status_code=400, detail="Ce devoir a déjà été soumis")

    sub = HomeworkSubmission(
        homework_id=homework_id,
        student_id=uid,
        text=payload.text,
        files=files_json,
    )
    db.add(sub)

    hw.status = "submitted"
    hw.updated_at = datetime.utcnow()
    db.add(hw)
    db.commit()

    return {"message": "Devoir soumis avec succès"}


# ── Update hints_checked ──────────────────────────────────────────────────────

@router.patch("/homework/{homework_id}/hints")
def update_hints_checked(
    homework_id: UUID,
    payload: HintsRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])

    hw = db.get(Homework, homework_id)
    if hw is None:
        raise HTTPException(status_code=404, detail="Devoir introuvable")
    if hw.student_id != uid:
        raise HTTPException(status_code=403, detail="Accès refusé")

    hw.hints_checked = payload.hints_checked
    hw.updated_at = datetime.utcnow()
    db.add(hw)
    db.commit()

    return {"message": "OK"}
