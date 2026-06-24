"""HTTP tests for the schedules API: lock gate, CRUD, run-now, and run history.

The route layer was previously untested; these exercise the real FastAPI app (with a
real DuckDB + migrations) so the 423-when-locked, run-now, and the new run-history
endpoint are validated end to end. Only the LLM turn is faked.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import agent


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "sched.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def _unlock(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})


def _add(client: TestClient, **over) -> str:
    body = {"title": "Brief", "prompt": "do it", "interval_minutes": 1440, "start_in_minutes": 0, "model": "m"}
    body.update(over)
    return client.post("/api/schedules", json=body).json()["id"]


def test_routes_require_unlock(client: TestClient) -> None:
    assert client.get("/api/schedules").status_code == 423
    assert client.post("/api/schedules/x/run").status_code == 423
    assert client.get("/api/schedules/x/runs").status_code == 423


def test_add_list_and_model_passthrough(client: TestClient) -> None:
    _unlock(client)
    sid = _add(client, model="ollama/llama3.1")
    schedules = client.get("/api/schedules").json()["schedules"]
    assert schedules[0]["id"] == sid and schedules[0]["model"] == "ollama/llama3.1"  # decrypted back


def test_run_now_persists_a_readable_result(client: TestClient, monkeypatch) -> None:
    _unlock(client)
    sid = _add(client)
    monkeypatch.setattr(agent, "run_turn", lambda *a, **k: {"status": "complete", "message": "the briefing"})
    assert client.post(f"/api/schedules/{sid}/run").json()["status"] == "complete"
    runs = client.get(f"/api/schedules/{sid}/runs").json()["runs"]
    assert runs[0]["status"] == "complete" and runs[0]["message"] == "the briefing"


def test_run_now_records_failure(client: TestClient, monkeypatch) -> None:
    _unlock(client)
    sid = _add(client)

    def boom(*_a, **_k):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(agent, "run_turn", boom)
    assert client.post(f"/api/schedules/{sid}/run").json()["status"] == "error"
    runs = client.get(f"/api/schedules/{sid}/runs").json()["runs"]
    assert runs[0]["status"] == "error" and "gateway down" in (runs[0]["error"] or "")


def test_runs_404_for_unknown_schedule(client: TestClient) -> None:
    _unlock(client)
    assert client.get("/api/schedules/nope/runs").status_code == 404
