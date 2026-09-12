from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class HomeworkCreate(BaseModel):
    student_id: UUID
    session_id: Optional[UUID] = None
    # Matches public.homeworks: subject_id is an optional FK into the catalog,
    # subject_name is the free-text label actually shown everywhere (see
    # app/routers/student_homework.py's `h.subject_name or subjects_map.get(...)`
    # fallback) — a plain `subject` column has never existed on this table.
    subject_id: Optional[UUID] = None
    subject_name: Optional[str] = None
    title: str
    statement: str
    hints: List[str] = []
    due_at: Optional[datetime] = None
    due_label: Optional[str] = None
    # Sanity ceiling only — the real, admin-configurable cap
    # (PlatformSettings.homework_kp_reward_max, migration 113) is enforced
    # in app/routers/homework.py's create_homework, since a static schema
    # bound can't read DB config.
    kp_reward: int = Field(default=50, ge=0, le=5000)


class HwFileOut(BaseModel):
    name: str
    size: int = 0
    type: str = ""
    url: Optional[str] = None


class HwSubmissionOut(BaseModel):
    text: Optional[str] = None
    files: List[HwFileOut] = []
    submitted_at: datetime


class HwGradeOut(BaseModel):
    score: float
    feedback: Optional[str] = None
    files: List[HwFileOut] = []
    kp_awarded: int = 0
    graded_at: datetime


class HomeworkResponse(BaseModel):
    id: UUID
    teacher_id: UUID
    student_id: UUID
    student_name: Optional[str] = None
    session_id: Optional[UUID] = None
    subject_name: Optional[str] = None
    title: str
    statement: str
    hints: List[str] = []
    due_at: Optional[datetime] = None
    due_label: Optional[str] = None
    status: str
    kp_reward: int
    created_at: datetime
    updated_at: Optional[datetime] = None
    submission: Optional[HwSubmissionOut] = None
    grade: Optional[HwGradeOut] = None

    model_config = {"from_attributes": True}


class HomeworkSubmitRequest(BaseModel):
    text: str
    attachment_url: Optional[str] = None


class HwFileIn(BaseModel):
    name: str
    size: int = 0
    type: str = ""
    url: Optional[str] = None


class HomeworkGradeRequest(BaseModel):
    score: float = Field(ge=0.0, le=20.0)
    feedback: Optional[str] = None
    kp_awarded: Optional[int] = Field(default=None, ge=0)  # if None, use homework.kp_reward
    files: List[HwFileIn] = []
