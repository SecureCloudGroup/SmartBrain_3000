"""Tests for remembered tool consent (consent.py + the agent/approve wiring)."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import consent, db, gateway


def _conn(tmp_path):
    conn = db.open_db(tmp_path / "c.duckdb")
    db.run_migrations(conn)  # creates meta
    return conn


def test_remember_only_reviewed(tmp_path) -> None:
    conn = _conn(tmp_path)
    assert consent.remember(conn, "remember_fact") is True   # REVIEWED write -> remembered
    assert consent.remember(conn, "delete_task") is False     # IRREVERSIBLE -> refused
    assert consent.remember(conn, "no_such_tool") is False    # unknown -> refused
    assert consent.remembered(conn) == {"remember_fact"}


def test_forget(tmp_path) -> None:
    conn = _conn(tmp_path)
    consent.remember(conn, "remember_fact")
    consent.forget(conn, "remember_fact")
    assert consent.remembered(conn) == set()


def test_remembered_corrupt_config_is_empty(tmp_path) -> None:
    conn = _conn(tmp_path)
    db.meta_set(conn, "remembered_tools", "{not valid json")
    assert consent.remembered(conn) == set()  # safest: remember nothing -> re-ask


def test_remembered_tier_filters_poisoned_entries(tmp_path) -> None:
    # Defense-in-depth: even if an IRREVERSIBLE/unknown name is written straight to
    # the meta row, the read path must drop it (only REVIEWED survives).
    conn = _conn(tmp_path)
    db.meta_set(conn, "remembered_tools", json.dumps(["delete_task", "no_such_tool", "remember_fact"]))
    assert consent.remembered(conn) == {"remember_fact"}


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def _reviewed_tool_call(*a, **k):
    """A model response proposing a REVIEWED write (remember_fact)."""
    args = json.dumps({"text": "I like tea"})
    return {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "remember_fact", "arguments": args}},
    ]}}]}


def test_approve_with_remember_roundtrip(client: TestClient, monkeypatch) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    monkeypatch.setattr(gateway, "chat_with_tools", _reviewed_tool_call)
    assert client.get("/api/agent/remembered").json()["tools"] == []  # nothing remembered yet
    # A REVIEWED write parks for approval (consent empty).
    turn = client.post("/api/agent/turn", json={"messages": [{"role": "user", "content": "remember"}], "model": "m"})
    body = turn.json()
    assert body["status"] == "awaiting_approval"
    pid = body["pending"][0]["id"]
    # Approve + remember -> consent recorded.
    assert client.post(f"/api/agent/pending/{pid}/approve", json={"remember": True}).status_code == 200
    assert client.get("/api/agent/remembered").json()["tools"] == ["remember_fact"]
    # Revoke.
    assert client.delete("/api/agent/remembered/remember_fact").status_code == 200
    assert client.get("/api/agent/remembered").json()["tools"] == []
