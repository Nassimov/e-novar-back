from __future__ import annotations

import unicodedata
from typing import Iterable, Optional, Union
from uuid import UUID

from sqlmodel import Session, select

from app.models.catalog import TeacherDeliveryOption
from app.models.profile import Profile, TeacherProfile


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def wilaya_compatible(
    tp: TeacherProfile,
    teacher_profile: Optional[Profile],
    target_wilayas: Union[str, Iterable[str]],
) -> bool:
    """
    Single source of truth for "does this teacher serve this wilaya" — used by
    both search (student_teachers.py) and booking-time re-validation, so the
    two can never drift apart.

    Priority mirrors the teacher's declared reach:
      1. teaching_nationwide → always compatible
      2. wilaya in teaching_wilayas[]
      3. legacy single teaching_wilaya field
      4. fallback to the teacher's own base profile wilaya
    """
    norm_targets = (
        {_norm(target_wilayas)}
        if isinstance(target_wilayas, str)
        else {_norm(w) for w in target_wilayas}
    )
    if not norm_targets:
        return False

    if getattr(tp, "teaching_nationwide", False):
        return True

    tw_arr = getattr(tp, "teaching_wilayas", None) or []
    if tw_arr and any(_norm(w) in norm_targets for w in tw_arr):
        return True

    if _norm(tp.teaching_wilaya or "") in norm_targets:
        return True

    return bool(teacher_profile and _norm(teacher_profile.wilaya or "") in norm_targets)


def delivery_option_compatible(db: Session, teacher_id: UUID, mode: str, session_type: str) -> bool:
    """Does this teacher offer this exact (mode, type) delivery capability?"""
    row = db.exec(
        select(TeacherDeliveryOption).where(
            TeacherDeliveryOption.teacher_id == teacher_id,
            TeacherDeliveryOption.mode == mode,
            TeacherDeliveryOption.type == session_type,
        )
    ).first()
    return row is not None


def _format_matches(student_lesson_format: str, pairs: set[tuple[str, str]]) -> bool:
    if student_lesson_format == "individual_at_teacher":
        return ("at_home", "individual") in pairs
    if student_lesson_format == "individual_at_student":
        return ("at_student", "individual") in pairs
    if student_lesson_format == "group":
        return any(t == "group" for _, t in pairs)
    if student_lesson_format == "online":
        return any(m == "online" for m, _ in pairs)
    return True


def lesson_format_compatible(
    delivery_pairs: Iterable[tuple[str, str]],
    student_lesson_format: Optional[Union[str, Iterable[str]]],
) -> bool:
    """
    Single source of truth for "does this teacher's declared (mode, type)
    capability set satisfy this student's lesson-format preference(s)" — used
    by both search (student_teachers.py) and the recommendation engine, so the
    two can never drift apart.

    student_lesson_format is a MULTI-select (list) of app.models.enums.StudentLessonFormat
    values — a student can want several formats at once, so compatibility is
    OR across the selection: a teacher matching ANY one of the student's
    preferred formats is compatible. (A single string is still accepted for
    backward compatibility with any caller not yet passing a list.)

    Mapping:
      individual_at_teacher -> teacher offers (at_home, individual)
      individual_at_student -> teacher offers (at_student, individual)
      group                 -> teacher offers any (mode, group) pair
      online                -> teacher offers any (online, type) pair

    No preference declared -> always compatible (never penalize/exclude).
    """
    if not student_lesson_format:
        return True

    formats = [student_lesson_format] if isinstance(student_lesson_format, str) else list(student_lesson_format)
    if not formats:
        return True

    pairs = set(delivery_pairs)
    return any(_format_matches(f, pairs) for f in formats)


def wants_online(student_lesson_format: Optional[Union[str, Iterable[str]]]) -> bool:
    """Is 'online' among the student's selected lesson formats?

    This replaces the old standalone `online_only` boolean as the "does this
    student want online lessons" signal — lesson_format is now a multi-select,
    so a student can want online AND e.g. individual_at_student at once,
    which is more expressive than the old exclusive toggle ever was.
    """
    if not student_lesson_format:
        return False
    formats = [student_lesson_format] if isinstance(student_lesson_format, str) else list(student_lesson_format)
    return "online" in formats
