from __future__ import annotations

import mimetypes
import uuid
from typing import Iterable, Optional

from app.config import get_settings
from app.database import get_supabase_service

settings = get_settings()

_JUSTIF_FOLDER = "challenge-justifications"


def _bucket():
    return get_supabase_service().storage.from_(settings.supabase_storage_bucket)


# ── Server-side upload guards (extension + declared MIME allow-list, size cap) ──
#
# A client can lie about Content-Type, so every endpoint that accepts an
# UploadFile must check BOTH the filename extension and the declared MIME
# type against an allow-list, and cap the byte size, before the bytes are
# ever handed to Supabase Storage. This does not sniff magic bytes (no
# python-magic dependency in this project), so it is not a defense against a
# maliciously-crafted file whose bytes don't match its declared type — but it
# does close the "no check at all" gap where any extension/any content-type/
# any size was accepted.

# Documents: diplomas, CVs, teacher onboarding paperwork.
DOCUMENT_CONTENT_TYPES = {
    "application/pdf",
    "image/jpeg", "image/png", "image/webp",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
DOCUMENT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".doc", ".docx"}
DOCUMENT_MAX_SIZE = 15 * 1024 * 1024  # 15 MB

# Chat attachments and classroom session files: broader mix, same allow-list
# used by app/routers/files.py's general-purpose /upload endpoint.
GENERAL_CONTENT_TYPES = {
    "image/jpeg", "image/png", "image/webp", "image/gif",
    "application/pdf",
    "video/mp4", "video/webm",
    "audio/mpeg", "audio/wav",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
GENERAL_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif",
    ".pdf",
    ".mp4", ".webm",
    ".mp3", ".wav",
    ".doc", ".docx", ".xls", ".xlsx",
}

# Dispute/evidence attachments: images, PDF, short video clips.
EVIDENCE_CONTENT_TYPES = {
    "image/jpeg", "image/png", "image/webp",
    "application/pdf",
    "video/mp4", "video/webm", "video/quicktime",
}
EVIDENCE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".pdf", ".mp4", ".webm", ".mov"}
EVIDENCE_MAX_SIZE = 20 * 1024 * 1024  # 20 MB


class UploadValidationError(ValueError):
    """Raised by validate_upload() — callers translate this into a 400."""


def validate_upload(
    *,
    filename: Optional[str],
    content_type: Optional[str],
    size: int,
    allowed_content_types: Iterable[str],
    allowed_extensions: Iterable[str],
    max_size: int,
) -> None:
    """Server-side allow-list + size guard for an uploaded file.

    Both the filename extension AND the client-declared content-type must be
    on their respective allow-lists — a client lying about one alone isn't
    enough to smuggle a disallowed type through. Raises UploadValidationError
    with a user-facing message on any violation; callers turn that into an
    HTTPException(400).
    """
    if size <= 0:
        raise UploadValidationError("Le fichier est vide")
    if size > max_size:
        raise UploadValidationError(f"Fichier trop volumineux (max {max_size // (1024 * 1024)} Mo)")

    name = filename or ""
    ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if ext not in set(allowed_extensions):
        raise UploadValidationError(f"Extension de fichier '{ext or '(aucune)'}' non autorisée")

    if content_type not in set(allowed_content_types):
        raise UploadValidationError(f"Type de fichier '{content_type}' non autorisé")


def upload_file(
    file_bytes: bytes,
    filename: str,
    content_type: Optional[str] = None,
    folder: str = "uploads",
) -> str:
    """Upload a file to Supabase Storage. Returns the public URL."""
    ext = filename.rsplit(".", 1)[-1] if "." in filename else "bin"
    path = f"{folder}/{uuid.uuid4()}.{ext}"

    if content_type is None:
        content_type, _ = mimetypes.guess_type(filename)
        content_type = content_type or "application/octet-stream"

    _bucket().upload(
        path=path,
        file=file_bytes,
        file_options={"content-type": content_type, "upsert": "false"},
    )

    return _bucket().get_public_url(path)


def delete_file(path: str) -> None:
    """Delete a file from Supabase Storage by its storage path or full public URL."""
    prefix = f"{settings.supabase_url}/storage/v1/object/public/{settings.supabase_storage_bucket}/"
    if path.startswith(prefix):
        path = path[len(prefix):]
    _bucket().remove([path])


def get_signed_url(path: str, expires_in: int = 3600) -> str:
    """Generate a signed URL for private/temporary file access."""
    prefix = f"{settings.supabase_url}/storage/v1/object/public/{settings.supabase_storage_bucket}/"
    if path.startswith(prefix):
        path = path[len(prefix):]
    result = _bucket().create_signed_url(path, expires_in)
    return result.get("signedURL", "")


# ── Challenge justification files (enovar-files/challenge-justifications/) ─────

def upload_challenge_proof(
    file_bytes: bytes,
    original_filename: str,
    content_type: str,
    role: str,
    user_id: str,
    challenge_id: str,
) -> str:
    """Upload a proof file to enovar-files/challenge-justifications/.
    Returns the storage path relative to the bucket root."""
    ext = original_filename.rsplit(".", 1)[-1].lower() if "." in original_filename else "bin"
    path = f"{_JUSTIF_FOLDER}/{user_id}/{challenge_id}/{uuid.uuid4()}.{ext}"
    _bucket().upload(
        path=path,
        file=file_bytes,
        file_options={"content-type": content_type, "upsert": "false"},
    )
    return path


def get_proof_public_url(storage_path: str) -> str:
    """Return the public URL for a challenge proof file in enovar-files."""
    return _bucket().get_public_url(storage_path)


def list_folder(folder: str) -> list[dict]:
    """List files in a storage folder. Returns [{name, path}] — directories excluded."""
    try:
        entries = _bucket().list(folder)
        return [
            {"name": e["name"], "path": f"{folder}/{e['name']}"}
            for e in (entries or [])
            if e.get("id") and not e.get("metadata", {}).get("size") == 0
        ]
    except Exception:
        return []


def delete_challenge_proof(storage_path: str) -> None:
    """Delete a challenge proof file from enovar-files."""
    try:
        _bucket().remove([storage_path])
    except Exception:
        pass
