"""Tests for the bounded agentic tool-calling loop (H4c). Gateway is mocked."""

from __future__ import annotations

import json
from collections.abc import Iterator

import duckdb
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import agent
from smartbrain_3000 import db as dbmod
from smartbrain_3000 import gateway, tools
from smartbrain_3000.approvals import ApprovalStore
from smartbrain_3000.audit import AuditLog
from smartbrain_3000.kb import KnowledgeBase
from smartbrain_3000.memory import MemoryStore
from smartbrain_3000.planner import Planner
from smartbrain_3000.secrets import gen_master_key


def _wired():
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    kb = KnowledgeBase(conn, key)
    kb.add("Doc", "searchable content about tea")
    ctx = tools.ToolContext(kb=kb, planner=Planner(conn, key), memory=MemoryStore(conn, key))
    return ctx, AuditLog(conn, key), ApprovalStore(conn, key, "sess1")


def _text(content):
    return {"choices": [{"message": {"content": content}}]}


_TC_SEQ = [0]


def _toolcalls(*calls):
    # Globally-unique tool_call ids (like a real model), so multi-call/multi-park
    # transcripts don't collide.
    tcs = []
    for n, a in calls:
        _TC_SEQ[0] += 1
        tcs.append({"id": f"call_{_TC_SEQ[0]}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}})
    return {"choices": [{"message": {"content": "", "tool_calls": tcs}}]}


def _script(monkeypatch, responses):
    it = iter(responses)
    monkeypatch.setattr(gateway, "chat_with_tools", lambda *a, **k: next(it))


def _recorder(monkeypatch, responses):
    """Mock chat_with_tools that records the messages it was called with."""
    it = iter(responses)
    calls: list = []

    def fake(messages, model, tools_spec, **k):
        calls.append([dict(m) for m in messages])
        return next(it)

    monkeypatch.setattr(gateway, "chat_with_tools", fake)
    return calls


def _approve_and_execute(ctx, audit, approvals, pid, name):
    assert approvals.approve(pid)
    result = tools.run(ctx, audit, name, approvals.get(pid)["args"], actor="user", claim=lambda: approvals.claim(pid))
    approvals.store_result(pid, result)


def _run(ctx, audit, approvals, msgs="hi", turn_id="t1"):
    return agent.run_turn(
        ctx, audit, approvals,
        messages=[{"role": "user", "content": msgs}], model="m", conversation_id=None, turn_id=turn_id,
    )


def test_no_tool_calls_completes(monkeypatch) -> None:
    ctx, audit, approvals = _wired()
    _script(monkeypatch, [_text("just an answer")])
    r = _run(ctx, audit, approvals)
    assert r["status"] == "complete" and r["message"] == "just an answer" and r["degraded"] is False


def test_observe_tool_auto_runs_then_completes(monkeypatch) -> None:
    ctx, audit, approvals = _wired()
    _script(monkeypatch, [_toolcalls(("kb_search", {"query": "tea"})), _text("found it")])
    r = _run(ctx, audit, approvals)
    assert r["status"] == "complete" and r["message"] == "found it"
    rows = audit.list()
    assert any(e["tool"] == "kb_search" and e["decision"] == "auto" and e["ok"] for e in rows)


def test_reviewed_tool_parks(monkeypatch) -> None:
    ctx, audit, approvals = _wired()
    _script(monkeypatch, [_toolcalls(("remember_fact", {"text": "I like tea"}))])
    r = _run(ctx, audit, approvals)
    assert r["status"] == "awaiting_approval" and r["pending"][0]["tool"] == "remember_fact"
    assert ctx.memory.list_memories() == []  # not executed
    assert approvals.get(r["pending"][0]["id"])["status"] == "pending"


def test_mixed_tier_step_runs_observe_inline_parks_dangerous(monkeypatch) -> None:
    ctx, audit, approvals = _wired()
    tid = ctx.planner.add_task("doomed")
    _script(monkeypatch, [_toolcalls(("kb_search", {"query": "tea"}), ("delete_task", {"task_id": tid}))])
    r = _run(ctx, audit, approvals)
    assert r["status"] == "awaiting_approval" and r["pending"][0]["tool"] == "delete_task"
    assert any(e["tool"] == "kb_search" and e["decision"] == "auto" for e in audit.list())  # observe ran
    assert len(ctx.planner.list_tasks()) == 1  # delete parked, not executed


def test_unknown_and_bad_tool_calls_do_not_crash(monkeypatch) -> None:
    ctx, audit, approvals = _wired()
    _script(monkeypatch, [_toolcalls(("no_such_tool", {})), _text("recovered")])
    r = _run(ctx, audit, approvals)
    assert r["status"] == "complete" and r["message"] == "recovered"


def test_max_steps_bound(monkeypatch) -> None:
    ctx, audit, approvals = _wired()
    monkeypatch.setattr(gateway, "chat_with_tools", lambda *a, **k: _toolcalls(("kb_search", {"query": "x"})))
    r = _run(ctx, audit, approvals)
    assert r["status"] == "max_steps"  # never loops forever


# --- text-emitted tool calls (local models / runtimes that don't parse tool syntax) -------

def test_text_emitted_tool_call_is_recovered_and_parks(monkeypatch) -> None:
    # A model prints a ```json tool call as the message body (no structured tool_calls).
    # run_turn must recover it and PARK it for approval — never show the JSON.
    ctx, audit, approvals = _wired()
    tid = ctx.planner.add_task("Call the dentist", "", "2026-06-23")
    blob = f'```json\n{{"name": "update_task", "arguments": {{"task_id": "{tid}", "due_date": "2026-06-27"}}}}\n```'
    _script(monkeypatch, [_text(blob)])
    r = _run(ctx, audit, approvals)
    assert r["status"] == "awaiting_approval" and r["pending"][0]["tool"] == "update_task"


def test_unparseable_tool_blob_is_hidden_not_shown(monkeypatch) -> None:
    # A leaked tool blob with a // comment (placeholder example) won't parse — we must NOT
    # show the raw JSON; replace it with a clean message.
    ctx, audit, approvals = _wired()
    blob = '```json\n{"name": "update_task", "arguments": {"task_id": "12345", // example\n"due_date": "2026-06-27"}}\n```'
    _script(monkeypatch, [_text(blob)])
    r = _run(ctx, audit, approvals)
    assert r["status"] == "complete"
    assert "```" not in r["message"] and '"arguments"' not in r["message"]  # raw JSON hidden
    assert "tool" in r["message"].lower()  # the clean "couldn't run" notice


def test_extract_text_tool_calls_known_tool_only() -> None:
    assert agent._extract_text_tool_calls('```json {"name":"list_tasks","arguments":{}}```')[0]["function"]["name"] == "list_tasks"
    assert agent._extract_text_tool_calls('{"name":"not_a_tool","arguments":{"x":1}}') == []  # unknown tool ignored
    assert agent._extract_text_tool_calls("a normal answer, no tools here") == []
    assert agent._extract_text_tool_calls("see https://example.com for details") == []  # // in URL must not trip it


def test_extract_text_tool_call_preserves_url_arg() -> None:
    # No comment-stripping, so a URL argument survives intact (would break if we stripped //).
    blob = '```json\n{"name": "web_fetch", "arguments": {"url": "https://example.com/page"}}\n```'
    out = agent._extract_text_tool_calls(blob)
    assert len(out) == 1 and json.loads(out[0]["function"]["arguments"])["url"] == "https://example.com/page"


def test_degrades_when_tools_unsupported(monkeypatch) -> None:
    ctx, audit, approvals = _wired()

    def _raise(*a, **k):
        err = gateway.GatewayError(400, "model does not support tools")
        err.tools_unsupported = True
        raise err

    monkeypatch.setattr(gateway, "chat_with_tools", _raise)
    monkeypatch.setattr(gateway, "chat", lambda messages, model: _text("plain answer"))
    r = _run(ctx, audit, approvals)
    assert r["status"] == "complete" and r["degraded"] is True and r["message"] == "plain answer"


def test_real_gateway_error_fails_closed(monkeypatch) -> None:
    ctx, audit, approvals = _wired()

    def _raise(*a, **k):
        raise gateway.GatewayError(401, "Incorrect API key")  # tools_unsupported stays False

    monkeypatch.setattr(gateway, "chat_with_tools", _raise)
    monkeypatch.setattr(gateway, "chat", _raise)  # the plain fallback hits the same real error
    with pytest.raises(gateway.GatewayError):
        _run(ctx, audit, approvals)  # surfaced, not masked as degraded


def test_degrades_on_any_first_step_error_when_plain_succeeds(monkeypatch) -> None:
    # A model that errors on the tools call (even without the tools_unsupported flag)
    # but can answer plainly should degrade rather than 502 — robust auto-tools.
    ctx, audit, approvals = _wired()

    def _raise(*a, **k):
        raise gateway.GatewayError(500, "upstream rejected the request")  # flag NOT set

    monkeypatch.setattr(gateway, "chat_with_tools", _raise)
    monkeypatch.setattr(gateway, "chat", lambda messages, model: _text("plain ok"))
    r = _run(ctx, audit, approvals)
    assert r["status"] == "complete" and r["degraded"] is True and r["message"] == "plain ok"


def test_remembered_reviewed_runs_inline_not_parked(monkeypatch) -> None:
    ctx, audit, approvals = _wired()
    _script(monkeypatch, [_toolcalls(("remember_fact", {"text": "I like tea"})), _text("done")])
    r = agent.run_turn(
        ctx, audit, approvals, messages=[{"role": "user", "content": "hi"}], model="m",
        conversation_id=None, turn_id="t1", auto_approve={"remember_fact"},
    )
    assert r["status"] == "complete" and r["message"] == "done"  # never parked
    assert ctx.memory.list_memories() != []  # actually executed via remembered consent
    assert any(e["tool"] == "remember_fact" and e["ok"] for e in audit.list())


def test_chokepoint_refuses_standing_claim_for_irreversible() -> None:
    # The irreversible-always-asks invariant lives at tools.run itself: a GRANTED
    # standing claim is refused for IRREVERSIBLE, audited, and never executes.
    ctx, audit, approvals = _wired()
    tid = ctx.planner.add_task("doomed")
    with pytest.raises(PermissionError):
        tools.run(ctx, audit, "delete_task", {"task_id": tid}, actor="assistant", claim=tools.GRANTED)
    assert len(ctx.planner.list_tasks()) == 1  # never executed
    assert any(e["tool"] == "delete_task" and e["decision"] == "errored" for e in audit.list())


def test_irreversible_never_auto_runs_even_if_remembered(monkeypatch) -> None:
    # Safety invariant: an IRREVERSIBLE tool MUST park even if its name is wrongly
    # in the consent set — the tier check guards it, not just the (REVIEWED-only) writer.
    ctx, audit, approvals = _wired()
    tid = ctx.planner.add_task("doomed")
    _script(monkeypatch, [_toolcalls(("delete_task", {"task_id": tid}))])
    r = agent.run_turn(
        ctx, audit, approvals, messages=[{"role": "user", "content": "hi"}], model="m",
        conversation_id=None, turn_id="t1", auto_approve={"delete_task"},
    )
    assert r["status"] == "awaiting_approval"
    assert len(ctx.planner.list_tasks()) == 1  # NOT deleted


def test_resume_after_approve_completes(monkeypatch) -> None:
    ctx, audit, approvals = _wired()
    _script(monkeypatch, [_toolcalls(("remember_fact", {"text": "x"}))])
    r1 = _run(ctx, audit, approvals)
    pid = r1["pending"][0]["id"]
    # simulate the approve route: CAS + claim + execute + store result
    assert approvals.approve(pid)
    result = tools.run(ctx, audit, "remember_fact", approvals.get(pid)["args"], actor="user", claim=lambda: approvals.claim(pid))
    approvals.store_result(pid, result)
    _script(monkeypatch, [_text("done — remembered it")])
    r2 = agent.resume_turn(ctx, audit, approvals, "t1")
    assert r2["status"] == "complete" and "done" in r2["message"]
    assert ctx.memory.list_memories()[0]["text"] == "x"  # the approved action ran


def test_resume_while_pending_stays_awaiting(monkeypatch) -> None:
    ctx, audit, approvals = _wired()
    _script(monkeypatch, [_toolcalls(("remember_fact", {"text": "x"}))])
    _run(ctx, audit, approvals)  # parks; approval not resolved
    assert agent.resume_turn(ctx, audit, approvals, "t1")["status"] == "awaiting_approval"


def test_resume_unknown_turn_is_none() -> None:
    ctx, audit, approvals = _wired()
    assert agent.resume_turn(ctx, audit, approvals, "nope") is None


def test_multi_park_resume_is_wellformed(monkeypatch) -> None:
    # A turn that parks TWICE must resume from the latest park, answering only its
    # calls — well-formed tool-message sequence, budget not reset.
    ctx, audit, approvals = _wired()
    _script(monkeypatch, [_toolcalls(("remember_fact", {"text": "a"}))])
    a = _run(ctx, audit, approvals)["pending"][0]["id"]
    _approve_and_execute(ctx, audit, approvals, a, "remember_fact")
    _script(monkeypatch, [_toolcalls(("add_task", {"title": "b"}))])
    r2 = agent.resume_turn(ctx, audit, approvals, "t1")
    assert r2["status"] == "awaiting_approval"
    b = r2["pending"][0]["id"]
    _approve_and_execute(ctx, audit, approvals, b, "add_task")
    calls = _recorder(monkeypatch, [_text("all done")])
    r3 = agent.resume_turn(ctx, audit, approvals, "t1")
    assert r3["status"] == "complete"
    msgs = calls[-1]
    ids = {tc["id"] for m in msgs if m.get("role") == "assistant" for tc in (m.get("tool_calls") or [])}
    tool_ids = [m["tool_call_id"] for m in msgs if m.get("role") == "tool"]
    assert tool_ids and all(t in ids for t in tool_ids)  # no orphan tool message
    assert len(tool_ids) == len(set(tool_ids))  # no duplicates
    assert ctx.memory.list_memories() and ctx.planner.list_tasks()  # both executed


def test_resume_executed_without_result_feeds_error(monkeypatch) -> None:
    # An executed-but-failed action (no stored result) must not be reported as success.
    ctx, audit, approvals = _wired()
    _script(monkeypatch, [_toolcalls(("remember_fact", {"text": "x"}))])
    pid = _run(ctx, audit, approvals)["pending"][0]["id"]
    approvals.approve(pid)
    approvals.claim(pid)  # executed, but store_result deliberately NOT called (handler-failure shape)
    calls = _recorder(monkeypatch, [_text("ok")])
    agent.resume_turn(ctx, audit, approvals, "t1")
    tool_msg = next(m for m in calls[-1] if m.get("role") == "tool")
    assert "error" in tool_msg["content"]  # not a forged {"ok": true}


def test_invalid_dangerous_args_go_inline_not_parked(monkeypatch) -> None:
    # delete_task with a non-string task_id fails validation -> inline error, not a wedged park.
    ctx, audit, approvals = _wired()
    _script(monkeypatch, [_toolcalls(("delete_task", {"task_id": 123})), _text("could not")])
    r = _run(ctx, audit, approvals)
    assert r["status"] == "complete"  # never parked an invalid call


def test_budget_survives_resume(monkeypatch) -> None:
    ctx, audit, approvals = _wired()
    _script(monkeypatch, [_toolcalls(("remember_fact", {"text": "x"}))])
    pid = _run(ctx, audit, approvals)["pending"][0]["id"]
    _approve_and_execute(ctx, audit, approvals, pid, "remember_fact")
    monkeypatch.setattr(gateway, "chat_with_tools", lambda *a, **k: _toolcalls(("kb_search", {"query": "x"})))
    # resumes at step 1 (not 0) and loops to the step bound — does not restart the budget
    assert agent.resume_turn(ctx, audit, approvals, "t1")["status"] == "max_steps"


def test_tools_unsupported_after_a_tool_ran_fails_closed(monkeypatch) -> None:
    ctx, audit, approvals = _wired()
    seq = iter([_toolcalls(("kb_search", {"query": "x"}))])

    def fake(*a, **k):
        try:
            return next(seq)
        except StopIteration:
            err = gateway.GatewayError(400, "does not support tools")
            err.tools_unsupported = True
            raise err

    monkeypatch.setattr(gateway, "chat_with_tools", fake)
    with pytest.raises(gateway.GatewayError):  # a tool already ran -> do NOT degrade, surface it
        _run(ctx, audit, approvals)


def test_observe_result_is_capped(monkeypatch) -> None:
    ctx, audit, approvals = _wired()
    ctx.kb.search = lambda *a, **k: [{"id": "1", "title": "T", "score": 1, "snippet": "x" * 20000}]
    calls = _recorder(monkeypatch, [_toolcalls(("kb_search", {"query": "q"})), _text("done")])
    _run(ctx, audit, approvals)
    tool_msg = next(m for m in calls[-1] if m.get("role") == "tool")
    assert len(tool_msg["content"]) <= agent._RESULT_CAP


# --- /api/agent/turn/stream (SSE) ----------------------------------------

@pytest.fixture()
def http_client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "stream.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as c:
        yield c


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    """Parse the SSE body into [(event, data-dict), ...] frames. Bounded by frame count."""
    frames: list[tuple[str, dict]] = []
    for raw in body.split("\n\n")[:200]:  # bounded
        chunk = raw.strip()
        if not chunk:
            continue
        event, data = "", {}
        for line in chunk.split("\n"):
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = json.loads(line.split(":", 1)[1].strip())
        if event:
            frames.append((event, data))
    return frames


def test_stream_endpoint_requires_unlock(http_client: TestClient) -> None:
    r = http_client.post("/api/agent/turn/stream", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 423


def _stream(*chunks: dict) -> Iterator[dict]:
    for c in chunks:
        yield c


def test_stream_yields_deltas_then_done(http_client: TestClient, monkeypatch) -> None:
    http_client.post("/api/account/setup", json={"passphrase": "correct-horse"})

    def fake(messages, model, **kw):
        return _stream(
            {"delta": "Hel", "tool_calls": None, "finish_reason": None},
            {"delta": "lo", "tool_calls": None, "finish_reason": None},
            {"delta": "", "tool_calls": None, "finish_reason": "stop"},
        )

    monkeypatch.setattr(gateway, "chat_stream", fake)
    r = http_client.post(
        "/api/agent/turn/stream",
        json={"messages": [{"role": "user", "content": "hi"}], "capability": "fast_chat", "conversation_id": "c1"},
    )
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    frames = _parse_sse(r.text)
    deltas = [d["text"] for ev, d in frames if ev == "delta"]
    assert "".join(deltas) == "Hello"
    terminal = frames[-1]
    assert terminal[0] == "done" and terminal[1]["message"] == "Hello"
    assert terminal[1]["conversation_id"] == "c1"


def test_stream_tool_turn_emits_pending(http_client: TestClient, monkeypatch) -> None:
    http_client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    tc = [{"id": "c1", "type": "function", "function": {"name": "kb_search", "arguments": "{}"}}]

    def fake(messages, model, **kw):
        return _stream(
            {"delta": "thinking", "tool_calls": None, "finish_reason": None},
            {"delta": "", "tool_calls": tc, "finish_reason": None},
        )

    monkeypatch.setattr(gateway, "chat_stream", fake)
    r = http_client.post(
        "/api/agent/turn/stream",
        json={"messages": [{"role": "user", "content": "hi"}], "capability": "fast_chat"},
    )
    assert r.status_code == 200
    frames = _parse_sse(r.text)
    events = [ev for ev, _ in frames]
    assert "pending" in events and events[-1] == "pending"  # terminal pending; client falls back
    assert "done" not in events  # never finished streaming through a tool turn


def test_stream_gateway_error_emits_error_frame(http_client: TestClient, monkeypatch) -> None:
    http_client.post("/api/account/setup", json={"passphrase": "correct-horse"})

    def fake(messages, model, **kw):
        def _gen():
            raise gateway.GatewayError(502, "boom")
            yield  # pragma: no cover
        return _gen()

    monkeypatch.setattr(gateway, "chat_stream", fake)
    r = http_client.post(
        "/api/agent/turn/stream",
        json={"messages": [{"role": "user", "content": "hi"}], "capability": "fast_chat"},
    )
    assert r.status_code == 200  # SSE response already opened — error is the LAST frame
    frames = _parse_sse(r.text)
    assert frames[-1][0] == "error" and frames[-1][1]["status"] == 502


def test_stream_unknown_capability_400(http_client: TestClient) -> None:
    http_client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    r = http_client.post(
        "/api/agent/turn/stream",
        json={"messages": [{"role": "user", "content": "hi"}], "capability": "bogus"},
    )
    assert r.status_code == 400


def test_stream_offers_tools_to_model(http_client: TestClient, monkeypatch) -> None:
    # Regression: the streaming fast path MUST offer tools, or the model can't call one and
    # narrates actions it never performs ("Task added" with no add_task / no audit / no park).
    http_client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    seen: dict = {}

    def fake(messages, model, **kw):
        seen["tools_spec"] = kw.get("tools_spec")
        return _stream({"delta": "hi", "tool_calls": None, "finish_reason": "stop"})

    monkeypatch.setattr(gateway, "chat_stream", fake)
    http_client.post(
        "/api/agent/turn/stream",
        json={"messages": [{"role": "user", "content": "add a task"}], "capability": "fast_chat"},
    )
    assert seen["tools_spec"], "stream must offer tools so the model can actually call one"
    names = [t.get("function", {}).get("name") for t in seen["tools_spec"]]
    assert "add_task" in names


def test_stream_retries_without_tools_when_unsupported(http_client: TestClient, monkeypatch) -> None:
    # A model that rejects the tools field must degrade to a plain stream, not error out.
    http_client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    calls: list = []

    def fake(messages, model, **kw):
        spec = kw.get("tools_spec")
        calls.append(spec)
        if spec is not None:
            err = gateway.GatewayError(400, "tools not supported")
            err.tools_unsupported = True

            def _boom():
                raise err
                yield  # pragma: no cover

            return _boom()
        return _stream({"delta": "plain", "tool_calls": None, "finish_reason": "stop"})

    monkeypatch.setattr(gateway, "chat_stream", fake)
    r = http_client.post(
        "/api/agent/turn/stream",
        json={"messages": [{"role": "user", "content": "hi"}], "capability": "fast_chat"},
    )
    frames = _parse_sse(r.text)
    assert calls[0] is not None and calls[1] is None  # tried with tools, then retried without
    assert frames[-1][0] == "done" and frames[-1][1]["message"] == "plain"


# --- /api/agent/turn HTTP-level error/degradation paths -------------------

def test_turn_route_degrades_when_tools_unsupported(http_client: TestClient, monkeypatch) -> None:
    # The HTTP route must surface a 200 + degraded answer when the model can't do
    # tools BUT plain chat succeeds (matches the unit-level invariant at the
    # route surface, not just inside agent.run_turn).
    http_client.post("/api/account/setup", json={"passphrase": "correct-horse"})

    def tools_die(*a, **k):
        err = gateway.GatewayError(400, "model does not support tools")
        err.tools_unsupported = True
        raise err

    monkeypatch.setattr(gateway, "chat_with_tools", tools_die)
    monkeypatch.setattr(gateway, "chat", lambda messages, model: _text("plain answer"))
    r = http_client.post(
        "/api/agent/turn",
        json={"messages": [{"role": "user", "content": "hi"}], "capability": "fast_chat"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "complete"
    assert body["degraded"] is True and body["message"] == "plain answer"


def test_turn_route_hard_gateway_error_is_502(http_client: TestClient, monkeypatch) -> None:
    # A REAL gateway error (tools_unsupported NOT set; plain chat also fails) must
    # surface as 502 with the upstream message, not a 500.
    http_client.post("/api/account/setup", json={"passphrase": "correct-horse"})

    def hard_error(*a, **k):
        raise gateway.GatewayError(401, "Incorrect API key")

    monkeypatch.setattr(gateway, "chat_with_tools", hard_error)
    monkeypatch.setattr(gateway, "chat", hard_error)
    r = http_client.post(
        "/api/agent/turn",
        json={"messages": [{"role": "user", "content": "hi"}], "capability": "fast_chat"},
    )
    assert r.status_code == 502
    assert "API key" in r.json().get("detail", "")


def test_turn_route_unknown_capability_400(http_client: TestClient) -> None:
    http_client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    r = http_client.post(
        "/api/agent/turn",
        json={"messages": [{"role": "user", "content": "hi"}], "capability": "bogus"},
    )
    assert r.status_code == 400
    assert "bogus" in r.json().get("detail", "")
