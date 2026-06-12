from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from uuid import UUID

import base64

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, select

from app.dependencies import get_current_user, get_db
from app.models.gamification import Challenge, ChallengeParticipation, ChallengeProofFile

router = APIRouter(tags=["challenges"])

# ── File validation constants ─────────────────────────────────────────────────

MAX_FILES = 5

_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".pdf", ".mp4", ".mov", ".webm"}

_EXT_TO_MIME: Dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}

_ALLOWED_MIME_TYPES = set(_EXT_TO_MIME.values())

# Per-category size limits (bytes)
_SIZE_LIMITS: Dict[str, int] = {
    "image": 10 * 1024 * 1024,        # 10 MB
    "application": 25 * 1024 * 1024,  # 25 MB (PDF)
    "video": 100 * 1024 * 1024,       # 100 MB
}


def _validate_upload(filename: str, content_type: Optional[str], file_size: int) -> str:
    """
    Validates a single uploaded file.
    Returns the canonical MIME type.
    Raises HTTPException(422) on any violation.
    """
    if not filename:
        raise HTTPException(status_code=422, detail="Nom de fichier manquant")

    dot_pos = filename.rfind(".")
    ext = ("." + filename[dot_pos + 1:].lower()) if dot_pos != -1 else ""
    if ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Fichier '{filename}' : format non supporté. "
                "Formats acceptés : JPG, PNG, WEBP, PDF, MP4, MOV, WEBM"
            ),
        )

    canonical_mime = _EXT_TO_MIME[ext]

    # Reject if browser-declared content_type is explicitly a disallowed type
    if content_type and content_type not in _ALLOWED_MIME_TYPES and content_type != "application/octet-stream":
        raise HTTPException(
            status_code=422,
            detail=f"Fichier '{filename}' : type MIME '{content_type}' non autorisé",
        )

    if file_size == 0:
        raise HTTPException(status_code=422, detail=f"Fichier '{filename}' : le fichier est vide")

    category = canonical_mime.split("/")[0]
    limit = _SIZE_LIMITS.get(category, _SIZE_LIMITS["image"])
    if file_size > limit:
        limit_mb = limit // (1024 * 1024)
        raise HTTPException(
            status_code=422,
            detail=f"Fichier '{filename}' : taille maximale {limit_mb} Mo dépassée",
        )

    return canonical_mime


# ── Output schemas ────────────────────────────────────────────────────────────

class ProofFileOut(BaseModel):
    id: str
    original_filename: str
    mime_type: str
    file_size_bytes: int
    sort_order: int


class ParticipationOut(BaseModel):
    id: str
    status: str
    started_at: str
    deadline_at: Optional[str]
    submitted_at: Optional[str]
    proof_url: Optional[str]
    proof_name: Optional[str]
    proof_message: Optional[str]
    reason: Optional[str]
    proof_files: List[ProofFileOut]


class ChallengeOut(BaseModel):
    id: str
    title: str
    description: str
    reward: int
    audience: str
    proof_type: str
    active: bool
    ends_at: Optional[str]
    timer_duration_sec: Optional[int]
    rules: Optional[str]
    proof_instructions: Optional[str]
    approval_conditions: Optional[str]
    participation: Optional[ParticipationOut]


class ChallengesListResponse(BaseModel):
    challenges: List[ChallengeOut]
    active_count: int
    potential_ep: int


class HistoryOut(BaseModel):
    participations: List[dict]


# ── Request-upload input/output schemas ───────────────────────────────────────

class FileMetadataIn(BaseModel):
    name: str
    size: int
    mime_type: str


class RequestUploadPayload(BaseModel):
    files: List[FileMetadataIn]


# ── Submit proof input schema (pure JSON — files as base64) ──────────────────

class ProofFileIn(BaseModel):
    content_base64: str         # base64-encoded file content (no data URL prefix)
    original_filename: str
    mime_type: str
    file_size_bytes: int


class SubmitProofPayload(BaseModel):
    proof_files: List[ProofFileIn] = []
    proof_message: Optional[str] = None
    proof_text: Optional[str] = None
    proof_url_link: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _proof_files_out(participation_id: UUID, db: Session) -> List[ProofFileOut]:
    try:
        files = db.exec(
            select(ChallengeProofFile)
            .where(ChallengeProofFile.participation_id == participation_id)
            .order_by(ChallengeProofFile.sort_order)
        ).all()
        return [
            ProofFileOut(
                id=str(f.id),
                original_filename=f.original_filename,
                mime_type=f.mime_type,
                file_size_bytes=f.file_size_bytes,
                sort_order=f.sort_order,
            )
            for f in files
        ]
    except Exception:
        db.rollback()
        return []


def _participation_out(p: ChallengeParticipation, db: Session) -> ParticipationOut:
    return ParticipationOut(
        id=str(p.id),
        status=p.status,
        started_at=p.started_at.isoformat(),
        deadline_at=p.deadline_at.isoformat() if p.deadline_at else None,
        submitted_at=p.submitted_at.isoformat() if p.submitted_at else None,
        proof_url=p.proof_url,
        proof_name=p.proof_name,
        proof_message=p.proof_message,
        reason=p.reason,
        proof_files=_proof_files_out(p.id, db),
    )


def _challenge_out(c: Challenge, p: Optional[ChallengeParticipation], db: Session) -> ChallengeOut:
    return ChallengeOut(
        id=c.id,
        title=c.title,
        description=c.description or "",
        reward=c.reward,
        audience=c.audience,
        proof_type=c.proof_type or "image-or-pdf",
        active=c.active,
        ends_at=c.ends_at.isoformat() if c.ends_at else None,
        timer_duration_sec=c.timer_duration_sec,
        rules=c.rules,
        proof_instructions=c.proof_instructions,
        approval_conditions=c.approval_conditions,
        participation=_participation_out(p, db) if p else None,
    )


def _auto_expire(user_id: UUID, db: Session) -> None:
    now = datetime.utcnow()
    expired_parts = db.exec(
        select(ChallengeParticipation).where(
            ChallengeParticipation.user_id == user_id,
            ChallengeParticipation.status == "in_progress",
            ChallengeParticipation.deadline_at.isnot(None),
            ChallengeParticipation.deadline_at < now,
        )
    ).all()
    if expired_parts:
        for ep in expired_parts:
            ep.status = "expired"
            db.add(ep)
        db.commit()


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/", response_model=ChallengesListResponse)
async def list_challenges(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])
    role = current_user["role"]
    now = datetime.utcnow()

    _auto_expire(user_id, db)

    stmt = select(Challenge).where(Challenge.active == True)
    stmt = stmt.where(
        (Challenge.ends_at.is_(None)) | (Challenge.ends_at >= now)
    )
    if role == "student":
        stmt = stmt.where(Challenge.audience.in_(["student", "both"]))
    elif role == "teacher":
        stmt = stmt.where(Challenge.audience.in_(["teacher", "both"]))

    challenges = db.exec(stmt).all()

    participations = {
        p.challenge_id: p
        for p in db.exec(
            select(ChallengeParticipation).where(
                ChallengeParticipation.user_id == user_id
            )
        ).all()
    }

    result = [_challenge_out(c, participations.get(c.id), db) for c in challenges]

    active_count = sum(
        1 for r in result
        if r.participation and r.participation.status == "in_progress"
    )
    potential_ep = sum(
        c.reward
        for c, r in zip(challenges, result)
        if r.participation and r.participation.status in ("in_progress", "submitted")
    )

    return ChallengesListResponse(
        challenges=result,
        active_count=active_count,
        potential_ep=potential_ep,
    )


@router.get("/history/", response_model=HistoryOut)
async def get_history(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])

    parts = db.exec(
        select(ChallengeParticipation)
        .where(ChallengeParticipation.user_id == user_id)
        .order_by(ChallengeParticipation.started_at.desc())
    ).all()

    challenge_ids = list({p.challenge_id for p in parts})
    challenges_map: Dict[str, Challenge] = {}
    if challenge_ids:
        for c in db.exec(select(Challenge).where(Challenge.id.in_(challenge_ids))).all():
            challenges_map[c.id] = c

    items = []
    for p in parts:
        c = challenges_map.get(p.challenge_id)
        proof_files = _proof_files_out(p.id, db)
        items.append({
            "participation_id": str(p.id),
            "challenge_id": p.challenge_id,
            "challenge_title": c.title if c else "—",
            "challenge_description": c.description if c else "",
            "reward": c.reward if c else 0,
            "status": p.status,
            "started_at": p.started_at.isoformat(),
            "submitted_at": p.submitted_at.isoformat() if p.submitted_at else None,
            "proof_url": p.proof_url,
            "proof_name": p.proof_name,
            "proof_message": p.proof_message,
            "reason": p.reason,
            "deadline_at": p.deadline_at.isoformat() if p.deadline_at else None,
            "proof_files": [pf.model_dump() for pf in proof_files],
        })

    return HistoryOut(participations=items)


@router.post("/{challenge_id}/start", response_model=ChallengeOut)
async def start_challenge(
    challenge_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])

    challenge = db.get(Challenge, challenge_id)
    if challenge is None or not challenge.active:
        raise HTTPException(status_code=404, detail="Challenge not found or inactive")

    existing = db.exec(
        select(ChallengeParticipation).where(
            ChallengeParticipation.challenge_id == challenge_id,
            ChallengeParticipation.user_id == user_id,
        )
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Already participated in this challenge")

    now = datetime.utcnow()
    deadline = (
        now + timedelta(seconds=challenge.timer_duration_sec)
        if challenge.timer_duration_sec
        else None
    )

    part = ChallengeParticipation(
        challenge_id=challenge_id,
        user_id=user_id,
        status="in_progress",
        started_at=now,
        deadline_at=deadline,
    )
    db.add(part)
    db.commit()
    db.refresh(part)

    return _challenge_out(challenge, part, db)


@router.post("/{challenge_id}/decline", status_code=status.HTTP_204_NO_CONTENT)
async def decline_challenge(
    challenge_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = UUID(current_user["id"])

    challenge = db.get(Challenge, challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail="Challenge not found")

    existing = db.exec(
        select(ChallengeParticipation).where(
            ChallengeParticipation.challenge_id == challenge_id,
            ChallengeParticipation.user_id == user_id,
        )
    ).first()

    if existing:
        if existing.status != "in_progress":
            raise HTTPException(status_code=400, detail="Cannot decline a challenge not in progress")
        existing.status = "declined"
        db.add(existing)
    else:
        part = ChallengeParticipation(
            challenge_id=challenge_id,
            user_id=user_id,
            status="declined",
        )
        db.add(part)

    db.commit()
    return None


@router.post("/{challenge_id}/request-upload")
def request_upload(
    challenge_id: str,
    payload: RequestUploadPayload,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return presigned Supabase upload URLs so the browser can upload files directly.
    No file bytes go through Railway — only metadata validation + URL generation."""
    from app.services.storage import generate_proof_upload_url

    user_id = UUID(current_user["id"])
    role = current_user.get("role", "student")
    now = datetime.utcnow()

    part = db.exec(
        select(ChallengeParticipation).where(
            ChallengeParticipation.challenge_id == challenge_id,
            ChallengeParticipation.user_id == user_id,
        )
    ).first()

    if part is None:
        raise HTTPException(status_code=404, detail="Participation introuvable")
    if part.status != "in_progress":
        raise HTTPException(status_code=400, detail="Ce défi n'est pas en cours")
    if part.deadline_at and part.deadline_at < now:
        raise HTTPException(status_code=400, detail="La durée du défi est expirée")

    if not payload.files:
        raise HTTPException(status_code=422, detail="Au moins un fichier de preuve est requis")
    if len(payload.files) > MAX_FILES:
        raise HTTPException(status_code=422, detail=f"Maximum {MAX_FILES} fichiers autorisés")

    uploads = []
    for f in payload.files:
        _validate_upload(f.name, f.mime_type, f.size)
        result = generate_proof_upload_url(f.name, role, str(user_id), challenge_id)
        uploads.append({
            "signed_url": result["signed_url"],
            "path": result["path"],
            "original_filename": f.name,
            "mime_type": f.mime_type,
            "file_size_bytes": f.size,
        })

    return {"uploads": uploads}


@router.post("/{challenge_id}/submit")
def submit_proof(
    challenge_id: str,
    payload: SubmitProofPayload,
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Submit a challenge proof.

    Files are base64-encoded by the browser and sent as plain JSON — the same
    format as every other endpoint (start, decline, etc.).  Railway decodes the
    base64 and uploads the raw bytes to Supabase Storage server-side, which
    eliminates all browser-to-Supabase CORS issues entirely.
    """
    from app.services.storage import upload_challenge_proof, delete_challenge_proof

    user_id = UUID(current_user["id"])
    role = current_user.get("role", "student")
    now = datetime.utcnow()

    part = db.exec(
        select(ChallengeParticipation).where(
            ChallengeParticipation.challenge_id == challenge_id,
            ChallengeParticipation.user_id == user_id,
        )
    ).first()

    if part is None:
        raise HTTPException(status_code=404, detail="Participation introuvable")
    if part.status != "in_progress":
        raise HTTPException(status_code=400, detail="Ce défi n'est pas en cours")
    if part.deadline_at and part.deadline_at < now:
        part.status = "expired"
        db.add(part)
        db.commit()
        raise HTTPException(status_code=400, detail="La durée du défi est expirée")

    challenge = db.get(Challenge, challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail="Défi introuvable")

    pt = challenge.proof_type or "image-or-pdf"

    if pt == "text":
        if not payload.proof_text or not payload.proof_text.strip():
            raise HTTPException(status_code=422, detail="Le texte de preuve est requis")
        part.proof_url = f"text::{payload.proof_text.strip()}"
        part.proof_name = "text-proof"

    elif pt == "url":
        if not payload.proof_url_link or not payload.proof_url_link.strip():
            raise HTTPException(status_code=422, detail="L'URL de preuve est requise")
        url = payload.proof_url_link.strip()
        if not url.startswith(("http://", "https://")):
            raise HTTPException(status_code=422, detail="L'URL doit commencer par http:// ou https://")
        part.proof_url = url
        part.proof_name = "url-proof"

    else:
        if not payload.proof_files:
            raise HTTPException(status_code=422, detail="Au moins un fichier de preuve est requis")
        if len(payload.proof_files) > MAX_FILES:
            raise HTTPException(status_code=422, detail=f"Maximum {MAX_FILES} fichiers autorisés")

        # Decode base64 and validate each file
        file_data: List[tuple] = []
        for fi in payload.proof_files:
            try:
                raw = base64.b64decode(fi.content_base64)
            except Exception:
                raise HTTPException(status_code=422, detail=f"Fichier '{fi.original_filename}' : encodage base64 invalide")
            canonical_mime = _validate_upload(fi.original_filename, fi.mime_type, len(raw))
            file_data.append((fi.original_filename, canonical_mime, raw))

        # Clean up any previous proof files (idempotent re-submission)
        try:
            old_files = db.exec(
                select(ChallengeProofFile).where(
                    ChallengeProofFile.participation_id == part.id
                )
            ).all()
            for old in old_files:
                try:
                    delete_challenge_proof(old.storage_path)
                except Exception:
                    pass
                db.delete(old)
            db.flush()
        except Exception:
            db.rollback()

        # Upload decoded bytes to Supabase Storage server-side
        storage_paths: List[str] = []
        try:
            for i, (filename, mime, raw) in enumerate(file_data):
                path = upload_challenge_proof(
                    file_bytes=raw,
                    original_filename=filename,
                    content_type=mime,
                    role=role,
                    user_id=str(user_id),
                    challenge_id=challenge_id,
                )
                storage_paths.append(path)
                pf = ChallengeProofFile(
                    participation_id=part.id,
                    storage_path=path,
                    original_filename=filename,
                    mime_type=mime,
                    file_size_bytes=len(raw),
                    sort_order=i,
                )
                db.add(pf)
            db.flush()
        except Exception as exc:
            db.rollback()
            raise HTTPException(status_code=500, detail=f"Erreur upload fichier : {exc}")

        part.proof_url = storage_paths[0]
        part.proof_name = file_data[0][0]

    part.proof_message = (payload.proof_message or "").strip() or None
    part.status = "submitted"
    part.submitted_at = now
    db.add(part)
    db.commit()
    db.refresh(part)

    return _participation_out(part, db)
