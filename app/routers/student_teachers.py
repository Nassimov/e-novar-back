from __future__ import annotations

import unicodedata
from datetime import date as dt_date, date
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.catalog import Level, Subject, TeacherDiploma, TeacherMode, TeacherSessionType, TeacherSubjectPrice
from app.models.profile import Profile, StudentProfile, TeacherProfile
from app.models.review import Review
from app.models.scheduling import TeacherSlot

router = APIRouter(tags=["Student"])

# LevelGroup ID → list of Level.code values that belong to that group
LEVEL_GROUP_CODES: dict[str, list[str]] = {
    "primary":    ["Primaire"],
    "middle":     ["BEM", "CEM", "3ème", "Moyen"],
    "high":       ["1AS", "2AS", "3AS", "BAC", "Terminale", "Bac"],
    "university": ["Université", "Licence", "Master", "Doctorat", "Univ"],
}


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


class TeacherSearchItem(BaseModel):
    id: str
    first_name: str
    last_name: str
    full_name: str
    avatar_url: Optional[str] = None
    wilaya: Optional[str] = None
    bio: Optional[str] = None
    headline: Optional[str] = None
    price_per_session: int
    rating_avg: float
    reviews_count: int
    verified: bool
    sponsored: bool
    badge: Optional[str] = None
    kp_reward: int
    subjects: List[str]
    levels: List[str]
    modes: List[str]
    session_types: List[str]
    lesson_formats: List[str]  # lesson formats offered across all subjects (flat)
    subject_formats: Dict[str, str]  # subject_name → dominant lesson format
    languages: List[str] = []


class TeacherSearchResponse(BaseModel):
    items: List[TeacherSearchItem]
    total: int


@router.get("/teachers/search", response_model=TeacherSearchResponse)
async def student_teachers_search(
    q: Optional[str] = Query(None),
    subject: Optional[str] = Query(None),
    wilaya: Optional[str] = Query(None),
    level: Optional[str] = Query(None),
    mode: Optional[str] = Query(None),
    session_type: Optional[str] = Query(None),
    price_min: Optional[int] = Query(None),
    price_max: Optional[int] = Query(None),
    min_rating: Optional[float] = Query(None),
    language: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),   # ISO "YYYY-MM-DD"
    date_to:   Optional[str] = Query(None),   # ISO "YYYY-MM-DD"
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Parse comma-separated multi-value params
    wilaya_list: list[str] = [w.strip() for w in (wilaya or "").split(",") if w.strip()]
    level_list: list[str] = [l.strip() for l in (level or "").split(",") if l.strip()]
    language_list: list[str] = [l.strip().lower() for l in (language or "").split(",") if l.strip()]

    # 0. Load requesting student's lesson format preference (optional — non-fatal)
    uid = UUID(current_user["id"])
    student_lesson_format: Optional[str] = None
    try:
        sp_student = db.exec(
            select(StudentProfile).where(StudentProfile.user_id == uid)
        ).first()
        if sp_student:
            student_lesson_format = sp_student.lesson_format
    except Exception:
        pass

    # 1. All approved teachers
    all_tp = db.exec(
        select(TeacherProfile).where(TeacherProfile.status == "approved")
    ).all()
    if not all_tp:
        return TeacherSearchResponse(items=[], total=0)

    teacher_ids: list[UUID] = [tp.user_id for tp in all_tp]
    tp_map: dict[UUID, TeacherProfile] = {tp.user_id: tp for tp in all_tp}

    # 2. Mode filter
    if mode:
        mode_rows = db.exec(
            select(TeacherMode).where(
                TeacherMode.teacher_id.in_(teacher_ids),
                TeacherMode.mode == mode,
            )
        ).all()
        mode_set = {r.teacher_id for r in mode_rows}
        teacher_ids = [tid for tid in teacher_ids if tid in mode_set]
        if not teacher_ids:
            return TeacherSearchResponse(items=[], total=0)

    # 3. Session type filter
    if session_type:
        st_rows = db.exec(
            select(TeacherSessionType).where(
                TeacherSessionType.teacher_id.in_(teacher_ids),
                TeacherSessionType.type == session_type,
            )
        ).all()
        st_set = {r.teacher_id for r in st_rows}
        teacher_ids = [tid for tid in teacher_ids if tid in st_set]
        if not teacher_ids:
            return TeacherSearchResponse(items=[], total=0)

    # 4. Batch-load subject/level data (needed for filtering and response)
    sp_all = db.exec(
        select(TeacherSubjectPrice).where(
            TeacherSubjectPrice.teacher_id.in_(teacher_ids),
            TeacherSubjectPrice.active == True,  # noqa: E712
        )
    ).all()

    all_sub_ids = list({r.subject_id for r in sp_all})
    all_lev_ids = list({r.level_id for r in sp_all})

    sub_name_map: dict[UUID, str] = {}
    lev_code_map: dict[UUID, str] = {}
    lev_label_map: dict[UUID, str] = {}

    if all_sub_ids:
        subs = db.exec(select(Subject).where(Subject.id.in_(all_sub_ids))).all()
        sub_name_map = {s.id: s.name for s in subs}

    if all_lev_ids:
        levs = db.exec(select(Level).where(Level.id.in_(all_lev_ids))).all()
        lev_code_map = {l.id: l.code for l in levs}
        lev_label_map = {l.id: l.label for l in levs}

    def _dominant_format(fmts: set[str]) -> str:
        """Collapse a set of lesson_format values to a single representative string."""
        clean = fmts - {"both"}
        if not clean or "both" in fmts:
            return "both"
        if clean == {"individual"}:
            return "individual"
        if clean == {"group"}:
            return "group"
        return "both"  # mixed individual+group → treat as both

    # Build per-teacher dicts (unique ordered lists) + minimum price from subject prices
    teacher_subjects: dict[UUID, list[str]] = {}
    teacher_level_codes: dict[UUID, list[str]] = {}
    teacher_level_labels: dict[UUID, list[str]] = {}
    teacher_min_price: dict[UUID, int] = {}
    teacher_lesson_formats: dict[UUID, set[str]] = {}
    # subject_name → set of lesson_format values, per teacher
    teacher_subject_formats: dict[UUID, dict[str, set[str]]] = {}

    for r in sp_all:
        tid = r.teacher_id
        sn = sub_name_map.get(r.subject_id)
        lc = lev_code_map.get(r.level_id)
        ll = lev_label_map.get(r.level_id)
        if sn and sn not in teacher_subjects.get(tid, []):
            teacher_subjects.setdefault(tid, []).append(sn)
        if lc and lc not in teacher_level_codes.get(tid, []):
            teacher_level_codes.setdefault(tid, []).append(lc)
        if ll and ll not in teacher_level_labels.get(tid, []):
            teacher_level_labels.setdefault(tid, []).append(ll)
        # Track minimum price_single across all subject/level combos
        if r.price_single > 0:
            if tid not in teacher_min_price or r.price_single < teacher_min_price[tid]:
                teacher_min_price[tid] = r.price_single
        # Collect lesson formats — flat set and per-subject set
        fmt = getattr(r, "lesson_format", "both") or "both"
        teacher_lesson_formats.setdefault(tid, set()).add(fmt)
        if sn:
            teacher_subject_formats.setdefault(tid, {}).setdefault(sn, set()).add(fmt)

    # 5. Subject filter — comma-separated list, OR logic (teacher matches any requested subject)
    if subject:
        subject_list = [s.strip() for s in subject.split(",") if s.strip()]
        if subject_list:
            norm_subs = {_norm(s) for s in subject_list}
            teacher_ids = [
                tid for tid in teacher_ids
                if any(_norm(s) in norm_subs for s in teacher_subjects.get(tid, []))
            ]
            if not teacher_ids:
                return TeacherSearchResponse(items=[], total=0)

    # 6. Level group filter (supports multiple comma-separated levels)
    if level_list:
        norm_codes: set[str] = set()
        for lv in level_list:
            for code in LEVEL_GROUP_CODES.get(lv, []):
                norm_codes.add(_norm(code))
        teacher_ids = [
            tid for tid in teacher_ids
            if any(_norm(c) in norm_codes for c in teacher_level_codes.get(tid, []))
        ]
        if not teacher_ids:
            return TeacherSearchResponse(items=[], total=0)

    # 7. Load profiles for remaining teachers
    profiles = db.exec(
        select(Profile).where(Profile.id.in_(teacher_ids))
    ).all()
    prof_map: dict[UUID, Profile] = {p.id: p for p in profiles}

    # 8. Wilaya filter — multi-wilaya: teacher matches if any requested wilaya is covered
    if wilaya_list:
        norm_wilayas = {_norm(w) for w in wilaya_list}

        def _teacher_serves_any_wilaya(tid: UUID) -> bool:
            tp = tp_map[tid]
            # Nationwide coverage (at_student with no wilaya restriction)
            if getattr(tp, "teaching_nationwide", False):
                return True
            # at_student: check multi-wilaya array
            tw_arr = getattr(tp, "teaching_wilayas", None) or []
            if tw_arr:
                if any(_norm(w) in norm_wilayas for w in tw_arr):
                    return True
            # at_home / legacy: single teaching_wilaya field
            if _norm(tp.teaching_wilaya or "") in norm_wilayas:
                return True
            # Profile home wilaya
            p = prof_map.get(tid)
            if p and _norm(p.wilaya or "") in norm_wilayas:
                return True
            return False

        teacher_ids = [tid for tid in teacher_ids if _teacher_serves_any_wilaya(tid)]
        if not teacher_ids:
            return TeacherSearchResponse(items=[], total=0)

    # 9. Price filter
    if price_min is not None:
        teacher_ids = [
            tid for tid in teacher_ids
            if tp_map[tid].price_per_session >= price_min
        ]
    if price_max is not None:
        teacher_ids = [
            tid for tid in teacher_ids
            if tp_map[tid].price_per_session <= price_max
        ]
    if not teacher_ids:
        return TeacherSearchResponse(items=[], total=0)

    # 10. Min-rating filter
    if min_rating is not None and min_rating > 0:
        teacher_ids = [
            tid for tid in teacher_ids
            if tp_map[tid].rating_avg >= min_rating
        ]
        if not teacher_ids:
            return TeacherSearchResponse(items=[], total=0)

    # 10b. Language filter
    if language_list:
        teacher_ids = [
            tid for tid in teacher_ids
            if any(
                lang in (getattr(tp_map[tid], "languages", None) or [])
                for lang in language_list
            )
        ]
        if not teacher_ids:
            return TeacherSearchResponse(items=[], total=0)

    # 10c. Date availability filter
    if date_from or date_to:
        from app.models.scheduling import TeacherSlot
        try:
            slot_query = select(TeacherSlot).where(
                TeacherSlot.teacher_id.in_(teacher_ids),
                TeacherSlot.status == "open",
            )
            if date_from:
                try:
                    slot_query = slot_query.where(TeacherSlot.slot_date >= dt_date.fromisoformat(date_from))
                except ValueError:
                    pass
            if date_to:
                try:
                    slot_query = slot_query.where(TeacherSlot.slot_date <= dt_date.fromisoformat(date_to))
                except ValueError:
                    pass
            open_slots = db.exec(slot_query).all()
            teachers_with_slots = {s.teacher_id for s in open_slots}
            if teachers_with_slots:
                # Only filter if some teachers have slots — graceful fallback if table is empty
                teacher_ids = [tid for tid in teacher_ids if tid in teachers_with_slots]
                if not teacher_ids:
                    return TeacherSearchResponse(items=[], total=0)
        except Exception:
            pass  # graceful: date filter is best-effort

    # 11. Free-text query filter (name, headline, or subject match)
    if q and q.strip():
        norm_q = _norm(q.strip())

        def q_matches(tid: UUID) -> bool:
            p = prof_map.get(tid)
            tp = tp_map.get(tid)
            if p and norm_q in _norm(p.full_name or ""):
                return True
            if tp and tp.headline and norm_q in _norm(tp.headline):
                return True
            return any(norm_q in _norm(s) for s in teacher_subjects.get(tid, []))

        teacher_ids = [tid for tid in teacher_ids if q_matches(tid)]
        if not teacher_ids:
            return TeacherSearchResponse(items=[], total=0)

    # 12. Batch-load modes and session types for response
    modes_all = db.exec(
        select(TeacherMode).where(TeacherMode.teacher_id.in_(teacher_ids))
    ).all()
    teacher_modes: dict[UUID, list[str]] = {}
    for m in modes_all:
        teacher_modes.setdefault(m.teacher_id, []).append(m.mode)

    stypes_all = db.exec(
        select(TeacherSessionType).where(TeacherSessionType.teacher_id.in_(teacher_ids))
    ).all()
    teacher_stypes: dict[UUID, list[str]] = {}
    for st in stypes_all:
        teacher_stypes.setdefault(st.teacher_id, []).append(st.type)

    # 13. Sort: sponsored first, then by ranking score
    def _format_compatible(tid: UUID) -> bool:
        if not student_lesson_format or student_lesson_format == "both":
            return True
        fmts = teacher_lesson_formats.get(tid, {"both"})
        return "both" in fmts or student_lesson_format in fmts

    def rank_score(tid: UUID) -> float:
        tp = tp_map[tid]
        s = tp.rating_avg * max(tp.reviews_count, 1)
        if tp.verified:
            s += 5.0
        if _format_compatible(tid):
            s += 3.0  # soft boost for lesson format match
        return s

    teacher_ids_sorted = sorted(
        teacher_ids,
        key=lambda tid: (1 if tp_map[tid].sponsored else 0, rank_score(tid)),
        reverse=True,
    )

    # 14. Build response
    items: list[TeacherSearchItem] = []
    for tid in teacher_ids_sorted:
        p = prof_map.get(tid)
        tp = tp_map.get(tid)
        if not p or not tp:
            continue
        items.append(TeacherSearchItem(
            id=str(tid),
            first_name=p.first_name or "",
            last_name=p.last_name or "",
            full_name=p.full_name or "",
            avatar_url=p.avatar_url,
            wilaya=tp.teaching_wilaya or p.wilaya,
            bio=tp.bio_long or p.bio,
            headline=tp.headline,
            price_per_session=teacher_min_price.get(tid) or tp.price_per_session,
            rating_avg=round(tp.rating_avg, 1),
            reviews_count=tp.reviews_count,
            verified=tp.verified,
            sponsored=tp.sponsored,
            badge=tp.badge,
            kp_reward=tp.kp_reward,
            subjects=teacher_subjects.get(tid, []),
            levels=teacher_level_labels.get(tid, []),
            modes=teacher_modes.get(tid, []),
            session_types=teacher_stypes.get(tid, []),
            lesson_formats=sorted(teacher_lesson_formats.get(tid, {"both"})),
            subject_formats={
                sn: _dominant_format(fmts)
                for sn, fmts in teacher_subject_formats.get(tid, {}).items()
            },
            languages=getattr(tp_map.get(tid), "languages", None) or [],
        ))

    return TeacherSearchResponse(items=items, total=len(items))


# ─── Teacher public profile detail ───────────────────────────────────────────

class TeacherSubjectItem(BaseModel):
    name: str
    level: str
    price: int


class TeacherDiplomaItem(BaseModel):
    id: str
    name: str
    file_url: Optional[str] = None
    file_type: Optional[str] = None
    verified: bool = False


class TeacherPublicProfile(BaseModel):
    id: str
    first_name: str
    last_name: str
    full_name: str
    avatar_url: Optional[str] = None
    bio: Optional[str] = None
    headline: Optional[str] = None
    wilaya: Optional[str] = None
    price_per_session: int
    kp_reward: int
    rating_avg: float
    reviews_count: int
    verified: bool
    badge: Optional[str] = None
    experience_years: int
    success_rate: Optional[float] = None
    students_count: int
    hours_taught: int
    subjects: List[TeacherSubjectItem]
    modes: List[str]
    session_types: List[str]
    diplomas: List[TeacherDiplomaItem]


class TeacherReviewItem(BaseModel):
    id: str
    reviewer_name: str
    rating: int
    comment: Optional[str] = None
    created_at: str


class TeacherSlotItem(BaseModel):
    id: str
    slot_date: str
    start_time: str
    end_time: str
    mode: str
    type: str


@router.get("/teachers/{teacher_id}", response_model=TeacherPublicProfile)
async def get_teacher_profile(
    teacher_id: UUID,
    db: Session = Depends(get_db),
    _: Dict[str, Any] = Depends(get_current_user),
):
    """Public teacher profile page data."""
    tp = db.exec(
        select(TeacherProfile).where(TeacherProfile.user_id == teacher_id)
    ).first()
    if tp is None or tp.status not in ("approved",):
        raise HTTPException(status_code=404, detail="Teacher not found")

    p = db.get(Profile, teacher_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Teacher profile not found")

    # Subjects from TeacherSubjectPrice + Subject + Level
    sp_rows = db.exec(
        select(TeacherSubjectPrice, Subject, Level)
        .join(Subject, Subject.id == TeacherSubjectPrice.subject_id)
        .join(Level, Level.id == TeacherSubjectPrice.level_id)
        .where(TeacherSubjectPrice.teacher_id == teacher_id)
        .where(TeacherSubjectPrice.active == True)  # noqa: E712
    ).all()
    subjects = [
        TeacherSubjectItem(name=subj.name, level=lvl.label, price=tsp.price_single)
        for tsp, subj, lvl in sp_rows
    ]

    # Modes
    mode_rows = db.exec(
        select(TeacherMode).where(TeacherMode.teacher_id == teacher_id)
    ).all()
    modes = [m.mode for m in mode_rows]

    # Session types
    stype_rows = db.exec(
        select(TeacherSessionType).where(TeacherSessionType.teacher_id == teacher_id)
    ).all()
    session_types = [st.type for st in stype_rows]

    # Diplomas
    diploma_rows = db.exec(
        select(TeacherDiploma).where(TeacherDiploma.teacher_id == teacher_id)
    ).all()
    diplomas = [
        TeacherDiplomaItem(
            id=str(d.id), name=d.name, file_url=d.file_url,
            file_type=d.file_type, verified=d.verified,
        )
        for d in diploma_rows
    ]

    return TeacherPublicProfile(
        id=str(teacher_id),
        first_name=p.first_name or "",
        last_name=p.last_name or "",
        full_name=p.full_name or "",
        avatar_url=p.avatar_url,
        bio=tp.bio_long or p.bio,
        headline=tp.headline,
        wilaya=tp.teaching_wilaya or p.wilaya,
        price_per_session=tp.price_per_session,
        kp_reward=tp.kp_reward,
        rating_avg=round(tp.rating_avg, 1),
        reviews_count=tp.reviews_count,
        verified=tp.verified,
        badge=tp.badge,
        experience_years=tp.experience_years,
        success_rate=tp.success_rate,
        students_count=tp.students_count,
        hours_taught=tp.hours_taught,
        subjects=subjects,
        modes=modes if modes else ["online"],
        session_types=session_types if session_types else ["individual"],
        diplomas=diplomas,
    )


@router.get("/teachers/{teacher_id}/reviews")
async def get_teacher_reviews(
    teacher_id: UUID,
    page: int = Query(1, ge=1),
    size: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    _: Dict[str, Any] = Depends(get_current_user),
):
    """Public reviews for a teacher, with reviewer name."""
    tp = db.exec(
        select(TeacherProfile).where(TeacherProfile.user_id == teacher_id)
    ).first()
    if tp is None:
        raise HTTPException(status_code=404, detail="Teacher not found")

    reviews = db.exec(
        select(Review)
        .where(Review.teacher_id == teacher_id)
        .where(Review.status == "visible")
        .order_by(Review.created_at.desc())
    ).all()

    # Batch-load reviewer names
    reviewer_ids = list({r.student_id for r in reviews})
    profiles = db.exec(select(Profile).where(Profile.id.in_(reviewer_ids))).all()
    prof_map = {p.id: p for p in profiles}

    total = len(reviews)
    offset = (page - 1) * size
    page_reviews = reviews[offset: offset + size]

    items = []
    for r in page_reviews:
        reviewer = prof_map.get(r.student_id)
        if reviewer:
            name_parts = (reviewer.full_name or "").split()
            if len(name_parts) >= 2:
                reviewer_name = f"{name_parts[0]} {name_parts[-1][0]}."
            else:
                reviewer_name = reviewer.full_name or "Étudiant"
        else:
            reviewer_name = "Étudiant"
        items.append(TeacherReviewItem(
            id=str(r.id),
            reviewer_name=reviewer_name,
            rating=r.rating,
            comment=r.comment,
            created_at=r.created_at.isoformat(),
        ))

    return {"items": items, "total": total, "page": page, "size": size}


@router.get("/teachers/{teacher_id}/slots")
async def get_teacher_slots(
    teacher_id: UUID,
    db: Session = Depends(get_db),
    _: Dict[str, Any] = Depends(get_current_user),
):
    """Upcoming open slots for a teacher (next 30 days)."""
    today = date.today()
    from datetime import timedelta
    end = today + timedelta(days=30)

    slots = db.exec(
        select(TeacherSlot)
        .where(TeacherSlot.teacher_id == teacher_id)
        .where(TeacherSlot.status == "open")
        .where(TeacherSlot.slot_date >= today)
        .where(TeacherSlot.slot_date <= end)
        .order_by(TeacherSlot.slot_date, TeacherSlot.start_time)
    ).all()

    return [
        TeacherSlotItem(
            id=str(s.id),
            slot_date=s.slot_date.isoformat(),
            start_time=str(s.start_time)[:5],
            end_time=str(s.end_time)[:5],
            mode=s.mode,
            type=s.type,
        )
        for s in slots
    ]
