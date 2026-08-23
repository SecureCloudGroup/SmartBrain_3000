"""Voice: speech-to-text and text-to-speech through a LOCAL OpenAI-compatible server.

The same shape as embeddings — the heavy model runs in a server the user configured
(oMLX on Apple Silicon serves /v1/audio/* natively; whisper.cpp-family or speaches
servers do the same on Windows/Linux) and SmartBrain proxies to it. Audio never leaves
the user's machines: the PWA's microphone bytes ride the encrypted WebRTC bridge to the
Desktop, and the Desktop talks only to a localhost/LAN server the user chose.

Calls go DIRECT to the server (not through Bifrost — it doesn't proxy audio) and hold
the local-model semaphore, so a transcription can never overlap a local chat generation
(single-request-at-a-time servers wedge otherwise; see gateway._LOCAL_SEM).
"""

from __future__ import annotations

import logging

import httpx

from . import gateway

log = logging.getLogger(__name__)

STT_MODEL_KEY = "voice:stt_model"
TTS_MODEL_KEY = "voice:tts_model"
TTS_VOICE_KEY = "voice:tts_voice"
# oMLX's whisper naming; other servers need the model the user pulled (set in Settings).
DEFAULT_STT_MODEL = "whisper-large-v3-turbo"

_MAX_AUDIO_BYTES = 15 * 1024 * 1024  # ~7 min of 16 kHz mono WAV; push-to-talk is far shorter
_MAX_SPEAK_CHARS = 4000  # one spoken chunk; the UI sends sentences, not essays
_TRANSCRIBE_TIMEOUT = 60.0  # first call may load the model from cold
_SPEAK_TIMEOUT = 60.0


class VoiceError(Exception):
    """A voice problem worth showing the user, with an HTTP-ish status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def server_config(store) -> dict:
    """Resolve the audio server: the dedicated voice server, else the MLX server.

    Returns {url, api_key, source} with url == "" when nothing is configured —
    the caller decides whether that's a 503 (transcribe) or just "unavailable" (status).
    """
    assert store is not None, "secret store required"
    url = store.get(gateway.VOICE_URL_KEY)
    if url:
        return {"url": gateway.localize_local_url(url), "api_key": store.get(gateway.VOICE_KEY_KEY) or "",
                "source": "voice"}
    url = store.get(gateway.MLX_URL_KEY)
    if url:
        return {"url": gateway.localize_local_url(url), "api_key": store.get(gateway.MLX_KEY_KEY) or "",
                "source": "mlx"}
    return {"url": "", "api_key": "", "source": ""}


def stt_model(store) -> str:
    return store.get(STT_MODEL_KEY) or DEFAULT_STT_MODEL


def status(store, probe: bool = False) -> dict:
    """What the voice UI needs to decide what to show: is STT/TTS worth offering?

    ``probe`` (settings page only) live-checks the server's /v1/models — several seconds
    against a dead host, so the chat page's per-mount call must NOT pay it; reachable is
    None when unprobed. Server TTS being configured is what tts_model says; browser
    system voices are the client's business and invisible from here.
    """
    cfg = server_config(store)
    out = {"configured": bool(cfg["url"]), "source": cfg["source"], "reachable": None,
           "stt_model": stt_model(store), "tts_model": store.get(TTS_MODEL_KEY) or "",
           "tts_voice": store.get(TTS_VOICE_KEY) or ""}
    if probe and cfg["url"]:
        probed = gateway.probe_mlx(cfg["url"], cfg["api_key"])  # plain /v1/models GET, best-effort
        out["reachable"] = bool(probed.get("reachable"))
    return out


def transcribe(store, audio: bytes, content_type: str = "audio/wav") -> str:
    """Send captured audio to the local server's /v1/audio/transcriptions; return the text."""
    assert audio, "audio bytes required"
    if len(audio) > _MAX_AUDIO_BYTES:
        raise VoiceError(413, "recording too long — try a shorter one")
    cfg = server_config(store)
    if not cfg["url"]:
        raise VoiceError(503, "no voice server configured — add one under Settings → Local models")
    headers = {"Authorization": f"Bearer {cfg['api_key']}"} if cfg["api_key"] else {}
    files = {"file": ("audio.wav", audio, content_type or "audio/wav")}
    data = {"model": stt_model(store), "response_format": "json"}
    try:
        with gateway.serialized_local():
            resp = httpx.post(f"{cfg['url'].rstrip('/')}/v1/audio/transcriptions",
                              headers=headers, files=files, data=data, timeout=_TRANSCRIBE_TIMEOUT)
    except httpx.TimeoutException:
        raise VoiceError(504, "the voice server took too long — is the model still loading?") from None
    except httpx.HTTPError as exc:
        raise VoiceError(502, f"could not reach the voice server: {exc.__class__.__name__}") from None
    if resp.status_code >= 400:
        raise VoiceError(502, _upstream_reason(resp))
    try:
        text = resp.json().get("text", "")
    except ValueError:
        raise VoiceError(502, "the voice server returned a non-JSON transcription") from None
    return str(text or "").strip()


def speak(store, text: str) -> tuple[bytes, str]:
    """Server-side TTS via /v1/audio/speech; returns (audio bytes, content type).

    Only used when the browser has no system voices (Linux desktops, mainly) or the
    user configured a premium local voice — requires ``voice:tts_model`` to be set.
    """
    assert text, "text required"
    cfg = server_config(store)
    if not cfg["url"]:
        raise VoiceError(503, "no voice server configured — add one under Settings → Local models")
    model = store.get(TTS_MODEL_KEY) or ""
    if not model:
        raise VoiceError(503, "no server voice model configured — set one under Settings → Local models")
    payload = {"model": model, "input": text[:_MAX_SPEAK_CHARS]}
    voice_name = store.get(TTS_VOICE_KEY) or ""
    if voice_name:
        payload["voice"] = voice_name
    headers = {"Authorization": f"Bearer {cfg['api_key']}"} if cfg["api_key"] else {}
    try:
        with gateway.serialized_local():
            resp = httpx.post(f"{cfg['url'].rstrip('/')}/v1/audio/speech",
                              headers=headers, json=payload, timeout=_SPEAK_TIMEOUT)
    except httpx.TimeoutException:
        raise VoiceError(504, "the voice server took too long — is the model still loading?") from None
    except httpx.HTTPError as exc:
        raise VoiceError(502, f"could not reach the voice server: {exc.__class__.__name__}") from None
    if resp.status_code >= 400:
        raise VoiceError(502, _upstream_reason(resp))
    return resp.content, resp.headers.get("content-type", "audio/wav")


def _upstream_reason(resp: httpx.Response) -> str:
    """A short, user-facing reason from an upstream error body (JSON error or status)."""
    try:
        detail = resp.json()
        message = detail.get("error", {}).get("message") if isinstance(detail.get("error"), dict) else None
        message = message or detail.get("detail") or detail.get("message")
        if message:
            return f"voice server error: {str(message)[:200]}"
    except ValueError:
        pass
    return f"voice server error (HTTP {resp.status_code})"
