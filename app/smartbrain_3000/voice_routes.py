"""HTTP surface for voice: transcribe (STT), speak (server TTS), and status.

All three require an unlocked vault (the server config lives in the encrypted secret
store) and none are Desktop-local: dictation from a paired phone is the point — its
audio arrives over the WebRTC bridge exactly like an upload, is transcribed on the
Desktop by the user's own local server, and never touches a third party.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.responses import Response

from . import moonshine, voice

router = APIRouter()

_MAX_BODY = 15 * 1024 * 1024  # mirror voice._MAX_AUDIO_BYTES at the transport edge


def _store(request: Request):
    store = getattr(request.app.state, "secret_store", None)
    if store is None:
        raise HTTPException(status_code=423, detail="locked: unlock first")
    return store


class SpeakIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@router.get("/api/voice/status")
def voice_status(request: Request, probe: int = 0, prepare: int = 0) -> dict:
    if prepare:
        moonshine.prefetch(request.app)  # idempotent; re-arms after an error
    return voice.status(_store(request), probe=bool(probe))


@router.post("/api/voice/transcribe")
async def voice_transcribe(request: Request) -> dict:
    """Raw audio body in (the client's 16 kHz mono WAV), transcript text out."""
    store = _store(request)
    audio = await request.body()
    if not audio:
        raise HTTPException(status_code=422, detail="empty audio body")
    if len(audio) > _MAX_BODY:
        raise HTTPException(status_code=413, detail="recording too long — try a shorter one")
    try:
        text = voice.transcribe(store, audio, request.headers.get("content-type", "audio/wav"))
    except voice.VoiceError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from None
    return {"text": text}


@router.post("/api/voice/speak")
def voice_speak(request: Request, body: SpeakIn) -> Response:
    """One spoken chunk of audio for ``text`` — the fallback when the browser has no
    system voices (Linux desktops) or the user configured a premium local voice."""
    store = _store(request)
    try:
        audio, content_type = voice.speak(store, body.text)
    except voice.VoiceError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from None
    return Response(content=audio, media_type=content_type)
