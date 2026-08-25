"""Voice: config fallback, the transcribe/speak proxies, and the HTTP surface."""

from __future__ import annotations

from collections.abc import Iterator

import duckdb
import httpx
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import gateway, voice
from smartbrain_3000.secrets import SecretStore, gen_master_key

_LOCAL = {"X-SB-Local": "1"}


@pytest.fixture(autouse=True)
def _fresh_server_skip():
    """The server-failure skip window is module state; without this, one test's
    fallback poisons every later test into silently using the local engine
    (adversarial-review finding — the auto-resolve test failed deterministically)."""
    voice.reset_server_skip()
    yield
    voice.reset_server_skip()


def _secret_store() -> SecretStore:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return SecretStore(conn, gen_master_key())


# --- config resolution -----------------------------------------------------

def test_server_config_prefers_voice_then_mlx() -> None:
    store = _secret_store()
    assert voice.server_config(store)["url"] == ""  # nothing configured
    store.put(gateway.MLX_URL_KEY, "http://127.0.0.1:8888")
    cfg = voice.server_config(store)
    assert cfg["source"] == "mlx" and "8888" in cfg["url"]
    store.put(gateway.VOICE_URL_KEY, "http://127.0.0.1:9999")
    cfg = voice.server_config(store)
    assert cfg["source"] == "voice" and "9999" in cfg["url"]


def test_stt_model_defaults() -> None:
    store = _secret_store()
    assert voice.stt_model(store) == voice.DEFAULT_STT_MODEL
    store.put(voice.STT_MODEL_KEY, "faster-whisper-small")
    assert voice.stt_model(store) == "faster-whisper-small"


# --- proxies (httpx.post monkeypatched) ------------------------------------

def _resp(status: int, json_body=None, content: bytes = b"", headers=None) -> httpx.Response:
    if json_body is not None:
        return httpx.Response(status, json=json_body, headers=headers or {})
    return httpx.Response(status, content=content, headers=headers or {})


def test_transcribe_happy_path(monkeypatch) -> None:
    store = _secret_store()
    store.put(gateway.MLX_URL_KEY, "http://127.0.0.1:8888")
    seen: dict = {}

    def fake_post(url, **kw):
        seen["url"] = url
        seen["model"] = kw["data"]["model"]
        seen["file"] = kw["files"]["file"]
        return _resp(200, {"text": "  add milk to my tasks "})

    monkeypatch.setattr(voice.httpx, "post", fake_post)
    text = voice.transcribe(store, b"RIFFxxxxWAVE")
    assert text == "add milk to my tasks"  # trimmed
    assert seen["url"].endswith("/v1/audio/transcriptions")
    assert seen["model"] == voice.DEFAULT_STT_MODEL


def test_transcribe_unconfigured_uses_local_engine(monkeypatch) -> None:
    store = _secret_store()
    monkeypatch.setattr(voice.stt_local, "transcribe_wav", lambda audio: "local text")
    assert voice.transcribe(store, b"RIFF") == "local text"


def test_transcribe_local_failure_is_actionable(monkeypatch) -> None:
    store = _secret_store()

    def not_ready(audio):
        raise RuntimeError("preparing voice (40%) — one-time download, try again shortly")
    monkeypatch.setattr(voice.stt_local, "transcribe_wav", not_ready)
    with pytest.raises(voice.VoiceError) as e:
        voice.transcribe(store, b"RIFF")
    assert e.value.status == 503 and "preparing voice (40%)" in e.value.message


def test_transcribe_server_failure_falls_back_to_local(monkeypatch) -> None:
    """A configured-but-broken server must not take dictation down: local carries on."""
    store = _secret_store()
    store.put(gateway.MLX_URL_KEY, "http://127.0.0.1:8888")
    monkeypatch.setattr(voice.stt_local, "transcribe_wav", lambda audio: "local rescue")

    monkeypatch.setattr(voice.httpx, "post",
                        lambda url, **kw: _resp(500, {"error": {"message": "model exploded"}}))
    assert voice.transcribe(store, b"RIFF") == "local rescue"

    def timeout(url, **kw):
        raise httpx.ConnectTimeout("slow")
    monkeypatch.setattr(voice.httpx, "post", timeout)
    assert voice.transcribe(store, b"RIFF") == "local rescue"

    with pytest.raises(voice.VoiceError) as e:  # size guard fires before any engine
        voice.transcribe(store, b"x" * (voice._MAX_AUDIO_BYTES + 1))
    assert e.value.status == 413


def test_transcribe_autoresolves_whisper_model(monkeypatch) -> None:
    """A 'not found' answer triggers catalog lookup, a retry with the server's own
    whisper model, and a persisted name — the field-test failure, self-healed."""
    store = _secret_store()
    store.put(gateway.MLX_URL_KEY, "http://127.0.0.1:8888")
    calls: list = []

    def fake_post(url, **kw):
        calls.append(kw["data"]["model"])
        if kw["data"]["model"] == voice.DEFAULT_STT_MODEL:
            return _resp(404, {"detail": "Model 'whisper-large-v3-turbo' not found. Available: Qwen, whisper-turbo-mlx"})
        return _resp(200, {"text": "resolved fine"})

    monkeypatch.setattr(voice.httpx, "post", fake_post)
    monkeypatch.setattr(voice.gateway, "probe_mlx",
                        lambda url, key, **kw: {"reachable": True, "models": ["Qwen", "whisper-turbo-mlx"]})
    assert voice.transcribe(store, b"RIFF") == "resolved fine"
    assert calls == [voice.DEFAULT_STT_MODEL, "whisper-turbo-mlx"]
    assert store.get(voice.STT_MODEL_KEY) == "whisper-turbo-mlx"  # sticky
    calls.clear()
    assert voice.transcribe(store, b"RIFF") == "resolved fine"
    assert calls == ["whisper-turbo-mlx"]  # no second resolution round-trip


def test_transcribe_no_server_whisper_falls_back_to_local(monkeypatch) -> None:
    store = _secret_store()
    store.put(gateway.MLX_URL_KEY, "http://127.0.0.1:8888")
    monkeypatch.setattr(voice.httpx, "post",
                        lambda url, **kw: _resp(404, {"detail": "Model 'x' not found. Available: Qwen"}))
    monkeypatch.setattr(voice.gateway, "probe_mlx",
                        lambda url, key, **kw: {"reachable": True, "models": ["Qwen"]})
    monkeypatch.setattr(voice.stt_local, "transcribe_wav", lambda audio: "moonshine says hi")
    assert voice.transcribe(store, b"RIFF") == "moonshine says hi"


def test_status_reports_local_engine() -> None:
    store = _secret_store()
    st = voice.status(store)
    assert st["stt_available"] is True
    assert st["local"]["phase"] in ("absent", "downloading", "loading", "ready", "error")
    assert st["engine"] in ("server", "local")


def test_server_failure_is_skipped_for_a_while(monkeypatch) -> None:
    """After a server failure, the next presses go STRAIGHT to local — no re-paying
    the server's round-trips on every dictation (field: latency + log spam)."""
    store = _secret_store()
    store.put(gateway.MLX_URL_KEY, "http://127.0.0.1:8888")
    monkeypatch.setattr(voice, "_server_skip_until", 0.0)
    calls = []

    def failing_post(url, **kw):
        calls.append(url)
        raise httpx.ConnectTimeout("down")
    monkeypatch.setattr(voice.httpx, "post", failing_post)
    monkeypatch.setattr(voice.stt_local, "transcribe_wav", lambda audio: "local")
    assert voice.transcribe(store, b"RIFF") == "local"
    assert len(calls) == 1
    assert voice.transcribe(store, b"RIFF") == "local"
    assert len(calls) == 1  # skip window: server not retried
    assert voice._current_engine(store) == "local"
    monkeypatch.setattr(voice, "_server_skip_until", 0.0)  # window expired
    assert voice._current_engine(store) == "server"


def test_status_probe_reports_stt_ready(monkeypatch) -> None:
    store = _secret_store()
    store.put(gateway.MLX_URL_KEY, "http://127.0.0.1:8888")
    monkeypatch.setattr(voice.gateway, "probe_mlx",
                        lambda url, key, **kw: {"reachable": True, "models": ["Qwen"]})
    st = voice.status(store, probe=True)
    assert st["reachable"] is True and st["stt_ready"] is False
    monkeypatch.setattr(voice.gateway, "probe_mlx",
                        lambda url, key, **kw: {"reachable": True, "models": ["Qwen", "any-whisper-build"]})
    assert voice.status(store, probe=True)["stt_ready"] is True
    assert voice.status(store)["stt_ready"] is None  # unprobed stays unknown


def test_speak_requires_tts_model(monkeypatch) -> None:
    store = _secret_store()
    store.put(gateway.MLX_URL_KEY, "http://127.0.0.1:8888")
    with pytest.raises(voice.VoiceError) as e:
        voice.speak(store, "hello")  # no tts model set -> server TTS is off
    assert e.value.status == 503

    store.put(voice.TTS_MODEL_KEY, "kokoro")
    store.put(voice.TTS_VOICE_KEY, "af_heart")
    seen: dict = {}

    def fake_post(url, **kw):
        seen["url"] = url
        seen["payload"] = kw["json"]
        return _resp(200, content=b"AUDIO", headers={"content-type": "audio/mpeg"})

    monkeypatch.setattr(voice.httpx, "post", fake_post)
    audio, ctype = voice.speak(store, "hello")
    assert audio == b"AUDIO" and ctype == "audio/mpeg"
    assert seen["url"].endswith("/v1/audio/speech")
    assert seen["payload"] == {"model": "kokoro", "input": "hello", "voice": "af_heart"}


# --- routes ----------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "t.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as c:
        c.post("/api/account/setup", json={"passphrase": "voice-test-pass"})
        yield c


def test_voice_routes_locked(client: TestClient) -> None:
    client.post("/api/account/lock")
    assert client.get("/api/voice/status").status_code == 423
    assert client.post("/api/voice/transcribe", content=b"x").status_code == 423
    assert client.post("/api/voice/speak", json={"text": "hi"}).status_code == 423


def test_voice_status_and_settings_roundtrip(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(voice.gateway, "probe_mlx", lambda url, key, **kw: {"reachable": True, "models": []})
    s = client.get("/api/voice/status").json()
    assert s["configured"] is False and s["stt_model"] == voice.DEFAULT_STT_MODEL
    assert s["reachable"] is None  # unprobed by default — the chat mount never pays a live probe

    r = client.put("/api/local-models/voice", json={
        "url": "http://127.0.0.1:9999", "api_key": "k",
        "stt_model": "whisper-small", "tts_model": "kokoro"}, headers=_LOCAL)
    assert r.status_code == 200
    st = r.json()["status"]
    assert st["configured"] and st["source"] == "voice" and st["reachable"] is True
    assert st["stt_model"] == "whisper-small" and st["tts_model"] == "kokoro"

    assert client.delete("/api/local-models/voice", headers=_LOCAL).status_code == 200
    assert client.get("/api/voice/status").json()["configured"] is False


def test_transcribe_route(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(voice, "transcribe", lambda store, audio, ctype="audio/wav": "hello world")
    import smartbrain_3000.voice_routes as vr
    monkeypatch.setattr(vr.voice, "transcribe", lambda store, audio, ctype="audio/wav": "hello world")
    r = client.post("/api/voice/transcribe", content=b"RIFF....WAVE",
                    headers={"content-type": "audio/wav"})
    assert r.status_code == 200 and r.json() == {"text": "hello world"}
    assert client.post("/api/voice/transcribe", content=b"").status_code == 422


def test_transcribe_route_surfaces_voice_errors(client: TestClient, monkeypatch) -> None:
    import smartbrain_3000.voice_routes as vr

    def boom(store, audio, ctype="audio/wav"):
        raise voice.VoiceError(503, "no voice server configured — add one under Settings → Local models")
    monkeypatch.setattr(vr.voice, "transcribe", boom)
    r = client.post("/api/voice/transcribe", content=b"x")
    assert r.status_code == 503 and "no voice server" in r.json()["detail"]


def test_speak_route(client: TestClient, monkeypatch) -> None:
    import smartbrain_3000.voice_routes as vr
    monkeypatch.setattr(vr.voice, "speak", lambda store, text: (b"AUDIO", "audio/mpeg"))
    r = client.post("/api/voice/speak", json={"text": "hi there"})
    assert r.status_code == 200 and r.content == b"AUDIO"
    assert r.headers["content-type"].startswith("audio/mpeg")
