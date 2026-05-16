from __future__ import annotations

import unicodedata
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.catalog import Level, Subject, TeacherMode, TeacherSessionType, TeacherSubjectPrice
from app.models.profile import Profile, TeacherProfile

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
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
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

    # Build per-teacher dicts (unique ordered lists)
    teacher_subjects: dict[UUID, list[str]] = {}
    teacher_level_codes: dict[UUID, list[str]] = {}
    teacher_level_labels: dict[UUID, list[str]] = {}

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

    # 5. Subject filter
    if subject:
        norm_sub = _norm(subject)
        teacher_ids = [
            tid for tid in teacher_ids
            if any(_norm(s) == norm_sub for s in teacher_subjects.get(tid, []))
        ]
        if not teacher_ids:
            return TeacherSearchResponse(items=[], total=0)

    # 6. Level group filter
    if level:
        target_codes = LEVEL_GROUP_CODES.get(level, [])
        norm_codes = {_norm(c) for c in target_codes}
        teacher_ids = [
            tid for tid in teacher_ids
            if any(
                _norm(c) in norm_codes
                for c in teacher_level_codes.get(tid, [])
            )
        ]
        if not teacher_ids:
            return TeacherSearchResponse(items=[], total=0)

    # 7. Load profiles for remaining teachers
    profiles = db.exec(
        select(Profile).where(Profile.id.in_(teacher_ids))
    ).all()
    prof_map: dict[UUID, Profile] = {p.id: p for p in profiles}

    # 8. Wilaya filter (match teaching_wilaya or home wilaya)
    if wilaya:
        norm_wil = _norm(wilaya)
        teacher_ids = [
            tid for tid in teacher_ids
            if (
                _norm(tp_map[tid].teaching_wilaya or "") == norm_wil
                or _norm((prof_map.get(tid) and prof_map[tid].wilaya) or "") == norm_wil
            )
        ]
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

    # 13. Sort: sponsored first, then by ranking score (rating × reviews + verified bonus)
    def rank_score(tp: TeacherProfile) -> float:
        s = tp.rating_avg * max(tp.reviews_count, 1)
        if tp.verified:
            s += 5.0
        return s

    teacher_ids_sorted = sorted(
        teacher_ids,
        key=lambda tid: (1 if tp_map[tid].sponsored else 0, rank_score(tp_map[tid])),
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
            price_per_session=tp.price_per_session,
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
        ))

    return TeacherSearchResponse(items=items, total=len(items))
