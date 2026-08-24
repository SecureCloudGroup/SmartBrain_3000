"""Moonshine local STT: WAV decode, readiness phases, and the guarded fetch."""

from __future__ import annotations

import io
import math
import struct
import wave

import pytest

from smartbrain_3000 import moonshine


def _wav(rate: int = 16000, seconds: float = 0.5, channels: int = 1, width: int = 2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        n = int(rate * seconds)
        w.writeframes(b"".join(struct.pack("<h", int(1000 * math.sin(i / 10))) for i in range(n)))
    return buf.getvalue()


def test_decode_wav_shape_and_range() -> None:
    audio = moonshine._decode_wav(_wav(seconds=1.0))
    assert audio.shape == (1, 16000)
    assert float(abs(audio).max()) <= 1.0


def test_decode_wav_resamples_stray_rates() -> None:
    audio = moonshine._decode_wav(_wav(rate=48000, seconds=1.0))
    assert audio.shape == (1, 16000)  # decimated to the model's rate


def test_decode_wav_refuses_garbage_and_wrong_formats() -> None:
    with pytest.raises(RuntimeError):
        moonshine._decode_wav(b"not a wav at all")
    with pytest.raises(RuntimeError):
        moonshine._decode_wav(_wav(channels=2))


def test_decode_wav_pads_sub_100ms_clips() -> None:
    audio = moonshine._decode_wav(_wav(seconds=0.01))
    assert audio.shape[1] >= 1600  # Moonshine's floor


def test_transcribe_reports_download_progress(monkeypatch) -> None:
    monkeypatch.setattr(moonshine, "_phase", "downloading")
    monkeypatch.setattr(moonshine, "_pct", 37)
    monkeypatch.setattr(moonshine, "_model", None)
    with pytest.raises(RuntimeError) as e:
        moonshine.transcribe_wav(_wav())
    assert "37%" in str(e.value)


def test_transcribe_surfaces_fetch_errors(monkeypatch) -> None:
    monkeypatch.setattr(moonshine, "_phase", "error")
    monkeypatch.setattr(moonshine, "_error", "model download failed verification (encoder_model.onnx)")
    monkeypatch.setattr(moonshine, "_model", None)
    with pytest.raises(RuntimeError) as e:
        moonshine.transcribe_wav(_wav())
    assert "verification" in str(e.value)


def test_prefetch_is_idempotent(monkeypatch) -> None:
    started = []
    monkeypatch.setattr(moonshine, "_prefetch_started", False)

    class FakeThread:
        def __init__(self, *a, **kw):
            started.append(1)
        def start(self):
            pass

    monkeypatch.setattr(moonshine.threading, "Thread", FakeThread)
    moonshine.prefetch()
    moonshine.prefetch()
    assert len(started) == 1  # second call is a no-op
