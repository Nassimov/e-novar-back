from __future__ import annotations

import mimetypes
import uuid
from typing import Optional

from app.config import get_settings
from app.database import get_supabase_service

settings = get_settings()


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
