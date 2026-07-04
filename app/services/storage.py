from __future__ import annotations

import mimetypes
import uuid
from typing import Optional

from app.config import get_settings
from app.database import get_supabase_service

settings = get_settings()

_JUSTIF_FOLDER = "challenge-justifications"


def _bucket():
    return get_supabase_service().storage.from_(settings.supabase_storage_bucket)


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
