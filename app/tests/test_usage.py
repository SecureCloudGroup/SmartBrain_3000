"""Tests for token-usage recording + the cost view (/api/usage)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import db, gateway, usage


def _conn(tmp_path):
    conn = db.open_db(tmp_path / "u.duckdb")
    db.run_migrations(conn)  # creates usage_log
    return conn


def test_record_and_summary(tmp_path) -> None:
    conn = _conn(tmp_path)
    usage.record(conn, "gemini/gemini-2.5-flash", 100, 50)
    usage.record(conn, "gemini/gemini-2.5-flash", 200, 25)
    usage.record(conn, "ollama/llama3.2", 10, 5)
    s = {r["model"]: r for r in usage.summary(conn)}
    assert s["gemini/gemini-2.5-flash"]["calls"] == 2
    assert s["gemini/gemini-2.5-flash"]["prompt_tokens"] == 300
    assert s["gemini/gemini-2.5-flash"]["completion_tokens"] == 75
    assert s["ollama/llama3.2"]["calls"] == 1


def test_record_response_handles_missing_usage(tmp_path) -> None:
    conn = _conn(tmp_path)
    usage.record_response(conn, "gemini/x", {"choices": []})  # no usage block -> ignored
    assert usage.summary(conn) == []
    usage.record_response(conn, "gemini/x", {"usage": {"prompt_tokens": 7, "completion_tokens": 3}})
    assert usage.summary(conn)[0]["prompt_tokens"] == 7


def test_summary_filters_by_time_window(tmp_path) -> None:
    import uuid

    conn = _conn(tmp_path)
    ins = "INSERT INTO usage_log (id, created_at, model, prompt_tokens, completion_tokens) VALUES (?, ?, ?, ?, ?);"
    conn.execute(ins, [uuid.uuid4().hex, "2026-06-01 10:00:00", "gemini/x", 100, 50])
    conn.execute(ins, [uuid.uuid4().hex, "2026-06-02 23:59:59.500", "gemini/x", 200, 25])  # sub-second, late in day
    assert usage.summary(conn, since="2026-06-02 00:00:00")[0]["prompt_tokens"] == 200  # later row only (inclusive)
    # Exclusive upper bound at the NEXT midnight still includes the 23:59:59.5 row (no sub-second drop).
    both = usage.summary(conn, since="2026-06-02 00:00:00", until="2026-06-03 00:00:00")
    assert len(both) == 1 and both[0]["completion_tokens"] == 25
    # until at day-2 midnight (exclusive) excludes all of day 2 -> only the day-1 row.
    assert usage.summary(conn, until="2026-06-02 00:00:00")[0]["prompt_tokens"] == 100


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def test_usage_endpoint_requires_unlock(client: TestClient) -> None:
    assert client.get("/api/usage").status_code == 423


def test_usage_endpoint_computes_cloud_cost(client: TestClient, monkeypatch) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    monkeypatch.setattr(gateway, "chat", lambda messages, model: {
        "choices": [{"message": {"role": "assistant", "content": "hi"}}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
    })
    r = client.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}], "model": "gemini/gemini-2.5-flash"})
    assert r.status_code == 200  # the chat records a usage row
    monkeypatch.setattr(gateway, "list_models", lambda: [
        {"id": "gemini/gemini-2.5-flash", "pricing": {"prompt": 3e-7, "completion": 2.5e-6}},
    ])
    u = client.get("/api/usage").json()
    row = next(x for x in u["usage"] if x["model"] == "gemini/gemini-2.5-flash")
    assert row["calls"] == 1 and row["local"] is False
    expected = 1000 * 3e-7 + 500 * 2.5e-6
    assert abs(row["cost"] - expected) < 1e-12 and abs(u["total_cost"] - expected) < 1e-12


def test_usage_endpoint_local_is_free(client: TestClient, monkeypatch) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    monkeypatch.setattr(gateway, "chat", lambda messages, model: {
        "choices": [{"message": {"content": "hi"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    })
    client.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}], "model": "ollama/llama3.2"})
    monkeypatch.setattr(gateway, "list_models", lambda: [])  # no pricing available
    u = client.get("/api/usage").json()
    row = next(x for x in u["usage"] if x["model"] == "ollama/llama3.2")
    assert row["local"] is True and row["cost"] == 0.0


def test_usage_endpoint_time_bounds(client: TestClient, monkeypatch) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    monkeypatch.setattr(gateway, "chat", lambda messages, model: {
        "choices": [{"message": {"content": "hi"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    })
    client.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}], "model": "gemini/x"})
    monkeypatch.setattr(gateway, "list_models", lambda: [])
    # A future 'since' excludes the just-recorded row.
    assert client.get("/api/usage", params={"since": "2099-01-01 00:00:00"}).json()["usage"] == []
    # A malformed bound is ignored (the row is still counted).
    assert len(client.get("/api/usage", params={"since": "not-a-date"}).json()["usage"]) == 1
