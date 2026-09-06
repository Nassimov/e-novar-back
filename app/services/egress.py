"""LiveKit Egress — session recording (RoomCompositeEgress uploaded to an
S3-compatible bucket). See app/config.py's EGRESS_S3_* settings (pointed at
Supabase Storage's own S3-compatible endpoint by default) and
app/routers/classroom.py's recording endpoints.

No LiveKit webhook receiver exists in this app, so the final file URL/
duration are only known once ListEgress is polled after the upload
finishes — see get_egress_status, called on every GET .../recordings.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from livekit.api import LiveKitAPI

from app.config import get_settings

settings = get_settings()


def is_configured() -> bool:
    return bool(
        settings.egress_s3_endpoint and settings.egress_s3_access_key
        and settings.egress_s3_secret_key and settings.egress_s3_bucket
    )


def _s3_upload():
    from livekit.api import S3Upload

    return S3Upload(
        access_key=settings.egress_s3_access_key,
        secret=settings.egress_s3_secret_key,
        region=settings.egress_s3_region,
        endpoint=settings.egress_s3_endpoint,
        bucket=settings.egress_s3_bucket,
        force_path_style=True,
    )


async def start_recording(room_name: str, session_id: str) -> Dict[str, Any]:
    """Starts a room-composite (camera+screen-share layout, mixed audio)
    recording, uploaded as an MP4. Returns the raw EgressInfo fields we
    persist (egress_id, status)."""
    from livekit.api import EncodedFileOutput, EncodedFileType, RoomCompositeEgressRequest

    async with LiveKitAPI(settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret) as lkapi:
        info = await lkapi.egress.start_room_composite_egress(
            RoomCompositeEgressRequest(
                room_name=room_name,
                layout="speaker",
                file_outputs=[
                    EncodedFileOutput(
                        file_type=EncodedFileType.MP4,
                        filepath=f"recordings/{session_id}/{{time}}.mp4",
                        s3=_s3_upload(),
                    )
                ],
            )
        )
        return {"egress_id": info.egress_id, "status": _status_str(info.status)}


async def stop_recording(egress_id: str) -> Dict[str, Any]:
    from livekit.api import StopEgressRequest

    async with LiveKitAPI(settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret) as lkapi:
        info = await lkapi.egress.stop_egress(StopEgressRequest(egress_id=egress_id))
        return {"egress_id": info.egress_id, "status": _status_str(info.status)}


async def get_egress_status(egress_id: str) -> Optional[Dict[str, Any]]:
    """Polled on read (no webhook receiver) — returns the current status and,
    once available, the uploaded file's location and duration."""
    from livekit.api import ListEgressRequest

    async with LiveKitAPI(settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret) as lkapi:
        resp = await lkapi.egress.list_egress(ListEgressRequest(egress_id=egress_id))
        if not resp.items:
            return None
        info = resp.items[0]
        file_result = info.file_results[0] if info.file_results else None
        return {
            "status": _status_str(info.status),
            "file_url": file_result.location if file_result else None,
            "duration_sec": round(file_result.duration / 1_000_000_000) if file_result and file_result.duration else None,
        }


def _status_str(status: Any) -> str:
    """EgressStatus (from livekit.protocol.egress) collapsed to this app's
    simpler 'active' | 'ending' | 'complete' | 'failed'."""
    from livekit.protocol.egress import EgressStatus

    if status == EgressStatus.EGRESS_COMPLETE:
        return "complete"
    if status in (EgressStatus.EGRESS_FAILED, EgressStatus.EGRESS_ABORTED, EgressStatus.EGRESS_LIMIT_REACHED):
        return "failed"
    if status == EgressStatus.EGRESS_ENDING:
        return "ending"
    return "active"
