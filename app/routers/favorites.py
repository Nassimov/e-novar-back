from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.catalog import Subject, TeacherSubjectPrice
from app.models.profile import Profile, TeacherProfile
from app.models.review import Favorite

router = APIRouter(tags=["Student"])


class FavoriteTeacher(BaseModel):
    id: str
    first_name: str
    last_name: str
    full_name: str
    avatar_url: Optional[str] = None
    subjects: List[str]
    rating_avg: float
    price_per_session: int


class FavoriteListResponse(BaseModel):
    items: List[FavoriteTeacher]
    total: int


class AddFavoriteRequest(BaseModel):
    teacher_id: UUID


@router.get("/", response_model=FavoriteListResponse)
async def list_favorites(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])

    favorites = db.exec(
        select(Favorite).where(Favorite.student_id == uid)
    ).all()

    if not favorites:
        return FavoriteListResponse(items=[], total=0)

    teacher_ids = [f.teacher_id for f in favorites]

    # Batch-load profiles and teacher profiles
    profiles = db.exec(select(Profile).where(Profile.id.in_(teacher_ids))).all()
    prof_map: dict[UUID, Profile] = {p.id: p for p in profiles}

    teacher_profiles = db.exec(
        select(TeacherProfile).where(TeacherProfile.user_id.in_(teacher_ids))
    ).all()
    tp_map: dict[UUID, TeacherProfile] = {tp.user_id: tp for tp in teacher_profiles}

    # Batch-load subject prices
    sp_all = db.exec(
        select(TeacherSubjectPrice).where(
            TeacherSubjectPrice.teacher_id.in_(teacher_ids),
            TeacherSubjectPrice.active == True,  # noqa: E712
        )
    ).all()

    all_sub_ids = list({r.subject_id for r in sp_all})
    sub_name_map: dict[UUID, str] = {}
    if all_sub_ids:
        subs = db.exec(select(Subject).where(Subject.id.in_(all_sub_ids))).all()
        sub_name_map = {s.id: s.name for s in subs}

    teacher_subjects: dict[UUID, list[str]] = {}
    teacher_min_price: dict[UUID, int] = {}
    for r in sp_all:
        tid = r.teacher_id
        sn = sub_name_map.get(r.subject_id)
        if sn and sn not in teacher_subjects.get(tid, []):
            teacher_subjects.setdefault(tid, []).append(sn)
        if r.price_single > 0:
            if tid not in teacher_min_price or r.price_single < teacher_min_price[tid]:
                teacher_min_price[tid] = r.price_single

    items: list[FavoriteTeacher] = []
    for fav in favorites:
        tid = fav.teacher_id
        p = prof_map.get(tid)
        tp = tp_map.get(tid)
        if not p:
            continue
        items.append(FavoriteTeacher(
            id=str(tid),
            first_name=p.first_name or "",
            last_name=p.last_name or "",
            full_name=p.full_name or "",
            avatar_url=p.avatar_url,
            subjects=teacher_subjects.get(tid, []),
            rating_avg=round(tp.rating_avg, 1) if tp else 0.0,
            price_per_session=teacher_min_price.get(tid) or (tp.price_per_session if tp else 0),
        ))

    return FavoriteListResponse(items=items, total=len(items))


@router.post("/", status_code=status.HTTP_201_CREATED)
async def add_favorite(
    payload: AddFavoriteRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])
    teacher_id = payload.teacher_id

    tp = db.exec(
        select(TeacherProfile).where(TeacherProfile.user_id == teacher_id)
    ).first()
    if not tp:
        raise HTTPException(status_code=404, detail="Enseignant introuvable")

    existing = db.exec(
        select(Favorite).where(
            Favorite.student_id == uid,
            Favorite.teacher_id == teacher_id,
        )
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Déjà dans les favoris")

    fav = Favorite(student_id=uid, teacher_id=teacher_id)
    db.add(fav)
    db.commit()
    return {"id": str(fav.id), "teacher_id": str(teacher_id)}


@router.delete("/{teacher_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_favorite(
    teacher_id: UUID,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    uid = UUID(current_user["id"])

    fav = db.exec(
        select(Favorite).where(
            Favorite.student_id == uid,
            Favorite.teacher_id == teacher_id,
        )
    ).first()
    if not fav:
        raise HTTPException(status_code=404, detail="Pas dans les favoris")

    db.delete(fav)
    db.commit()
    return None
