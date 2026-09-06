"""LiveKit Agent worker — live captions for classroom sessions.

Joins every classroom room automatically (default/automatic dispatch — see
WorkerOptions below, agent_name left unset), subscribes to each remote
participant's microphone track, streams the audio through Deepgram, and
publishes each transcript back to the room via LiveKit's own native
transcription protocol (rtc.Transcription / TranscriptionSegment). Any
connected client picks these up for free via RoomEvent.TranscriptionReceived
(see @livekit/components-react's useTrackTranscription, or a plain
room.on(RoomEvent.TranscriptionReceived, ...) listener) — no custom relay
needed, mirroring how the whiteboard now rides LiveKit's own data channel
instead of the app's bespoke classroom WebSocket.

This is intentionally NOT built on the high-level AgentSession recipe
(livekit.agents.voice) — that abstraction is designed around a single voice
assistant driving one conversation, whereas a classroom needs EVERY
participant's mic transcribed independently and simultaneously, with no LLM
or TTS involved at all. Subscribing to tracks and running STT directly is
the documented low-level pattern for exactly this "transcription bot" case.

Deployment
----------
This is a separate, long-running WORKER process — not an HTTP endpoint —
and must run as its own Railway service pointed at this same repo, with a
custom start command (see the `agent` line in Procfile). It reuses the
existing LIVEKIT_URL/LIVEKIT_API_KEY/LIVEKIT_API_SECRET and additionally
needs DEEPGRAM_API_KEY. Requires `pip install livekit-agents
livekit-plugins-deepgram` (see requirements.txt).
"""
from __future__ import annotations

import asyncio
import logging

from livekit import rtc
from livekit.agents import JobContext, WorkerOptions, cli
from livekit.agents.stt import SpeechEventType
from livekit.plugins import deepgram

logger = logging.getLogger("enovar.transcription_agent")

# Deepgram's own expected input format — AudioStream resamples to this from
# whatever the publisher actually sent.
_SAMPLE_RATE = 16000
_NUM_CHANNELS = 1


async def _transcribe_track(room: rtc.Room, participant: rtc.RemoteParticipant, track: rtc.Track) -> None:
    """Runs for the lifetime of one subscribed audio track: forwards its
    frames into a dedicated Deepgram stream and publishes each transcript
    segment, attributed to this specific participant/track, back to the
    room. Returns when the audio stream ends (track unpublished, or the
    participant leaves)."""
    stt = deepgram.STT(sample_rate=_SAMPLE_RATE, detect_language=True, interim_results=False)
    stt_stream = stt.stream()
    audio_stream = rtc.AudioStream(track, sample_rate=_SAMPLE_RATE, num_channels=_NUM_CHANNELS)

    async def _forward_audio() -> None:
        try:
            async for event in audio_stream:
                stt_stream.push_frame(event.frame)
        finally:
            stt_stream.end_input()

    forward_task = None
    try:
        forward_task = asyncio.create_task(_forward_audio())

        segment_seq = 0
        async for speech_event in stt_stream:
            if speech_event.type != SpeechEventType.FINAL_TRANSCRIPT:
                continue
            if not speech_event.alternatives:
                continue
            text = speech_event.alternatives[0].text.strip()
            if not text:
                continue
            segment_seq += 1
            try:
                await room.local_participant.publish_transcription(
                    rtc.Transcription(
                        participant_identity=participant.identity,
                        track_sid=track.sid,
                        segments=[
                            rtc.TranscriptionSegment(
                                id=f"{track.sid}-{segment_seq}",
                                text=text,
                                start_time=0,
                                end_time=0,
                                language=speech_event.alternatives[0].language or "",
                                final=True,
                            )
                        ],
                    )
                )
            except Exception:
                logger.exception("Failed to publish transcription segment for %s", participant.identity)
    finally:
        if forward_task is not None:
            forward_task.cancel()
        await stt_stream.aclose()


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    room = ctx.room
    tasks: dict[str, asyncio.Task[None]] = {}

    def _on_track_subscribed(track: rtc.Track, publication: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant) -> None:
        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return
        logger.info("Transcribing audio track sid=%s participant=%s", track.sid, participant.identity)
        tasks[track.sid] = asyncio.create_task(_transcribe_track(room, participant, track))

    def _on_track_unsubscribed(track: rtc.Track, publication: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant) -> None:
        task = tasks.pop(track.sid, None)
        if task is not None:
            task.cancel()

    room.on("track_subscribed", _on_track_subscribed)
    room.on("track_unsubscribed", _on_track_unsubscribed)

    # Participants who joined before this agent finished connecting also
    # need their already-published tracks picked up.
    for participant in room.remote_participants.values():
        for publication in participant.track_publications.values():
            if publication.track is not None and publication.track.kind == rtc.TrackKind.KIND_AUDIO:
                _on_track_subscribed(publication.track, publication, participant)


if __name__ == "__main__":
    # agent_name intentionally left unset (automatic dispatch) — this agent
    # joins every room the app creates, since LiveKit is only ever used here
    # for classroom sessions. Run with: python -m app.agents.transcription_agent start
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
