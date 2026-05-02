from __future__ import annotations

import mimetypes
import uuid
from typing import Optional

import boto3
from botocore.config import Config

from app.config import get_settings

settings = get_settings()

_s3_client = None


def get_s3_client():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client(
            "s3",
            endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            config=Config(signature_version="s3v4"),
            region_name="auto",
        )
    return _s3_client


def upload_file(
    file_bytes: bytes,
    filename: str,
    content_type: Optional[str] = None,
    folder: str = "uploads",
) -> str:
    """Upload a file to Cloudflare R2. Returns the public URL."""
    client = get_s3_client()

    ext = filename.rsplit(".", 1)[-1] if "." in filename else "bin"
    unique_name = f"{folder}/{uuid.uuid4()}.{ext}"

    if content_type is None:
        content_type, _ = mimetypes.guess_type(filename)
        content_type = content_type or "application/octet-stream"

    client.put_object(
        Bucket=settings.r2_bucket_name,
        Key=unique_name,
        Body=file_bytes,
        ContentType=content_type,
    )

    return f"{settings.r2_public_url}/{unique_name}"


def delete_file(key: str) -> None:
    """Delete a file from Cloudflare R2 by its key."""
    client = get_s3_client()
    # Strip the public URL prefix if present
    if key.startswith(settings.r2_public_url):
        key = key[len(settings.r2_public_url):].lstrip("/")
    client.delete_object(Bucket=settings.r2_bucket_name, Key=key)


def get_presigned_url(key: str, expires_in: int = 3600) -> str:
    """Generate a presigned URL for private file access."""
    client = get_s3_client()
    if key.startswith(settings.r2_public_url):
        key = key[len(settings.r2_public_url):].lstrip("/")
    url = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.r2_bucket_name, "Key": key},
        ExpiresIn=expires_in,
    )
    return url
