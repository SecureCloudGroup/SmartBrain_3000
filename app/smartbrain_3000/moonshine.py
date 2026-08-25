"""In-process speech-to-text: Moonshine (base) on ONNX Runtime — voice that just works.

The zero-touch default under voice.py's resolution chain: no server to run, no model
to name, nothing to configure. The two model graphs (~236 MB total) are fetched ONCE —
hash-pinned, straight into the app's data dir — by a background thread at unlock, so
the first mic press normally finds them ready; a press that arrives earlier gets an
honest "preparing (N%)" instead of a stall. English-only and base-tier accuracy by
design; a configured audio server (oMLX with whisper etc.) silently outranks this.

The inference loop is vendored from Useful Sensors' `useful-moonshine-onnx` package
(MIT — see THIRD_PARTY_LICENSES.md) minus its librosa audio loader: the client always
sends 16 kHz mono WAV, decoded here with the stdlib. Vendoring (~100 lines) spares the
wheelhouse the librosa->numba dependency chain, which doesn't even build on every
Python we may ship.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import threading
import wave
from pathlib import Path

import numpy as np

from . import db

log = logging.getLogger(__name__)

_REPO_BASE = "https://huggingface.co/UsefulSensors/moonshine/resolve/main/onnx/merged/base/float"
# Exact bytes validated on 2026-08-24 (0.86 s for an 8 s utterance, CPU-only, M4 Max).
# A served file that hashes differently is discarded, never loaded. Sizes ride along so
# download progress is byte-accurate across BOTH files (per-file windows lied at 100%
# while the second file still streamed — the field caught it).
MODEL_FILES = {
    "encoder_model.onnx":
        ("153e128e7abd64a74ee47f2c3f585c3171c4d46cbb368b032827934c4e01e779", 80_818_781),
    "decoder_model_merged.onnx":
        ("58778763ca8438963190244d6b26572bdca2cedec56a4b91e828f3f2d69ef3c5", 166_211_345),
}
_TOTAL_BYTES = sum(size for _, size in MODEL_FILES.values())
_TOKENIZER = Path(__file__).parent / "assets" / "moonshine-tokenizer.json"

_MAX_SECONDS = 64  # Moonshine's per-call ceiling; push-to-talk is far shorter
_MAX_TOKENS = 192

# Download/load state, readable without the lock (single writer, torn reads harmless).
# Phases: absent -> downloading (byte-accurate pct) -> loading (engine init, seconds)
#         -> ready | error (retryable — prefetch() re-arms from error/absent).
_state_lock = threading.Lock()
_phase = "absent"
_pct = 0
_error = ""
_model = None  # loaded MoonshineModel, once ready
_fetch_inflight = False


def model_dir() -> Path:
    # Beside the database, wherever that lives (env override, container mount, or the
    # per-OS default) — models are user data with the same lifecycle as the rest.
    return db.resolve_db_path().parent / "models" / "moonshine-base"


def status() -> dict:
    return {"phase": _phase, "pct": _pct, "error": _error}


def prefetch(app=None) -> None:
    """Kick the background model fetch (app startup + unlock call it). Idempotent while
    a fetch is in flight or the model is ready; re-arms after an error so a transient
    network failure heals on the next call instead of wedging voice forever.

    The phase flips to "downloading" AT ARM TIME, not when the thread gets scheduled:
    a status read racing the thread start must see motion, or the chat page's retry
    button reads a stale "error" and stops polling (the review's wedge scenario).
    """
    if os.environ.get("SMARTBRAIN_NO_VOICE_PREFETCH"):
        return  # test suites and network-forbidden deploys opt out explicitly
    global _fetch_inflight, _phase, _pct, _error
    with _state_lock:
        if _fetch_inflight or _phase == "ready":
            return
        _fetch_inflight = True
        _phase, _pct, _error = "downloading", 0, ""
    threading.Thread(target=_fetch_and_load, name="moonshine-prefetch", daemon=True).start()


def _fetch_and_load() -> None:
    """The ONLY code that downloads or loads — always on this background thread. A
    request thread never blocks on it (the field test's five-minute hang was transcribe
    downloading 236 MB inline while the UI showed nothing)."""
    global _phase, _pct, _error, _model, _fetch_inflight
    try:
        directory = model_dir()
        directory.mkdir(parents=True, exist_ok=True)
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
        log.info("moonshine: model ready")
    except Exception as exc:
        with _state_lock:
            _phase, _error = "error", str(exc)[:200]
        log.warning("moonshine fetch failed: %s", exc)
    finally:
        with _state_lock:
            _fetch_inflight = False


def _set_pct(done_bytes: int) -> None:
    global _pct
    _pct = min(99, done_bytes * 100 // _TOTAL_BYTES)  # 100 is "ready", nothing else


def _download(url: str, dest: Path, digest: str, base_bytes: int) -> None:
    """Stream one pinned file to disk with byte-accurate progress; refuse a hash mismatch."""
    import httpx  # lazy: matches the app's import-time discipline for heavy paths

    log.info("moonshine: downloading %s", dest.name)
    tmp = dest.with_suffix(".part")
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


def transcribe_wav(wav_bytes: bytes) -> str:
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
    tokens = model.generate(audio)
    import tokenizers  # lazy for the same reason as httpx above

    text = tokenizers.Tokenizer.from_file(str(_TOKENIZER)).decode_batch(tokens)[0]
    return str(text or "").strip()


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
    if rate != 16000:  # tolerate a stray rate by nearest-sample decimation/repeat
        idx = (np.arange(int(len(samples) * 16000 / rate)) * rate / 16000).astype(np.int64)
        samples = samples[np.clip(idx, 0, len(samples) - 1)]
    if len(samples) < 1600:  # <0.1s: Moonshine's floor — nothing meaningful was said
        return np.zeros((1, 1600), dtype=np.float32)
    if len(samples) > 16000 * _MAX_SECONDS:
        samples = samples[: 16000 * _MAX_SECONDS]
    return samples[None, ...]


def _load_model(directory: Path):
    import onnxruntime  # lazy: ~40 MB of native code the non-voice paths never pay for

    onnxruntime.set_default_logger_severity(3)  # errors only; init chatter isn't ours
    return MoonshineModel(
        onnxruntime.InferenceSession(str(directory / "encoder_model.onnx")),
        onnxruntime.InferenceSession(str(directory / "decoder_model_merged.onnx")),
    )


class MoonshineModel:
    """Vendored inference loop (Useful Sensors, MIT) for the merged-decoder ONNX export.

    Constants are Moonshine *base*: 8 layers, 8 KV heads, head_dim 52. One generate()
    at a time (the run lock): concurrent dictations would fight for the same cores and
    both lose; serialized, each stays sub-second.
    """

    _NUM_LAYERS = 8
    _KV_HEADS = 8
    _HEAD_DIM = 52
    _START_TOKEN = 1
    _EOS_TOKEN = 2

    def __init__(self, encoder, decoder) -> None:
        self.encoder = encoder
        self.decoder = decoder
        self.encoder_input_names = [x.name for x in encoder.get_inputs()]
        self.decoder_input_names = [x.name for x in decoder.get_inputs()]
        self._run_lock = threading.Lock()

    def generate(self, audio: np.ndarray, max_len: int = _MAX_TOKENS) -> list:
        assert audio.ndim == 2, "audio must be [batch, samples]"
        with self._run_lock:
            return self._generate(audio, max_len)

    def _generate(self, audio: np.ndarray, max_len: int) -> list:
        mask = np.ones_like(audio, dtype=np.int64)
        encoder_inputs = {"input_values": audio}
        if "attention_mask" in self.encoder_input_names:
            encoder_inputs["attention_mask"] = mask
        hidden = self.encoder.run(None, encoder_inputs)[0]

        past = {
            f"past_key_values.{i}.{a}.{b}": np.zeros(
                (0, self._KV_HEADS, 1, self._HEAD_DIM), dtype=np.float32)
            for i in range(self._NUM_LAYERS)
            for a in ("decoder", "encoder")
            for b in ("key", "value")
        }
        tokens = [self._START_TOKEN]
        input_ids = [tokens]
        for i in range(max_len):
            use_cache = i > 0
            decoder_inputs = dict(input_ids=input_ids, encoder_hidden_states=hidden,
                                  use_cache_branch=[use_cache], **past)
            if "encoder_attention_mask" in self.decoder_input_names:
                decoder_inputs["encoder_attention_mask"] = mask
            logits, *present = self.decoder.run(None, decoder_inputs)
            next_token = logits[0, -1].argmax().item()
            tokens.append(next_token)
            if next_token == self._EOS_TOKEN:
                break
            input_ids = [[next_token]]
            for key, value in zip(past.keys(), present):
                if not use_cache or "decoder" in key:
                    past[key] = value
        return [tokens]
