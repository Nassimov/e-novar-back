from __future__ import annotations

import mimetypes
import uuid
from typing import Optional

from app.config import get_settings
from app.database import get_supabase_service

settings = get_settings()

_PROOF_BUCKET = "challenge-proofs"


def _bucket():
    return get_supabase_service().storage.from_(settings.supabase_storage_bucket)


def _proof_bucket():
    return get_supabase_service().storage.from_(_PROOF_BUCKET)


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
    # Strip the public URL prefix to get the storage path
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


# ── Challenge proof files (private bucket) ────────────────────────────────────

def upload_challenge_proof(
    file_bytes: bytes,
    original_filename: str,
    content_type: str,
    role: str,
    user_id: str,
    challenge_id: str,
) -> str:
    """Upload a proof file to the private challenge-proofs bucket. Returns storage_path."""
    ext = original_filename.rsplit(".", 1)[-1].lower() if "." in original_filename else "bin"
    path = f"{role}/{user_id}/{challenge_id}/{uuid.uuid4()}.{ext}"
    _proof_bucket().upload(
        path=path,
        file=file_bytes,
        file_options={"content-type": content_type, "upsert": "false"},
    )
    return path


def get_proof_signed_url(storage_path: str, expires_in: int = 3600) -> str:
    """Generate a signed URL for a challenge proof file."""
    result = _proof_bucket().create_signed_url(storage_path, expires_in)
    if isinstance(result, dict):
        return result.get("signedURL", result.get("signedUrl", ""))
    return ""


def delete_challenge_proof(storage_path: str) -> None:
    """Delete a challenge proof file from the private bucket."""
    try:
        _proof_bucket().remove([storage_path])
    except Exception:
        pass
