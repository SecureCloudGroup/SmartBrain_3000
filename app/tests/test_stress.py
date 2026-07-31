"""Stress: the app under sustained, mixed, concurrent load.

Everything that went wrong on a live install in one day — a stream that hung a browser,
a background job starving chat, a producer thread that could have leaked the single
local-model slot — shares a shape: it only appears when several things happen at once.
The unit tests hold each piece still; this file shakes them together.

Hermetic by construction: a fake gateway (no model, no network) that holds the REAL
local-model semaphore exactly as gateway.chat_stream does, so a missing release is a
failure here rather than a wedged machine later. Every test carries a deadline, so a
deadlock fails in seconds instead of hanging CI.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import agent_routes, gateway

_LOCAL_MODEL = "mlx/stress-model"  # "mlx/" makes gateway treat it as a single-slot local model
_DEADLINE = 60.0  # a whole test; anything slower than this is a hang, not slowness


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "stress.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as c:
        c.post("/api/account/setup", json={"passphrase": "correct-horse"})
        yield c


def _fake_stream(*, chunks: int = 3, delay: float = 0.02):
    """A streaming gateway that holds the local slot across the whole stream — the real
    shape (gateway.chat_stream wraps its iteration in _serialized), which is what makes a
    leaked release detectable."""

    def fake(messages, model, **kw):
        def gen():
            with gateway._serialized(model):
                for i in range(chunks):
                    time.sleep(delay)
                    yield {"delta": f"part{i} ", "tool_calls": None, "finish_reason": None}
                yield {"delta": "", "tool_calls": None, "finish_reason": "stop"}

        return gen()

    return fake


def _await_free_slot(budget: float = 5.0) -> bool:
    """The local-model slot must return to free; poll rather than assume."""
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if gateway.local_available():
            return True
        time.sleep(0.05)
    return gateway.local_available()


def test_mixed_load_stays_healthy(client: TestClient, monkeypatch) -> None:
    """Chats, agent turns, searches and history reads all at once: no 5xx, no hang."""
    monkeypatch.setattr(gateway, "chat_stream", _fake_stream())
    monkeypatch.setattr(gateway, "chat",
                        lambda *a, **k: {"choices": [{"message": {"content": "ok"}}]})
    monkeypatch.setattr(gateway, "chat_with_tools",
                        lambda *a, **k: {"choices": [{"message": {"content": "ok"}}]})

    def a_stream(i: int) -> int:
        r = client.post("/api/agent/turn/stream",
                        json={"messages": [{"role": "user", "content": f"q{i}"}], "model": _LOCAL_MODEL})
        return r.status_code

    def a_chat(i: int) -> int:
        r = client.post("/api/chat",
                        json={"messages": [{"role": "user", "content": f"c{i}"}], "model": _LOCAL_MODEL})
        return r.status_code

    def a_search(i: int) -> int:
        return client.get("/api/kb/search", params={"q": f"term{i}", "mode": "lexical"}).status_code

    def a_read(i: int) -> int:
        return client.get("/api/conversations").status_code

    started = time.monotonic()
    codes: list[int] = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = []
        for i in range(6):  # 24 requests, four kinds, all overlapping
            futures += [pool.submit(a_stream, i), pool.submit(a_chat, i),
                        pool.submit(a_search, i), pool.submit(a_read, i)]
        for f in as_completed(futures, timeout=_DEADLINE):
            codes.append(f.result())

    assert len(codes) == 24
    assert not [c for c in codes if c >= 500], f"server errors under load: {codes}"
    assert time.monotonic() - started < _DEADLINE, "mixed load must not deadlock"
    assert _await_free_slot(), "the local-model slot must be free once the load stops"


def _live_server(tmp_path, monkeypatch):
    """Boot the real app under uvicorn on a random port.

    TestClient cannot express this test: it drains a response the caller walks away from,
    so the server never sees a disconnect. Proving that an abandoned answer releases the
    model needs a real socket that really closes — verified by removing the guard and
    watching this fail.
    """
    import threading

    import uvicorn

    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "live.duckdb"))
    from smartbrain_3000.main import create_app

    config = uvicorn.Config(create_app(), host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:  # wait for the socket to be assigned
        if server.started and server.servers:
            break
        time.sleep(0.05)
    assert server.started and server.servers, "the test server never came up"
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, f"http://127.0.0.1:{port}"


def test_a_severed_stream_releases_the_model_slot(tmp_path, monkeypatch) -> None:
    """A browser that walks away mid-answer must not cost the machine its model.

    The producer runs on its own thread and hands frames to the response through a bounded
    queue. When the reader vanishes that queue fills and the producer BLOCKS — holding the
    single local-model slot. Waiting forever there would strand every later local call, so
    it must give up. The queue and give-up timeout are shrunk so the pile-up takes
    milliseconds instead of a real 256-frame backlog.
    """
    import httpx

    monkeypatch.setattr(agent_routes, "_SSE_QUEUE_FRAMES", 2)
    monkeypatch.setattr(agent_routes, "_SSE_PRODUCER_PUT_TIMEOUT", 1.0)
    monkeypatch.setattr(gateway, "chat_stream", _fake_stream(chunks=500, delay=0.001))

    server, base = _live_server(tmp_path, monkeypatch)
    try:
        with httpx.Client(base_url=base, timeout=20.0) as setup:
            assert setup.post("/api/account/setup", json={"passphrase": "correct-horse"}).status_code == 200
        for _ in range(2):  # connect, take one frame, hang up hard
            c = httpx.Client(base_url=base, timeout=20.0)
            with c.stream("POST", "/api/agent/turn/stream",
                          json={"messages": [{"role": "user", "content": "hi"}],
                                "model": _LOCAL_MODEL}) as r:
                assert r.status_code == 200
                for _line in r.iter_lines():
                    break
            c.close()  # the socket really closes: the server sees a disconnect
        assert _await_free_slot(8.0), "an abandoned stream must not strand the local-model slot"
    finally:
        server.should_exit = True


def test_repeated_tool_turns_do_not_grow_the_primed_store(client: TestClient, monkeypatch) -> None:
    """The parked-response cache is a cache, not a leak: bounded no matter the traffic."""

    def tool_stream(messages, model, **kw):
        def gen():
            with gateway._serialized(model):
                yield {"delta": "", "tool_calls": [{"index": 0, "id": "c1",
                       "function": {"name": "list_tasks", "arguments": "{}"}}],
                       "finish_reason": "tool_calls"}

        return gen()

    monkeypatch.setattr(gateway, "chat_stream", tool_stream)
    for i in range(agent_routes._MAX_PRIMED * 3):  # far more turns than the cache may hold
        r = client.post("/api/agent/turn/stream",
                        json={"messages": [{"role": "user", "content": f"tasks? {i}"}],
                              "model": _LOCAL_MODEL})
        assert r.status_code == 200
    assert len(agent_routes._primed) <= agent_routes._MAX_PRIMED, "the parked-response cache must stay bounded"
    assert _await_free_slot(), "tool turns must leave the model slot free too"


def test_locking_mid_load_is_refused_cleanly(client: TestClient, monkeypatch) -> None:
    """Locking the vault while requests are in flight: gated endpoints say 423, never 500."""
    monkeypatch.setattr(gateway, "chat_stream", _fake_stream(chunks=6, delay=0.03))
    monkeypatch.setattr(gateway, "chat",
                        lambda *a, **k: {"choices": [{"message": {"content": "ok"}}]})

    codes: list[int] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(lambda i=i: client.post(
            "/api/chat", json={"messages": [{"role": "user", "content": f"q{i}"}],
                               "model": _LOCAL_MODEL}).status_code) for i in range(5)]
        time.sleep(0.05)
        futures.append(pool.submit(lambda: client.post("/api/account/lock").status_code))
        for f in as_completed(futures, timeout=_DEADLINE):
            codes.append(f.result())

    assert not [c for c in codes if c >= 500], f"a mid-flight lock must never 500: {codes}"
    assert client.get("/api/conversations").status_code == 423, "locked means locked"
    assert _await_free_slot(), "a lock during load must not strand the model slot"
