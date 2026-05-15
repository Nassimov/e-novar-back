from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel

from app.dependencies import get_current_user
from app.services.storage import delete_file, upload_file

router = APIRouter(tags=["files"])

ALLOWED_CONTENT_TYPES = {
    "image/jpeg", "image/png", "image/webp", "image/gif",
    "application/pdf",
    "video/mp4", "video/webm",
    "audio/mpeg", "audio/wav",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_file_endpoint(
    file: UploadFile = File(...),
    folder: str = Form(default="uploads"),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Upload a file to Supabase Storage."""
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"File type '{file.content_type}' is not allowed",
        )

    contents = await file.read()
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File too large (max 50MB)")

    # Prefix folder with user ID for organization
    user_folder = f"{folder}/{current_user['id']}"

    url = upload_file(
        file_bytes=contents,
        filename=file.filename or "upload",
        content_type=file.content_type,
        folder=user_folder,
    )

    return {
        "url": url,
        "filename": file.filename,
        "content_type": file.content_type,
        "size_bytes": len(contents),
    }


@router.delete("/{key:path}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_file_endpoint(
    key: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Delete a file from Supabase Storage by path."""
    # Security: file path must start with the user's UUID segment (exact prefix, not substring match)
    user_id = current_user["id"]
    role = current_user.get("role", "student")

    # Normalise separators and check that the key belongs to this user.
    # Accepted patterns: "{user_id}/...", "avatars/{user_id}/...", etc.
    # We require the user_id to appear as a complete path segment (surrounded by '/' or at start/end).
    import re as _re
    owns_file = bool(_re.search(r"(^|/)" + _re.escape(user_id) + r"(/|$)", key))

    if role != "admin" and not owns_file:
        raise HTTPException(
            status_code=403,
            detail="You can only delete your own files",
        )

    try:
        delete_file(key)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to delete file: {exc}")

    return None
