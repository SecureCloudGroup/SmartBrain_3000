"""In-process speech-to-text: Whisper (base, int8) via faster-whisper — the wheel
thousands of local apps actually ship.

The zero-touch default under voice.py's resolution chain: no server, no setup. The
model files (~141 MB total) are fetched ONCE — hash-pinned, streamed into the app's
data dir by a background thread that starts at boot — and every state is visible:
byte-accurate download percent, an explicit engine-load phase, and errors that re-arm
themselves. transcribe never blocks on any of it.

Why Whisper and not Moonshine: the field found Moonshine base returning EMPTY on
perfectly healthy captured speech (byte-verified: Whisper transcribed the same audio
flawlessly, 0.47 s on CPU). Robustness on real-world voices is the entire job.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import shutil
import socket
import threading
import wave
from pathlib import Path

import numpy as np

from . import db

log = logging.getLogger(__name__)

_REPO_BASE = "https://huggingface.co/Systran/faster-whisper-base/resolve/main"
# Exact bytes validated on 2026-08-25 against the operator's real captured speech
# (perfect transcript, 0.47 s warm on CPU). Hashed from the benchmarked files; a
# served file that hashes differently is discarded, never loaded.
MODEL_FILES = {
    "model.bin": ("d01c3014881c9c6f3133c182f3d2887eb6ca1c789a7538c5c007196857a0a6a9", 145_217_532),
    "config.json": ("56a6d8110d311f19c8f0471e562832c7527f146b567275bfca59fcf7c184da9a", 2_309),
    "tokenizer.json": ("fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab", 2_203_239),
    "vocabulary.txt": ("34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913", 459_861),
}
_TOTAL_BYTES = sum(size for _, size in MODEL_FILES.values())

_MAX_SECONDS = 120  # transcribe cap; push-to-talk is far shorter
_TARGET_RATE = 16000

# Download/load state, readable without the lock (single writer, torn reads harmless).
# Phases: absent -> downloading (byte-accurate pct) -> loading (engine init, seconds)
#         -> ready | error (retryable — prefetch() re-arms from error/absent).
_state_lock = threading.Lock()
_phase = "absent"
_pct = 0
_error = ""
_model = None  # loaded faster_whisper.WhisperModel, once ready
_run_lock = threading.Lock()  # one transcription at a time; each is sub-second
_fetch_inflight = False


def model_dir() -> Path:
    # Beside the database, wherever that lives (env override, container mount, or the
    # per-OS default) — models are user data with the same lifecycle as the rest.
    return db.resolve_db_path().parent / "models" / "whisper-base"


def status() -> dict:
    return {"phase": _phase, "pct": _pct, "error": _error}


def prefetch(app=None) -> None:
    """Kick the background model fetch (app startup + unlock call it). Idempotent while
    a fetch is in flight or the model is ready; re-arms after an error so a transient
    network failure heals on the next call instead of wedging voice forever.

    The phase flips to "downloading" AT ARM TIME, not when the thread gets scheduled:
    a status read racing the thread start must see motion, or the chat page's retry
    button reads a stale "error" and stops polling.
    """
    if os.environ.get("SMARTBRAIN_NO_VOICE_PREFETCH"):
        return  # test suites and network-forbidden deploys opt out explicitly
    global _fetch_inflight, _phase, _pct, _error
    with _state_lock:
        if _fetch_inflight or _phase == "ready":
            return
        _fetch_inflight = True
        _phase, _pct, _error = "downloading", 0, ""
    threading.Thread(target=_fetch_and_load, name="stt-prefetch", daemon=True).start()


def describe_failure(exc: BaseException) -> str:
    """A fixed, user-facing sentence for a failed download or load — never the raw exception.

    The Status page shows this string and the API returns it, so it must say what to DO
    without echoing library internals (URLs, paths, stack context) to the browser. The
    complete exception goes to the log, where it belongs. Order matters: an HTTP status
    line also contains a URL, so status codes are classified before reachability.
    """
    name = type(exc).__name__
    text = str(exc).lower()
    if (isinstance(exc, OSError) and getattr(exc, "errno", None) == 28) or "no space left" in text:
        return "not enough disk space to download the voice model — free some space and retry"
    if "verification" in text or "checksum" in text or "digest" in text:
        return "the downloaded voice model failed verification — retry the download"
    if "http" in text and any(f" {c}" in text or f"http {c}" in text or f"({c}" in text for c in ("403", "404", "429", "500", "502", "503")):
        return "the model download server refused the request — retry later"
    if isinstance(exc, TimeoutError) or "timed out" in text or "timeout" in name.lower():
        return "the voice model download timed out — check the network and retry"
    if (isinstance(exc, (ConnectionError, socket.gaierror, socket.herror, socket.timeout))
            or any(k in name for k in ("Connection", "URLError", "RemoteDisconnected", "SSL", "gaierror"))
            or "name or service" in text or "nodename" in text or "connection refused" in text):
        return "could not reach the model download (huggingface.co) — check the network and retry"
    if any(k in text for k in ("ctranslate", "whisper", "model.bin", "load")):
        return "the voice engine failed to load its model files — retry the download"
    return "the voice model download failed — retry; if it keeps failing, check the log"


def _fetch_and_load() -> None:
    """The ONLY code that downloads or loads — always on this background thread. A
    request thread never blocks on it."""
    global _phase, _pct, _error, _model, _fetch_inflight
    try:
        directory = model_dir()
        directory.mkdir(parents=True, exist_ok=True)
        _retire_moonshine(directory.parent)
        done_bytes = 0
        for name in sorted(MODEL_FILES):
            digest, size = MODEL_FILES[name]
            dest = directory / name
            if dest.exists() and _sha256(dest) == digest:
                done_bytes += size
                _set_pct(done_bytes)
                continue
            _download(f"{_REPO_BASE}/{name}", dest, digest, done_bytes)
            done_bytes += size
            _set_pct(done_bytes)
        with _state_lock:
            _phase = "loading"  # engine init takes seconds; saying "100%" here is a lie
        model = _load_model(directory)
        with _state_lock:
            _model = model
            _phase, _pct = "ready", 100
        log.info("stt: whisper model ready")
    except Exception as exc:
        with _state_lock:
            _phase, _error = "error", describe_failure(exc)
        log.warning("stt fetch failed: %s", exc)  # the full reason stays in the log
    finally:
        with _state_lock:
            _fetch_inflight = False


def _retire_moonshine(models_root: Path) -> None:
    """Best-effort removal of the retired Moonshine files (~236 MB nobody needs)."""
    try:
        old = models_root / "moonshine-base"
        if old.is_dir():
            shutil.rmtree(old)
            log.info("stt: removed the retired moonshine-base model")
    except Exception as exc:
        log.debug("moonshine cleanup skipped: %s", exc)


def _set_pct(done_bytes: int) -> None:
    global _pct
    _pct = min(99, done_bytes * 100 // _TOTAL_BYTES)  # 100 is "ready", nothing else


def _download(url: str, dest: Path, digest: str, base_bytes: int) -> None:
    """Stream one pinned file to disk with byte-accurate progress; refuse a hash mismatch."""
    import httpx  # lazy: matches the app's import-time discipline for heavy paths

    log.info("stt: downloading %s", dest.name)
    tmp = dest.with_suffix(dest.suffix + ".part")
    hasher = hashlib.sha256()
    with httpx.stream("GET", url, follow_redirects=True, timeout=120.0) as resp:
        if resp.status_code != 200:
            raise RuntimeError(f"model download failed (HTTP {resp.status_code})")
        seen = 0
        with open(tmp, "wb") as f:
            for chunk in resp.iter_bytes(1 << 20):
                f.write(chunk)
                hasher.update(chunk)
                seen += len(chunk)
                _set_pct(base_bytes + seen)
    if hasher.hexdigest() != digest:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"model download failed verification ({dest.name})")
    tmp.replace(dest)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_model(directory: Path):
    from faster_whisper import (
        WhisperModel,  # lazy: native code the non-voice paths never pay for
    )

    return WhisperModel(str(directory), device="cpu", compute_type="int8")


def transcribe_wav(wav_bytes: bytes, *, partial: bool = False) -> str:
    """16 kHz mono 16-bit WAV in, text out. NEVER blocks on the model: not-ready raises
    immediately with the live phase, and absent/error states re-arm the background fetch
    so the system is always healing itself while the user sees honest progress."""
    assert wav_bytes, "audio required"
    with _state_lock:
        model = _model
        phase, pct, err = _phase, _pct, _error
    if model is None:
        if phase in ("absent", "error"):
            prefetch()  # re-arm; the UI polls status and enables the mic when ready
        if phase == "error":
            raise RuntimeError(f"voice model unavailable ({err}) — retrying the download now")
        if phase == "loading":
            raise RuntimeError("voice is almost ready — loading the engine, a few more seconds")
        raise RuntimeError(f"preparing voice ({pct}%) — one-time download, the mic enables itself when ready")
    audio = _decode_wav(wav_bytes)
    with _run_lock:  # CT2 transcription is not guaranteed thread-safe; each call is fast
        # Live snapshots decode greedily: they are re-read every second while the user
        # is still talking, so speed beats the last few percent of accuracy — the final
        # pass at the pause keeps the full beam.
        segments, _info = model.transcribe(audio[0], beam_size=1 if partial else 5)
        text = " ".join(seg.text.strip() for seg in segments)
    return text.strip()


def _decode_wav(wav_bytes: bytes) -> np.ndarray:
    """Stdlib WAV decode -> float32 [1, samples]; the client always sends 16 kHz mono."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            if w.getsampwidth() != 2 or w.getnchannels() != 1:
                raise RuntimeError("expected 16-bit mono WAV audio")
            rate = w.getframerate()
            frames = w.readframes(w.getnframes())
    except (wave.Error, EOFError):
        raise RuntimeError("could not read the audio recording") from None
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if rate != _TARGET_RATE:  # tolerate a stray rate by nearest-sample decimation/repeat
        idx = (np.arange(int(len(samples) * _TARGET_RATE / rate)) * rate / _TARGET_RATE).astype(np.int64)
        samples = samples[np.clip(idx, 0, len(samples) - 1)]
    if len(samples) < 1600:  # <0.1s: nothing meaningful was said
        return np.zeros((1, 1600), dtype=np.float32)
    if len(samples) > _TARGET_RATE * _MAX_SECONDS:
        samples = samples[: _TARGET_RATE * _MAX_SECONDS]
    return samples[None, ...]
