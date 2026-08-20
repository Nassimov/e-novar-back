from __future__ import annotations

import math
import re
import unicodedata
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field as PydField
from sqlmodel import Session, func, select

from app.core.cache import cache_invalidate
from app.dependencies import get_admin_user, get_db
from app.models.booking import TutoringSession
from app.models.catalog import Level, Subject, TeacherSubjectPrice
from app.models.scheduling import TeacherSlotSubject
from app.models.teacher import TeacherPayout
from app.schemas.admin import WithdrawalProcessRequest

router = APIRouter(tags=["admin-content"])


# ── Helpers ────────────────────────────────────────────────────────────────

def _slugify(value: str) -> str:
    """Convert text to a DB-safe slug: remove accents, lowercase, replace non-alphanum with _."""
    value = unicodedata.normalize("NFD", value)
    value = "".join(c for c in value if unicodedata.category(c) != "Mn")
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    return value.strip("_") or "item"


def _unique_value(db: Session, model, column, base: str) -> str:
    """Return `base`, or `base_2`, `base_3`, ... — whichever isn't already taken on `column`."""
    candidate = base
    i = 1
    while db.exec(select(model).where(column == candidate)).first() is not None:
        i += 1
        candidate = f"{base}_{i}"
    return candidate


# ── Schemas ──────────────────────────────────────────────────────────────────

class SubjectCreate(BaseModel):
    name: str = PydField(min_length=1, max_length=100)
    icon: Optional[str] = None


class SubjectUpdate(BaseModel):
    name: Optional[str] = PydField(default=None, min_length=1, max_length=100)
    icon: Optional[str] = None


class LevelCreate(BaseModel):
    label: str = PydField(min_length=1, max_length=100)
    position: int = 0


class LevelUpdate(BaseModel):
    label: Optional[str] = PydField(default=None, min_length=1, max_length=100)
    position: Optional[int] = None


# ── Subjects CRUD (mirrors public.subjects) ─────────────────────────────────

def _subject_stats(db: Session) -> tuple[Dict[UUID, int], Dict[UUID, int], Dict[UUID, int]]:
    """Return (teachers_by_subject, levels_by_subject, sessions_by_subject) count maps,
    computed from real join tables — never faked."""
    teacher_rows = db.exec(
        select(
            TeacherSubjectPrice.subject_id,
            func.count(func.distinct(TeacherSubjectPrice.teacher_id)),
        )
        .where(TeacherSubjectPrice.active == True)  # noqa: E712
        .group_by(TeacherSubjectPrice.subject_id)
    ).all()
    teachers_by_subject = {row[0]: row[1] for row in teacher_rows}

    level_rows = db.exec(
        select(
            TeacherSubjectPrice.subject_id,
            func.count(func.distinct(TeacherSubjectPrice.level_id)),
        )
        .where(TeacherSubjectPrice.active == True)  # noqa: E712
        .group_by(TeacherSubjectPrice.subject_id)
    ).all()
    levels_by_subject = {row[0]: row[1] for row in level_rows}

    session_rows = db.exec(
        select(TutoringSession.subject_id, func.count(TutoringSession.id))
        .where(TutoringSession.subject_id.is_not(None))
        .group_by(TutoringSession.subject_id)
    ).all()
    sessions_by_subject = {row[0]: row[1] for row in session_rows}

    return teachers_by_subject, levels_by_subject, sessions_by_subject


def _serialize_subject(s: Subject, teachers: Dict[UUID, int], levels: Dict[UUID, int], sessions: Dict[UUID, int]) -> Dict[str, Any]:
    return {
        "id": str(s.id),
        "slug": s.slug,
        "name": s.name,
        "icon": s.icon,
        "teachers_count": teachers.get(s.id, 0),
        "levels_count": levels.get(s.id, 0),
        "sessions_count": sessions.get(s.id, 0),
    }


@router.get("/subjects")
async def list_subjects(
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """List all subjects with real usage stats (teachers offering it, levels covered, sessions held)."""
    subjects = db.exec(select(Subject).order_by(Subject.name)).all()
    teachers, levels, sessions = _subject_stats(db)
    return {
        "items": [_serialize_subject(s, teachers, levels, sessions) for s in subjects],
        "total": len(subjects),
    }


@router.post("/subjects", status_code=status.HTTP_201_CREATED)
async def create_subject(
    payload: SubjectCreate,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Create a new subject."""
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Le nom est requis")
    dup = db.exec(select(Subject).where(func.lower(Subject.name) == name.lower())).first()
    if dup:
        raise HTTPException(status_code=409, detail="Une matière avec ce nom existe déjà")

    slug = _unique_value(db, Subject, Subject.slug, _slugify(name))
    subject = Subject(slug=slug, name=name, icon=payload.icon)
    db.add(subject)
    db.commit()
    db.refresh(subject)
    cache_invalidate("catalog:subjects")
    return _serialize_subject(subject, {}, {}, {})


@router.put("/subjects/{subject_id}")
async def update_subject(
    subject_id: UUID,
    payload: SubjectUpdate,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Update a subject's name and/or icon."""
    subject = db.get(Subject, subject_id)
    if subject is None:
        raise HTTPException(status_code=404, detail="Matière introuvable")

    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Le nom est requis")
        dup = db.exec(
            select(Subject).where(func.lower(Subject.name) == name.lower(), Subject.id != subject_id)
        ).first()
        if dup:
            raise HTTPException(status_code=409, detail="Une matière avec ce nom existe déjà")
        subject.name = name

    if payload.icon is not None:
        subject.icon = payload.icon

    db.add(subject)
    db.commit()
    db.refresh(subject)
    cache_invalidate("catalog:subjects")
    teachers, levels, sessions = _subject_stats(db)
    return _serialize_subject(subject, teachers, levels, sessions)


@router.delete("/subjects/{subject_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_subject(
    subject_id: UUID,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Delete a subject. Blocked if teachers price it or sessions reference it, to avoid orphaning data."""
    subject = db.get(Subject, subject_id)
    if subject is None:
        raise HTTPException(status_code=404, detail="Matière introuvable")

    if db.exec(select(TeacherSubjectPrice.id).where(TeacherSubjectPrice.subject_id == subject_id).limit(1)).first():
        raise HTTPException(
            status_code=409,
            detail="Cette matière est utilisée dans des tarifs enseignants et ne peut pas être supprimée.",
        )
    if db.exec(select(TutoringSession.id).where(TutoringSession.subject_id == subject_id).limit(1)).first():
        raise HTTPException(
            status_code=409,
            detail="Cette matière est référencée par des sessions existantes et ne peut pas être supprimée.",
        )
    if db.exec(select(TeacherSlotSubject.id).where(TeacherSlotSubject.subject_id == subject_id).limit(1)).first():
        raise HTTPException(
            status_code=409,
            detail="Cette matière est référencée par des créneaux enseignants et ne peut pas être supprimée.",
        )

    db.delete(subject)
    db.commit()
    cache_invalidate("catalog:subjects")
    return None


# ── Levels CRUD (mirrors public.levels) ─────────────────────────────────────

@router.get("/levels")
async def list_levels_admin(
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """List all school levels ordered by position."""
    levels = db.exec(select(Level).order_by(Level.position, Level.label)).all()
    return {
        "items": [{"id": str(l.id), "code": l.code, "label": l.label, "position": l.position} for l in levels],
        "total": len(levels),
    }


@router.post("/levels", status_code=status.HTTP_201_CREATED)
async def create_level(
    payload: LevelCreate,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Create a new school level."""
    label = payload.label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="Le libellé est requis")
    dup = db.exec(select(Level).where(func.lower(Level.label) == label.lower())).first()
    if dup:
        raise HTTPException(status_code=409, detail="Un niveau avec ce libellé existe déjà")

    code = _unique_value(db, Level, Level.code, _slugify(label))
    position = payload.position
    if not position:
        max_position = db.exec(select(func.max(Level.position))).first()
        position = (max_position or 0) + 1
    level = Level(code=code, label=label, position=position)
    db.add(level)
    db.commit()
    db.refresh(level)
    cache_invalidate("catalog:levels")
    return {"id": str(level.id), "code": level.code, "label": level.label, "position": level.position}


@router.put("/levels/{level_id}")
async def update_level(
    level_id: UUID,
    payload: LevelUpdate,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Update a level's label and/or position."""
    level = db.get(Level, level_id)
    if level is None:
        raise HTTPException(status_code=404, detail="Niveau introuvable")

    if payload.label is not None:
        label = payload.label.strip()
        if not label:
            raise HTTPException(status_code=400, detail="Le libellé est requis")
        dup = db.exec(
            select(Level).where(func.lower(Level.label) == label.lower(), Level.id != level_id)
        ).first()
        if dup:
            raise HTTPException(status_code=409, detail="Un niveau avec ce libellé existe déjà")
        level.label = label

    if payload.position is not None:
        level.position = payload.position

    db.add(level)
    db.commit()
    db.refresh(level)
    cache_invalidate("catalog:levels")
    return {"id": str(level.id), "code": level.code, "label": level.label, "position": level.position}


@router.delete("/levels/{level_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_level(
    level_id: UUID,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Delete a level. Blocked if teachers price it or sessions reference it, to avoid orphaning data."""
    level = db.get(Level, level_id)
    if level is None:
        raise HTTPException(status_code=404, detail="Niveau introuvable")

    if db.exec(select(TeacherSubjectPrice.id).where(TeacherSubjectPrice.level_id == level_id).limit(1)).first():
        raise HTTPException(
            status_code=409,
            detail="Ce niveau est utilisé dans des tarifs enseignants et ne peut pas être supprimé.",
        )
    if db.exec(select(TutoringSession.id).where(TutoringSession.level_id == level_id).limit(1)).first():
        raise HTTPException(
            status_code=409,
            detail="Ce niveau est référencé par des sessions existantes et ne peut pas être supprimé.",
        )
    if db.exec(select(TeacherSlotSubject.id).where(TeacherSlotSubject.level_id == level_id).limit(1)).first():
        raise HTTPException(
            status_code=409,
            detail="Ce niveau est référencé par des créneaux enseignants et ne peut pas être supprimé.",
        )

    db.delete(level)
    db.commit()
    cache_invalidate("catalog:levels")
    return None


# ── Payout management (EP → DZD) ────────────────────────────────────────────
# Note: no dedicated UI section in admin.content.tsx consumes these endpoints
# today (there never was one in the mock either) — they're kept as a working
# backend surface for a future payouts/wallet admin page.

@router.get("/withdrawals")
async def list_withdrawals(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status_filter: Optional[str] = Query(None, alias="status"),
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """List all EP→DZD payout requests."""
    query = select(TeacherPayout)
    if status_filter:
        query = query.where(TeacherPayout.status == status_filter)

    payouts = db.exec(query.order_by(TeacherPayout.requested_at.desc())).all()
    total = len(payouts)
    offset = (page - 1) * size
    paginated = payouts[offset: offset + size]

    return {
        "items": [
            {
                "id": str(p.id),
                "teacher_id": str(p.teacher_id),
                "ep_amount": p.ep_amount,
                "dzd_amount": p.dzd_amount,
                "status": p.status,
                "iban": p.iban,
                "bank_holder": p.bank_holder,
                "admin_note": p.admin_note,
                "requested_at": p.requested_at.isoformat(),
                "processed_at": p.processed_at.isoformat() if p.processed_at else None,
            }
            for p in paginated
        ],
        "total": total,
        "page": page,
        "size": size,
        "pages": math.ceil(total / size) if total else 0,
    }


@router.put("/withdrawals/{withdrawal_id}")
async def process_withdrawal(
    withdrawal_id: UUID,
    payload: WithdrawalProcessRequest,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Approve or reject an EP→DZD payout request.
    On approve: deducts EP via kp_transactions (trigger updates kp_balances).
    """
    from datetime import datetime

    payout = db.get(TeacherPayout, withdrawal_id)
    if payout is None:
        raise HTTPException(status_code=404, detail="Payout request not found")
    if payout.status != "pending":
        raise HTTPException(status_code=400, detail="Only pending payouts can be processed")

    if payload.action == "approve":
        if payload.dzd_amount is None:
            raise HTTPException(status_code=400, detail="dzd_amount requis pour approuver un retrait")
        payout.status = "approved"
        payout.dzd_amount = payload.dzd_amount
        payout.processed_at = datetime.utcnow()

        # Deduct EP from teacher's balance via kp_transactions
        from app.models.kp import KpTransaction
        db.add(KpTransaction(
            user_id=payout.teacher_id,
            amount=-payout.ep_amount,
            source="reward",
            label=f"Retrait EP → DZD ({payload.dzd_amount} DZD)",
            ref_type="payout",
            ref_id=payout.id,
        ))

    elif payload.action == "reject":
        payout.status = "rejected"
        payout.processed_at = datetime.utcnow()
    else:
        raise HTTPException(status_code=400, detail="action invalide. Utilise 'approve' ou 'reject'")

    if payload.admin_note:
        payout.admin_note = payload.admin_note

    db.add(payout)
    db.commit()
    return {"message": f"Payout {payload.action}d", "payout_id": str(withdrawal_id)}
