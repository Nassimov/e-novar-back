from __future__ import annotations

import unicodedata
from typing import Dict, List, Optional, Set, Tuple
from uuid import UUID

from sqlmodel import Session, select

from app.models.catalog import Subject, TeacherDeliveryOption, TeacherSubjectPrice
from app.models.profile import Profile, StudentProfile, TeacherProfile
from app.models.scheduling import TeacherSlot, TeacherSlotSubject
from app.services import matching

# ── Scoring weights ────────────────────────────────────────────────────────────
# Not scientifically tuned — reasonable and monotonic in the right direction.
# Subject match is the heaviest signal (the single most important criterion);
# quality (rating/verified/sponsored) is a light tiebreaker, not a primary driver.
_W_SUBJECT = 10.0
_W_FORMAT = 4.0
_W_WILAYA = 4.0
_W_LANGUAGE = 2.0
_W_BUDGET = 3.0
_W_BUDGET_OVER_PENALTY = 1.5
_W_AVAILABILITY = 2.0
_W_GOALS = 1.0
_W_QUALITY = 2.0
# Small refinement bonus on top of _W_SUBJECT: the teacher doesn't just claim
# to teach this subject (catalog match), they have an actual open, bookable
# slot for it right now. Bonus-only — never a penalty, and never gates
# eligibility, same as the general availability signal above.
_W_SLOT_SUBJECT_MATCH = 1.5

# Mirrors the 4 fixed availability blocks defined in
# student.onboarding.availability.tsx (frontend) — keep these hour ranges in sync.
_BLOCK_RANGES: Dict[str, Tuple[int, int]] = {
    "morning": (8, 12),
    "noon": (12, 14),
    "afternoon": (14, 18),
    "evening": (18, 22),
}


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def _block_for_hour(hour: int) -> Optional[str]:
    for block, (start, end) in _BLOCK_RANGES.items():
        if start <= hour < end:
            return block
    return None


def _parse_student_slot_keys(available_slots: Optional[List[str]]) -> Set[Tuple[int, str]]:
    """Parse 'day:block' composite strings into (day_index, block) tuples."""
    keys: Set[Tuple[int, str]] = set()
    for s in available_slots or []:
        try:
            day_str, block = s.split(":", 1)
            keys.add((int(day_str), block))
        except (ValueError, AttributeError):
            continue
    return keys


def _slot_keys_for_teacher(db: Session, teacher_id: UUID) -> Set[Tuple[int, str]]:
    """Derive (day_of_week, block) keys from a teacher's own open TeacherSlot rows."""
    slots = db.exec(
        select(TeacherSlot).where(
            TeacherSlot.teacher_id == teacher_id,
            TeacherSlot.status == "open",
        )
    ).all()
    keys: Set[Tuple[int, str]] = set()
    for slot in slots:
        block = _block_for_hour(slot.start_time.hour)
        if block:
            keys.add((slot.slot_date.weekday(), block))
    return keys


def score_teacher(
    student: StudentProfile,
    teacher: TeacherProfile,
    teacher_base_profile: Optional[Profile],
    teacher_subject_ids: Set[UUID],
    delivery_pairs: Set[Tuple[str, str]],
    teacher_min_price: Optional[int],
    student_subject_ids: Set[UUID],
    student_slot_keys: Set[Tuple[int, str]],
    teacher_slot_keys: Set[Tuple[int, str]],
    teacher_slot_subject_ids: Optional[Set[UUID]] = None,
) -> float:
    """
    Best-overall-compatibility score for ranking (not filtering) a teacher
    against a student's preferences. Every signal below is additive/ranking
    only — nothing here excludes a teacher, including having zero declared
    schedule slots (which is treated as neutral, never a penalty).
    """
    score = 0.0

    # Subjects — heaviest signal.
    if student_subject_ids and teacher_subject_ids:
        overlap = len(student_subject_ids & teacher_subject_ids)
        if overlap:
            score += _W_SUBJECT * (overlap / len(student_subject_ids))

    # Slot-level subject match — refinement on top of the catalog match above:
    # the teacher has an actual open, bookable slot for one of the requested
    # subjects (from teacher_slot_subjects), not just a declared capability.
    if student_subject_ids and teacher_slot_subject_ids:
        if student_subject_ids & teacher_slot_subject_ids:
            score += _W_SLOT_SUBJECT_MATCH

    # Goals — light free-text keyword heuristic against the teacher's bio/headline.
    if student.goals:
        haystack = _norm(f"{teacher.headline or ''} {teacher.bio_long or ''}")
        hits = sum(1 for g in student.goals if g and _norm(g) in haystack)
        if hits:
            score += _W_GOALS * min(hits, 3)

    # Recurring availability overlap — neutral (not negative) if the teacher has
    # no declared schedule at all; a real overlap is a bonus, a real non-overlap
    # (teacher HAS slots, just none matching) is simply no bonus, never a penalty.
    if teacher_slot_keys and student_slot_keys:
        if student_slot_keys & teacher_slot_keys:
            score += _W_AVAILABILITY

    # Budget — reward within-budget, mildly discount (never exclude) over-budget.
    if teacher_min_price is not None and student.budget_max:
        if teacher_min_price <= student.budget_max:
            score += _W_BUDGET
        else:
            score -= _W_BUDGET_OVER_PENALTY

    # Lesson format.
    if matching.lesson_format_compatible(delivery_pairs, student.lesson_format):
        score += _W_FORMAT

    # Wilaya / online preference.
    if student.online_only:
        if any(mode == "online" for mode, _ in delivery_pairs):
            score += _W_WILAYA
    elif student.wilaya and matching.wilaya_compatible(teacher, teacher_base_profile, student.wilaya):
        score += _W_WILAYA

    # Spoken languages overlap.
    if student.languages and teacher.languages:
        if {_norm(l) for l in student.languages} & {_norm(l) for l in teacher.languages}:
            score += _W_LANGUAGE

    # Quality tiebreaker — small relative to compatibility signals above.
    if teacher.reviews_count:
        score += _W_QUALITY * (teacher.rating_avg * min(teacher.reviews_count, 20) / 20)
    if teacher.verified:
        score += 0.5
    if teacher.sponsored:
        score += 0.5

    return score


def rank_teachers(db: Session, student_id: UUID, limit: int = 3) -> List[TeacherProfile]:
    """
    Rank approved teachers by best-overall-compatibility with this student's
    preferences (subjects, goals, recurring availability, budget, lesson
    format, wilaya/online, spoken languages). Never hard-excludes a teacher
    for lacking declared schedule slots — availability only ever improves
    ranking, it does not gate eligibility.
    """
    student = db.get(StudentProfile, student_id)
    if student is None:
        return []

    candidates = db.exec(select(TeacherProfile).where(TeacherProfile.status == "approved")).all()
    if not candidates:
        return []

    teacher_ids = [tp.user_id for tp in candidates]

    base_profiles = db.exec(select(Profile).where(Profile.id.in_(teacher_ids))).all()
    base_profile_map: Dict[UUID, Profile] = {p.id: p for p in base_profiles}

    subject_rows = db.exec(
        select(TeacherSubjectPrice).where(
            TeacherSubjectPrice.teacher_id.in_(teacher_ids),
            TeacherSubjectPrice.active == True,  # noqa: E712
        )
    ).all()
    teacher_subject_ids: Dict[UUID, Set[UUID]] = {}
    teacher_min_price: Dict[UUID, int] = {}
    for r in subject_rows:
        teacher_subject_ids.setdefault(r.teacher_id, set()).add(r.subject_id)
        if r.price_single > 0:
            cur = teacher_min_price.get(r.teacher_id)
            if cur is None or r.price_single < cur:
                teacher_min_price[r.teacher_id] = r.price_single

    delivery_rows = db.exec(
        select(TeacherDeliveryOption).where(TeacherDeliveryOption.teacher_id.in_(teacher_ids))
    ).all()
    teacher_delivery_pairs: Dict[UUID, Set[Tuple[str, str]]] = {}
    for d in delivery_rows:
        teacher_delivery_pairs.setdefault(d.teacher_id, set()).add((d.mode, d.type))

    # Subjects with an actual open slot right now (teacher_slot_subjects),
    # bonus-only refinement on top of the general catalog match.
    slot_subject_rows = db.exec(
        select(TeacherSlotSubject.subject_id, TeacherSlot.teacher_id)
        .join(TeacherSlot, TeacherSlot.id == TeacherSlotSubject.slot_id)
        .where(TeacherSlot.teacher_id.in_(teacher_ids), TeacherSlot.status == "open")
    ).all()
    teacher_slot_subject_ids: Dict[UUID, Set[UUID]] = {}
    for subject_id, teacher_id in slot_subject_rows:
        teacher_slot_subject_ids.setdefault(teacher_id, set()).add(subject_id)

    # Resolve the student's subjects_interested (stored as names) to Subject IDs.
    student_subject_ids: Set[UUID] = set()
    if student.subjects_interested:
        wanted = {_norm(s) for s in student.subjects_interested}
        all_subjects = db.exec(select(Subject)).all()
        student_subject_ids = {s.id for s in all_subjects if _norm(s.name) in wanted or _norm(s.slug) in wanted}

    student_slot_keys = _parse_student_slot_keys(student.available_slots)

    scored: List[Tuple[float, TeacherProfile]] = []
    for tp in candidates:
        teacher_slot_keys = _slot_keys_for_teacher(db, tp.user_id) if student_slot_keys else set()
        s = score_teacher(
            student=student,
            teacher=tp,
            teacher_base_profile=base_profile_map.get(tp.user_id),
            teacher_subject_ids=teacher_subject_ids.get(tp.user_id, set()),
            delivery_pairs=teacher_delivery_pairs.get(tp.user_id, set()),
            teacher_min_price=teacher_min_price.get(tp.user_id),
            student_subject_ids=student_subject_ids,
            student_slot_keys=student_slot_keys,
            teacher_slot_keys=teacher_slot_keys,
            teacher_slot_subject_ids=teacher_slot_subject_ids.get(tp.user_id),
        )
        scored.append((s, tp))

    scored.sort(key=lambda pair: (1 if pair[1].sponsored else 0, pair[0]), reverse=True)
    return [tp for _, tp in scored[:limit]]


def min_price_for_teachers(db: Session, teacher_ids: List[UUID]) -> Dict[UUID, int]:
    """Shared helper: min active TeacherSubjectPrice.price_single per teacher."""
    if not teacher_ids:
        return {}
    rows = db.exec(
        select(TeacherSubjectPrice).where(
            TeacherSubjectPrice.teacher_id.in_(teacher_ids),
            TeacherSubjectPrice.active == True,  # noqa: E712
        )
    ).all()
    result: Dict[UUID, int] = {}
    for r in rows:
        if r.price_single > 0:
            cur = result.get(r.teacher_id)
            if cur is None or r.price_single < cur:
                result[r.teacher_id] = r.price_single
    return result
