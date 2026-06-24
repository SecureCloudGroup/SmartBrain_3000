"""End-to-end: REAL app -> REAL agent -> REAL gateway -> REAL local Bifrost.

This is the test that would have caught BOTH shipped bugs. Nothing in the agent/
gateway chain is monkeypatched; the model's tool call arrives over a real socket.
We assert the chain a user actually exercises: ask to add a task -> it PARKS for
approval (not narrated) -> approve -> the task really lands in the planner. And the
streaming endpoint must OFFER tools (the regression that let the model narrate).
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from _fakegateway import FakeGateway

_ADD_TASK_CALL = [{
    "id": "call_1", "type": "function",
    "function": {"name": "add_task", "arguments": json.dumps({"title": "Call the dentist", "due_date": "2026-06-23"})},
}]


@pytest.fixture()
def app_and_gateway(tmp_path, monkeypatch) -> Iterator[tuple[TestClient, FakeGateway]]:
    server = FakeGateway().start()
    monkeypatch.setenv("SMARTBRAIN_LLM_GATEWAY_URL", server.url)
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "it.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as client:
        client.post("/api/account/setup", json={"passphrase": "correct-horse"})
        yield client, server
    server.stop()


def _turn(client: TestClient, text: str) -> dict:
    r = client.post("/api/agent/turn", json={
        "messages": [{"role": "user", "content": text}], "model": "gemini/gemini-2.5-flash",
    })
    assert r.status_code == 200, r.text
    return r.json()


def test_action_parks_for_approval_then_executes(app_and_gateway) -> None:
    client, server = app_and_gateway
    server.reply_tool_calls = _ADD_TASK_CALL  # the model decides to call add_task

    # 1) Ask to add a task -> it must PARK, not be narrated as done.
    result = _turn(client, "add a task to call the dentist tomorrow at 2 pm")
    assert result["status"] == "awaiting_approval", result
    assert result["pending"][0]["tool"] == "add_task"
    pid = result["pending"][0]["id"]

    # 2) Nothing is in the planner yet — a parked action has NOT run.
    assert client.get("/api/tasks").json()["tasks"] == []

    # 3) Approve it -> the REAL add_task tool runs and the task lands.
    server.reply_tool_calls = None  # the post-approval turn should just answer
    ok = client.post(f"/api/agent/pending/{pid}/approve", json={})
    assert ok.status_code == 200, ok.text
    titles = [t["title"] for t in client.get("/api/tasks").json()["tasks"]]
    assert "Call the dentist" in titles

    # 4) The whole thing is audited: proposed -> approved -> executed.
    entries = client.get("/api/audit").json()["entries"]
    phases = [e["decision"] for e in entries if e["tool"] == "add_task"]
    assert {"proposed", "approved", "executed"} <= set(phases), phases


def test_stream_endpoint_offers_tools_and_signals_tool_turn(app_and_gateway) -> None:
    # The streaming fast path must offer tools (the bug: it didn't, so the model
    # narrated). With a tool call queued, the stream must emit `pending` so the UI
    # falls back to the approval flow — never `done` on a fabricated action.
    client, server = app_and_gateway
    server.reply_tool_calls = _ADD_TASK_CALL
    r = client.post("/api/agent/turn/stream", json={
        "messages": [{"role": "user", "content": "add a task"}], "model": "gemini/gemini-2.5-flash",
    })
    assert r.status_code == 200
    events = [ln[len("event: "):] for ln in r.text.splitlines() if ln.startswith("event: ")]
    assert "pending" in events and "done" not in events
    # And prove tools were actually offered on the wire.
    sent = server.last("/v1/chat/completions")
    assert sent["tools"] and any(t["function"]["name"] == "add_task" for t in sent["tools"])


def test_knowledge_semantic_search_end_to_end(app_and_gateway) -> None:
    # Real path: add a note -> reindex (embeds via the real gateway over a socket) ->
    # semantic search (embeds the query, ranks by cosine in real DuckDB) returns the doc.
    # Catches a silently-broken embedding path (would show as degraded/no results).
    client, server = app_and_gateway
    server.embedding = [0.11, 0.22, 0.33, 0.44]
    doc = client.post("/api/kb", json={"title": "Lease", "content": "The lease renews automatically."})
    assert doc.status_code == 200, doc.text
    rx = client.post("/api/kb/reindex").json()
    assert rx["embedded"] >= 1 and rx["failed"] == 0, rx
    res = client.get("/api/kb/search", params={"q": "rental agreement", "mode": "semantic"}).json()
    assert res.get("degraded") is False, res
    assert any(r["title"] == "Lease" for r in res["results"]), res
    assert server.last("/v1/embeddings")["model"]  # embeddings really went over the wire


def test_knowledge_semantic_ranking(app_and_gateway) -> None:
    # With input-dependent embeddings (server.embedding left None), the query must rank the
    # topically-matching doc ABOVE the unrelated one — exercises real cosine ranking, not
    # just the wire path. (Caught the Supervisor's "constant embedding = untested ranking".)
    client, server = app_and_gateway
    client.post("/api/kb", json={"title": "Lease", "content": "The lease and rent renew automatically."})
    client.post("/api/kb", json={"title": "Dog", "content": "The dog and car need attention tomorrow."})
    rx = client.post("/api/kb/reindex").json()
    assert rx["embedded"] >= 2 and rx["failed"] == 0, rx
    res = client.get("/api/kb/search", params={"q": "rent lease renewal", "mode": "semantic"}).json()
    assert res["degraded"] is False, res
    assert res["results"], res
    assert res["results"][0]["title"] == "Lease", res  # ranked above the unrelated Dog doc


def test_local_model_save_surfaces_gateway_sync_failure(app_and_gateway) -> None:
    # Saving a local model must NOT report plain success when gateway registration failed
    # (Finding 2/3: was returning {"ok": true} unconditionally).
    client, server = app_and_gateway
    server.fail_status = 503  # Bifrost admin unreachable -> registration raises
    r = client.put("/api/local-models/ollama", json={"url": "http://host.docker.internal:11434"})
    assert r.status_code == 200 and r.json()["gateway_synced"] is False, r.text
    server.fail_status = None  # reachable now -> registration succeeds
    r2 = client.put("/api/local-models/ollama", json={"url": "http://host.docker.internal:11434"})
    assert r2.json()["gateway_synced"] is True, r2.text


def test_schedule_run_now_drives_agent_through_gateway(app_and_gateway) -> None:
    # Real chain: POST run-now -> run_schedule -> agent.run_turn -> real gateway -> tool call
    # over a socket -> parks for approval. Catches a scheduler<->agent contract drift that the
    # mocked scheduler tests (which patch agent.run_turn) cannot see.
    client, server = app_and_gateway
    server.reply_tool_calls = _ADD_TASK_CALL
    made = client.post("/api/schedules", json={
        "title": "Daily dentist", "prompt": "add a task to call the dentist",
        "interval_minutes": 0, "model": "gemini/gemini-2.5-flash",
    })
    assert made.status_code == 200, made.text
    sid = made.json().get("id") or client.get("/api/schedules").json()["schedules"][0]["id"]
    r = client.post(f"/api/schedules/{sid}/run")
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["status"] == "awaiting_approval", result
    assert result["pending"][0]["tool"] == "add_task", result


def test_plain_question_streams_text(app_and_gateway) -> None:
    client, server = app_and_gateway
    server.reply_text = "Paris is the capital of France."
    server.reply_tool_calls = None
    r = client.post("/api/agent/turn/stream", json={
        "messages": [{"role": "user", "content": "capital of France?"}], "model": "gemini/gemini-2.5-flash",
    })
    assert r.status_code == 200
    deltas = [ln for ln in r.text.splitlines() if ln.startswith("event: delta")]
    assert deltas  # streamed token-by-token
    assert "event: done" in r.text
