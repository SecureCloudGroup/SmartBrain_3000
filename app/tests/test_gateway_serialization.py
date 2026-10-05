"""Local-model call serialization + interactive timeout (the oMLX 'model is busy' fix).

A local model server (Ollama / MLX / oMLX) serves ONE request at a time and errors on any
overlap ("model is busy; cannot reload runtime settings variant until active requests finish").
The gateway must serialize every local-provider call so the app never sends it overlapping
requests (foreground chat + background reindex embed + a retry). Cloud providers stay parallel.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import agent, agent_routes, gateway


class _FakeResp:
    status_code = 200

    def __init__(self, data: dict) -> None:
        self._data = data

    def json(self) -> dict:
        return self._data


class _ConcurrencyClient:
    """Records the peak number of overlapping .post() calls."""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def post(self, *_a, **_k) -> _FakeResp:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.05)  # widen the overlap window so a missing lock is visible
        with self._lock:
            self.active -= 1
        return _FakeResp({"choices": [{"message": {"content": "ok"}}]})


def _hammer(model: str, client: _ConcurrencyClient, n: int = 6) -> int:
    threads = [
        threading.Thread(target=lambda: gateway.chat([{"role": "user", "content": "hi"}], model, client=client))
        for _ in range(n)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return client.max_active


def test_is_local_model_detection() -> None:
    assert gateway._is_local("mlx/gemma-4-26B") and gateway._is_local("ollama/llama3.1")
    assert not gateway._is_local("openai/gpt-4o-mini") and not gateway._is_local("anthropic/claude")
    assert not gateway._is_local("")


def test_local_model_calls_are_serialized() -> None:
    # The heart of the fix: concurrent calls to a LOCAL model never overlap their HTTP posts.
    assert _hammer("mlx/gemma-4-26B", _ConcurrencyClient()) == 1


def test_cloud_model_calls_run_in_parallel() -> None:
    # Cloud providers parallelize fine and must NOT be needlessly serialized.
    assert _hammer("openai/gpt-4o-mini", _ConcurrencyClient()) > 1


def test_local_semaphore_release_is_not_thread_bound() -> None:
    # The critical property: the streaming path acquires the local semaphore on one Starlette
    # threadpool worker and releases it on ANOTHER (the sync SSE generator is driven across workers
    # with no thread affinity). A Lock/RLock raises RuntimeError on a cross-thread release and would
    # wedge the lock forever; a Semaphore must allow acquire-on-A / release-on-B.
    gateway._LOCAL_SEM.acquire()  # "worker A" == this thread
    err: dict = {}

    def release_on_worker_b() -> None:
        try:
            gateway._LOCAL_SEM.release()
        except Exception as exc:  # a threading.Lock/RLock would raise here
            err["exc"] = exc

    t = threading.Thread(target=release_on_worker_b)
    t.start()
    t.join()
    assert "exc" not in err, f"cross-thread release must not raise: {err.get('exc')!r}"
    assert gateway.local_available() is True  # released cleanly on the foreign thread -> free


def test_local_available_reflects_an_in_flight_call() -> None:
    # local_available() is False while a local-model call holds the semaphore (so a background
    # backfill skips), True when free. Held from ANOTHER thread — a peek must see cross-thread state.
    assert gateway.local_available() is True
    holding = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with gateway._serialized("mlx/gemma-4-26B"):
            holding.set()
            release.wait(3.0)

    t = threading.Thread(target=hold)
    t.start()
    assert holding.wait(3.0)
    assert gateway.local_available() is False  # a local call is in flight
    release.set()
    t.join()
    assert gateway.local_available() is True  # free again


def test_local_waiters_counts_pending_acquires() -> None:
    """L5: the Library's build polls ``local_waiters`` between texts so a foreground chat/embed doesn't
    keep losing the race on a non-FIFO semaphore. The counter rises for every ``_serialized`` acquire
    that is still waiting and clears when it gets served."""
    assert gateway.local_waiters() == 0
    holding = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with gateway._serialized("mlx/foo"):
            holding.set()
            release.wait(3.0)
    held = threading.Thread(target=hold); held.start()
    assert holding.wait(3.0)
    # the semaphore is held; two more callers queue up and show as waiters
    waiting_in = threading.Event()
    done = threading.Event()

    def waiter() -> None:
        waiting_in.set()
        with gateway._serialized("mlx/foo"):
            pass
        done.set()
    w1 = threading.Thread(target=waiter); w2 = threading.Thread(target=waiter)
    w1.start(); w2.start()
    for _ in range(50):  # bounded poll until both are blocked on acquire
        if gateway.local_waiters() >= 2:
            break
        time.sleep(0.01)
    assert gateway.local_waiters() == 2
    release.set(); held.join(); w1.join(); w2.join()
    assert gateway.local_waiters() == 0


def test_serialized_acquire_timeout_fails_fast_with_local_busy() -> None:
    """L6: a short ``acquire_timeout`` lets the Library's request-path ask embed fall back to the keyword
    ranking (busy is not a failure) instead of blocking behind a foreground chat stream. The call raises
    ``LocalBusy`` within the budget, so the pooled httpx client is never POSTed to."""
    import pytest
    holding = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with gateway._serialized("mlx/foo"):
            holding.set()
            release.wait(3.0)
    t = threading.Thread(target=hold); t.start()
    try:
        assert holding.wait(3.0)
        t0 = time.monotonic()
        with pytest.raises(gateway.LocalBusy), gateway._serialized("mlx/foo", acquire_timeout=0.05):
            pass
        waited = time.monotonic() - t0
        assert waited < 0.75, waited  # bounded by the acquire_timeout, not the stream
    finally:
        release.set(); t.join()


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "s.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        test_client.post("/api/account/setup", json={"passphrase": "correct-horse"})
        yield test_client


def test_interactive_turn_uses_a_generous_timeout(client: TestClient, monkeypatch) -> None:
    # A big local model (gemma-4 26B) needs far more than the old 60s; abandoning it early made a
    # retry collide with the still-running request. The interactive route now passes a longer budget.
    captured: dict = {}
    monkeypatch.setattr(agent, "run_turn", lambda *_a, **k: captured.update(timeout=k.get("timeout")) or {"status": "complete", "message": "hi"})
    r = client.post("/api/agent/turn", json={"messages": [{"role": "user", "content": "hi"}], "model": "mlx/gemma-4-26B"})
    assert r.status_code == 200
    assert captured["timeout"] == agent_routes._INTERACTIVE_TIMEOUT
    assert agent_routes._INTERACTIVE_TIMEOUT >= 120  # comfortably covers a cold big-model load
