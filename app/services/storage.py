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

_PUBLIC_FALLBACK_PREFIX = "public::"


def generate_proof_upload_url(
    original_filename: str,
    role: str,
    user_id: str,
    challenge_id: str,
) -> dict:
    """Generate a presigned upload URL for a challenge proof file.
    The browser uploads directly to Supabase using this URL (PUT request).
    Returns { signed_url, path } where path is the storage path to record in DB."""
    ext = original_filename.rsplit(".", 1)[-1].lower() if "." in original_filename else "bin"
    relative_path = f"challenge-proofs/{role}/{user_id}/{challenge_id}/{uuid.uuid4()}.{ext}"

    # Try private bucket first, fall back to main bucket
    try:
        result = _proof_bucket().create_signed_upload_url(relative_path)
        storage_path = relative_path
    except Exception:
        result = _bucket().create_signed_upload_url(relative_path)
        storage_path = f"{_PUBLIC_FALLBACK_PREFIX}{relative_path}"

    signed_url = result.get("signed_url", result.get("signedUrl", result.get("signedURL", "")))
    return {"signed_url": signed_url, "path": storage_path}


def upload_challenge_proof(
    file_bytes: bytes,
    original_filename: str,
    content_type: str,
    role: str,
    user_id: str,
    challenge_id: str,
) -> str:
    """Upload a proof file to the private challenge-proofs bucket (server-side path).
    Falls back to the main public bucket if the private bucket hasn't been created yet."""
    ext = original_filename.rsplit(".", 1)[-1].lower() if "." in original_filename else "bin"
    path = f"{role}/{user_id}/{challenge_id}/{uuid.uuid4()}.{ext}"
    try:
        _proof_bucket().upload(
            path=path,
            file=file_bytes,
            file_options={"content-type": content_type, "upsert": "false"},
        )
        return path
    except Exception:
        # Private bucket 'challenge-proofs' not created yet — store in main bucket temporarily
        fallback_path = f"challenge-proofs/{role}/{user_id}/{challenge_id}/{uuid.uuid4()}.{ext}"
        _bucket().upload(
            path=fallback_path,
            file=file_bytes,
            file_options={"content-type": content_type, "upsert": "false"},
        )
        return f"{_PUBLIC_FALLBACK_PREFIX}{fallback_path}"


def get_proof_signed_url(storage_path: str, expires_in: int = 3600) -> str:
    """Generate a signed URL for a challenge proof file.
    For files stored in the public bucket fallback, returns the public URL directly."""
    if storage_path.startswith(_PUBLIC_FALLBACK_PREFIX):
        real_path = storage_path[len(_PUBLIC_FALLBACK_PREFIX):]
        return _bucket().get_public_url(real_path)
    try:
        result = _proof_bucket().create_signed_url(storage_path, expires_in)
        if isinstance(result, dict):
            return result.get("signedURL", result.get("signedUrl", ""))
        return ""
    except Exception:
        return ""


def delete_challenge_proof(storage_path: str) -> None:
    """Delete a challenge proof file from the private or fallback bucket."""
    try:
        if storage_path.startswith(_PUBLIC_FALLBACK_PREFIX):
            _bucket().remove([storage_path[len(_PUBLIC_FALLBACK_PREFIX):]])
        else:
            _proof_bucket().remove([storage_path])
    except Exception:
        pass
