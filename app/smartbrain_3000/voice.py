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
import time

import httpx

from . import gateway, stt_local

log = logging.getLogger(__name__)

# After a configured server fails to transcribe, skip it for a while instead of paying
# its round-trips (and log spam) on EVERY mic press — the local engine serves meanwhile.
_SERVER_SKIP_SECONDS = 300.0
_server_skip_until = 0.0

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


def _find_whisper(models: list) -> str:
    """First whisper-family model id in a server catalog, or ""."""
    for mid in models:
        if isinstance(mid, str) and "whisper" in mid.lower():
            return mid
    return ""


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
           "tts_voice": store.get(TTS_VOICE_KEY) or "", "stt_ready": None,
           # The local engine makes dictation unconditionally available; `local` carries
           # its phase/pct so the mic can show "Preparing voice (N%)" honestly.
           "stt_available": True, "local": stt_local.status(),
           "engine": _current_engine(store)}
    if probe and cfg["url"]:
        probed = gateway.probe_mlx(cfg["url"], cfg["api_key"])  # plain /v1/models GET, best-effort
        out["reachable"] = bool(probed.get("reachable"))
        if out["reachable"]:
            models = probed.get("models") or []
            # Ready = the configured model is loaded, or ANY whisper-family model is
            # (transcribe auto-resolves to it). The field test proved a reachable
            # server with no whisper model fails with a 'not found' the UI must warn
            # about BEFORE the user is mid-dictation.
            out["stt_ready"] = stt_model(store) in models or bool(_find_whisper(models))
    return out


def transcribe(store, audio: bytes, content_type: str = "audio/wav") -> str:
    """Send captured audio to the local server's /v1/audio/transcriptions; return the text.

    Self-healing on the model name: servers refuse ids they haven't loaded (the field
    test hit "Model 'whisper-large-v3-turbo' not found"), and every server names its
    whisper differently — on a not-found, look at what the server actually HAS, retry
    with its whisper-family model, and persist the resolved name so the next call is
    direct. A server with NO whisper model gets an error that says exactly that and
    exactly what to do — not a shrug.
    """
    assert audio, "audio bytes required"
    if len(audio) > _MAX_AUDIO_BYTES:
        raise VoiceError(413, "recording too long — try a shorter one")
    global _server_skip_until
    cfg = server_config(store)
    if not cfg["url"] or time.monotonic() < _server_skip_until:
        return _transcribe_local(audio)  # the zero-touch default: in-process Moonshine
    try:
        return _transcribe_server(store, cfg, audio, content_type)
    except VoiceError as exc:
        # A configured server that can't transcribe right now (down, or no whisper
        # model) must not take dictation down with it — the local engine carries on,
        # and for a while we stop paying the server's round-trips on every press.
        _server_skip_until = time.monotonic() + _SERVER_SKIP_SECONDS
        log.warning("voice: server transcription unavailable (%s) — local engine for the next %d min",
                    exc.message, int(_SERVER_SKIP_SECONDS // 60))
        return _transcribe_local(audio)


def reset_server_skip() -> None:
    """Forget a server-failure skip window (config just changed — try it fresh)."""
    global _server_skip_until
    _server_skip_until = 0.0


def _current_engine(store) -> str:
    """Which engine the NEXT dictation will use — the status/Status-page truth."""
    cfg = server_config(store)
    if cfg["url"] and time.monotonic() >= _server_skip_until:
        return "server"
    return "local"


def _transcribe_local(audio: bytes) -> str:
    try:
        return stt_local.transcribe_wav(audio)
    except RuntimeError as exc:
        raise VoiceError(503, str(exc)) from None


def _transcribe_server(store, cfg: dict, audio: bytes, content_type: str) -> str:
    resp = _post_transcription(cfg, audio, content_type, stt_model(store))
    if resp.status_code >= 400:
        reason = _upstream_reason(resp)
        if "not found" not in reason.lower():
            raise VoiceError(502, reason)
        probe = gateway.probe_mlx(cfg["url"], cfg["api_key"])
        resolved = _find_whisper(probe.get("models") or [])
        if not resolved:
            raise VoiceError(503, "the voice server has no transcription (whisper) model loaded")
        resp = _post_transcription(cfg, audio, content_type, resolved)
        if resp.status_code >= 400:
            raise VoiceError(502, _upstream_reason(resp))
        store.put(STT_MODEL_KEY, resolved)  # sticky: next call goes straight there
        log.info("voice: transcription model auto-resolved")
    try:
        text = resp.json().get("text", "")
    except ValueError:
        raise VoiceError(502, "the voice server returned a non-JSON transcription") from None
    return str(text or "").strip()


def _post_transcription(cfg: dict, audio: bytes, content_type: str, model: str) -> httpx.Response:
    headers = {"Authorization": f"Bearer {cfg['api_key']}"} if cfg["api_key"] else {}
    files = {"file": ("audio.wav", audio, content_type or "audio/wav")}
    data = {"model": model, "response_format": "json"}
    try:
        with gateway.serialized_local():
            return httpx.post(f"{cfg['url'].rstrip('/')}/v1/audio/transcriptions",
                              headers=headers, files=files, data=data, timeout=_TRANSCRIBE_TIMEOUT)
    except httpx.TimeoutException:
        raise VoiceError(504, "the voice server took too long — is the model still loading?") from None
    except httpx.HTTPError as exc:
        raise VoiceError(502, f"could not reach the voice server: {exc.__class__.__name__}") from None


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
