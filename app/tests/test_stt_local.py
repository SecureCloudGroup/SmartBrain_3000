"""Local Whisper STT: WAV decode, readiness phases, and the guarded fetch."""

from __future__ import annotations

import io
import math
import struct
import wave

import pytest

from smartbrain_3000 import stt_local as moonshine


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
    assert audio.shape[1] >= 1600  # the decoder's floor


def test_transcribe_reports_download_progress(monkeypatch) -> None:
    monkeypatch.setattr(moonshine, "_phase", "downloading")
    monkeypatch.setattr(moonshine, "_pct", 37)
    monkeypatch.setattr(moonshine, "_model", None)
    with pytest.raises(RuntimeError) as e:
        moonshine.transcribe_wav(_wav())
    assert "37%" in str(e.value)


def test_transcribe_never_blocks_when_absent(monkeypatch) -> None:
    """The field's five-minute hang: transcribe used to download 236 MB inline. Now an
    absent model kicks the background fetch and raises IMMEDIATELY with the phase."""
    kicked = []
    monkeypatch.setattr(moonshine, "_phase", "absent")
    monkeypatch.setattr(moonshine, "_pct", 0)
    monkeypatch.setattr(moonshine, "_model", None)
    monkeypatch.setattr(moonshine, "prefetch", lambda app=None: kicked.append(1))
    with pytest.raises(RuntimeError) as e:
        moonshine.transcribe_wav(_wav())
    assert "preparing voice" in str(e.value)
    assert kicked == [1]  # self-healing: the press armed the fetch


def test_transcribe_loading_phase_is_not_a_percent_lie(monkeypatch) -> None:
    """Engine init after the download has its own words — '(100%)' while visibly not
    working was the field's exact complaint."""
    monkeypatch.setattr(moonshine, "_phase", "loading")
    monkeypatch.setattr(moonshine, "_model", None)
    with pytest.raises(RuntimeError) as e:
        moonshine.transcribe_wav(_wav())
    msg = str(e.value)
    assert "almost ready" in msg and "%" not in msg


def test_transcribe_error_rearms_the_fetch(monkeypatch) -> None:
    kicked = []
    monkeypatch.setattr(moonshine, "_phase", "error")
    monkeypatch.setattr(moonshine, "_error", "model download failed verification (encoder_model.onnx)")
    monkeypatch.setattr(moonshine, "_model", None)
    monkeypatch.setattr(moonshine, "prefetch", lambda app=None: kicked.append(1))
    with pytest.raises(RuntimeError) as e:
        moonshine.transcribe_wav(_wav())
    assert "verification" in str(e.value) and "retrying" in str(e.value)
    assert kicked == [1]


def test_prefetch_single_flight_but_rearms_after_error(monkeypatch) -> None:
    started = []
    monkeypatch.delenv("SMARTBRAIN_NO_VOICE_PREFETCH", raising=False)  # conftest sets it suite-wide
    monkeypatch.setattr(moonshine, "_fetch_inflight", False)
    monkeypatch.setattr(moonshine, "_phase", "absent")

    class FakeThread:
        def __init__(self, *a, **kw):
            started.append(1)
        def start(self):
            pass

    monkeypatch.setattr(moonshine.threading, "Thread", FakeThread)
    moonshine.prefetch()
    assert len(started) == 1
    # Arm-time transition: a status read racing the thread start must see motion,
    # or the chat retry button wedges on a stale "error".
    assert moonshine._phase == "downloading"
    moonshine.prefetch()  # in flight -> no-op
    assert len(started) == 1
    monkeypatch.setattr(moonshine, "_fetch_inflight", False)
    monkeypatch.setattr(moonshine, "_phase", "error")
    moonshine.prefetch()  # error state re-arms (transient network failures heal)
    assert len(started) == 2
    monkeypatch.setattr(moonshine, "_fetch_inflight", False)
    monkeypatch.setattr(moonshine, "_phase", "ready")
    moonshine.prefetch()  # ready never refetches
    assert len(started) == 2


def test_prefetch_env_optout(monkeypatch) -> None:
    monkeypatch.setenv("SMARTBRAIN_NO_VOICE_PREFETCH", "1")
    monkeypatch.setattr(moonshine, "_fetch_inflight", False)
    monkeypatch.setattr(moonshine, "_phase", "absent")
    started = []

    class FakeThread:
        def __init__(self, *a, **kw):
            started.append(1)
        def start(self):
            pass

    monkeypatch.setattr(moonshine.threading, "Thread", FakeThread)
    moonshine.prefetch()
    assert started == []  # hermetic tests / network-forbidden deploys stay quiet


def test_pct_is_byte_accurate_and_reserves_100_for_ready(monkeypatch) -> None:
    monkeypatch.setattr(moonshine, "_pct", 0)
    moonshine._set_pct(moonshine._TOTAL_BYTES)  # every byte down, engine not loaded yet
    assert moonshine._pct == 99  # 100 means READY, nothing else — the field saw '(100%)' lie


def test_partial_decodes_greedily_final_keeps_the_beam(monkeypatch) -> None:
    """Live snapshots trade beam width for speed; the final pass does not."""
    beams = []

    class _Model:
        def transcribe(self, audio, **kw):
            beams.append(kw["beam_size"])
            return iter(()), None

    monkeypatch.setattr(moonshine, "_phase", "ready")
    monkeypatch.setattr(moonshine, "_model", _Model())
    moonshine.transcribe_wav(_wav(), partial=True)
    moonshine.transcribe_wav(_wav())
    assert beams == [1, 5]
