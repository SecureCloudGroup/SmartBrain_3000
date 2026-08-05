"""Tests for the bounded agentic tool-calling loop (H4c). Gateway is mocked."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

import duckdb
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import agent, agent_routes
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
    # Finalization unavailable too (gateway down) -> the bound still terminates the loop.
    def _down(*a, **k):
        raise gateway.GatewayError(502, "gateway down")
    monkeypatch.setattr(gateway, "chat", _down)
    r = _run(ctx, audit, approvals)
    assert r["status"] == "max_steps"  # never loops forever


def test_exhausted_budget_finalizes_with_an_answer(monkeypatch) -> None:
    # The internal counter must never reach chat as the whole reply ("step budget
    # exhausted", seen live after a long document was paged until the budget died):
    # when steps run out, one tools-disabled call answers from the gathered results.
    ctx, audit, approvals = _wired()
    monkeypatch.setattr(gateway, "chat_with_tools", lambda *a, **k: _toolcalls(("kb_search", {"query": "x"})))
    seen = {}

    def _plain(messages, model, timeout=60.0):
        seen["last"] = messages[-1]
        return _text("Here is the summary from what I read.")

    monkeypatch.setattr(gateway, "chat", _plain)
    r = _run(ctx, audit, approvals)
    assert r["status"] == "complete" and r["message"] == "Here is the summary from what I read."
    assert r["steps"] == agent._MAX_STEPS and r["sources"] == []
    assert seen["last"]["role"] == "system", "the answer-now nudge rides in as a system line"
    assert "do not request any more" in seen["last"]["content"].lower()


def test_result_cap_truncates_tool_result_fed_back(monkeypatch) -> None:
    # A big OBSERVE result (a full document) is truncated to result_cap before it's fed to the model,
    # so a big-context model can be given a bigger cap than a small one.
    ctx, audit, approvals = _wired()
    ctx.kb.add("Long", "L" * 5000)
    calls = _recorder(monkeypatch, [_toolcalls(("read_document", {"query": "Long"})), _text("done")])
    agent.run_turn(ctx, audit, approvals, messages=[{"role": "user", "content": "hi"}],
                   model="m", conversation_id=None, turn_id="cap", result_cap=100)
    tool_msg = next(m for m in calls[1] if m.get("role") == "tool")
    assert len(tool_msg["content"]) == 100  # honored the passed cap


def test_result_cap_defaults_to_no_extra_truncation(monkeypatch) -> None:
    # Omitting result_cap keeps the historical default (_RESULT_CAP), so existing callers are unaffected.
    ctx, audit, approvals = _wired()
    ctx.kb.add("Long", "L" * 5000)
    calls = _recorder(monkeypatch, [_toolcalls(("read_document", {"query": "Long"})), _text("done")])
    _run(ctx, audit, approvals)  # no result_cap -> default 8000, well above this ~5KB result
    tool_msg = next(m for m in calls[1] if m.get("role") == "tool")
    assert 5000 < len(tool_msg["content"]) <= agent._RESULT_CAP  # full result, not clipped to 100


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


def test_function_keyed_tool_blob_gets_guidance_notice(monkeypatch) -> None:
    # Qwen2.5-Coder-style leak: a {"function": ..., "arguments": ...} blob ("function"
    # instead of "name", so unrecoverable). The reply is kept but a guidance notice is
    # appended so the user blames the model, not the app.
    ctx, audit, approvals = _wired()
    blob = '```json\n{"function": "read_document", "arguments": {"doc_id": "1"}}\n```'
    _script(monkeypatch, [_text(blob)])
    r = _run(ctx, audit, approvals)
    assert r["status"] == "complete"
    assert r["message"].startswith(blob)  # original reply preserved
    assert "Settings → Model routing" in r["message"]  # guidance appended


def test_normal_prose_gets_no_tool_notice(monkeypatch) -> None:
    # Prose that merely talks about tools/functions/arguments must NOT trip the probe.
    ctx, audit, approvals = _wired()
    prose = 'The "arguments" of a function are its parameters; a tool call names both.'
    _script(monkeypatch, [_text(prose)])
    r = _run(ctx, audit, approvals)
    assert r["status"] == "complete" and r["message"] == prose


def test_looks_like_tool_attempt_shapes() -> None:
    assert agent._looks_like_tool_attempt('{"function": "read_document", "arguments": {"doc_id": "1"}}')
    assert agent._looks_like_tool_attempt('```json\n{"tool": "search", "parameters": {"q": "tea"}}\n```')
    assert agent._looks_like_tool_attempt('{"tool_call": {"name": "x", "arguments": {}}}')  # one envelope deep
    assert agent._looks_like_tool_attempt('{"name": "not_a_tool", "arguments": {}}')  # unknown tool still flagged
    assert not agent._looks_like_tool_attempt("a normal answer, no tools here")
    assert not agent._looks_like_tool_attempt('use the "arguments" keyword when calling a function')  # prose, not JSON
    assert not agent._looks_like_tool_attempt('{"function": "read_document"}')  # no arguments-ish key


def test_degrades_when_tools_unsupported(monkeypatch) -> None:
    ctx, audit, approvals = _wired()

    def _raise(*a, **k):
        err = gateway.GatewayError(400, "model does not support tools")
        err.tools_unsupported = True
        raise err

    monkeypatch.setattr(gateway, "chat_with_tools", _raise)
    monkeypatch.setattr(gateway, "chat", lambda messages, model, **k: _text("plain answer"))
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
    monkeypatch.setattr(gateway, "chat", lambda messages, model, **k: _text("plain ok"))
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
        json={"messages": [{"role": "user", "content": "hi"}], "capability": "chat", "conversation_id": "c1"},
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
        json={"messages": [{"role": "user", "content": "hi"}], "capability": "chat"},
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
        json={"messages": [{"role": "user", "content": "hi"}], "capability": "chat"},
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
        json={"messages": [{"role": "user", "content": "add a task"}], "capability": "chat"},
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
        json={"messages": [{"role": "user", "content": "hi"}], "capability": "chat"},
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
    monkeypatch.setattr(gateway, "chat", lambda messages, model, **k: _text("plain answer"))
    r = http_client.post(
        "/api/agent/turn",
        json={"messages": [{"role": "user", "content": "hi"}], "capability": "chat"},
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
        json={"messages": [{"role": "user", "content": "hi"}], "capability": "chat"},
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


# --- deterministic chat citations: sources come from TOOL RESULTS, never model prose ---

def test_kb_search_result_yields_sources(monkeypatch) -> None:
    ctx, audit, approvals = _wired()
    _script(monkeypatch, [_toolcalls(("kb_search", {"query": "tea"})), _text("found it")])
    r = _run(ctx, audit, approvals)
    assert r["status"] == "complete"
    (src,) = r["sources"]
    assert src["id"] and src["title"] == "Doc"
    assert set(src) == {"id", "title", "source", "page", "page_label", "offset"}


def test_read_document_result_yields_one_document_source(monkeypatch) -> None:
    ctx, audit, approvals = _wired()
    _script(monkeypatch, [_toolcalls(("read_document", {"query": "Doc"})), _text("done")])
    r = _run(ctx, audit, approvals)
    assert r["status"] == "complete"
    (src,) = r["sources"]
    # One citation for the whole document; offset None -> Knowledge opens it at the top.
    assert src["id"] and src["title"] == "Doc" and src["offset"] is None


def test_plain_answer_completes_with_empty_sources(monkeypatch) -> None:
    ctx, audit, approvals = _wired()
    _script(monkeypatch, [_text("just an answer")])
    r = _run(ctx, audit, approvals)
    assert r["status"] == "complete" and r["sources"] == []


def test_sources_survive_a_park_and_resume(monkeypatch) -> None:
    # kb_search runs inline, remember_fact parks. After approval, the resumed turn must
    # still cite the search that ran BEFORE the pause (turn_state carries the messages).
    ctx, audit, approvals = _wired()
    _script(monkeypatch, [_toolcalls(("kb_search", {"query": "tea"}), ("remember_fact", {"text": "likes tea"}))])
    r = _run(ctx, audit, approvals, turn_id="t-cite")
    assert r["status"] == "awaiting_approval"
    _approve_and_execute(ctx, audit, approvals, r["pending"][0]["id"], "remember_fact")
    _script(monkeypatch, [_text("done — noted")])
    r2 = agent.resume_turn(ctx, audit, approvals, "t-cite")
    assert r2["status"] == "complete"
    assert [s["title"] for s in r2["sources"]] == ["Doc"]


def test_citations_from_malformed_or_foreign_results_are_empty() -> None:
    assert agent._citations_from("kb_search", "not json") == []
    assert agent._citations_from("kb_search", json.dumps({"error": "boom"})) == []
    assert agent._citations_from("kb_search", json.dumps(["not", "a", "dict"])) == []
    assert agent._citations_from("read_document", json.dumps({"title": "no id"})) == []
    assert agent._citations_from("add_task", json.dumps({"id": "x", "title": "t"})) == []


def test_collect_sources_dedupes_and_bounds() -> None:
    hits = [{"id": f"d{i}", "title": f"T{i}", "source": "", "page": None, "page_label": "page", "offset": i}
            for i in range(30)]
    call = {"id": "c1", "type": "function", "function": {"name": "kb_search", "arguments": "{}"}}
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"results": hits, "degraded": False})},
        # The same result fed back twice (e.g. a repeated search) must not double the chips.
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"results": hits[:2], "degraded": False})},
    ]
    out = agent._collect_sources(messages)
    assert len(out) == agent._MAX_SOURCES  # bounded (30 hits offered)
    assert len({(s["id"], s["offset"]) for s in out}) == len(out)  # deduped by (id, offset)


def test_fit_for_finalize_rebuilds_within_budget() -> None:
    # The transcript at exhaustion can exceed the model's context (seen live: five
    # 34k-char pages of one document into a 32k-token model) — the rescue prompt must
    # be rebuilt to fit, keeping the question, the FIRST result, and the newest work.
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "summarize the deal"},
        {"role": "assistant", "content": "", "tool_calls": [{}]},
        {"role": "tool", "content": "FIRST" + "a" * 5000},
        {"role": "assistant", "content": "", "tool_calls": [{}]},
        {"role": "tool", "content": "MID" + "b" * 5000},
        {"role": "assistant", "content": "", "tool_calls": [{}]},
        {"role": "tool", "content": "NEWEST" + "c" * 5000},
    ]
    out = agent._fit_for_finalize(msgs, budget_chars=9000)
    total = sum(len(m["content"]) for m in out)
    assert total <= 9000 + len(agent._EXHAUSTED_NUDGE) + 200  # nudge + label ride above the results budget
    assert out[0]["content"] == "sys" and out[1]["content"] == "summarize the deal"
    flat = out[2]["content"]
    assert "FIRST" in flat and "NEWEST" in flat, "anchors: first result + newest work"
    assert out[-1]["content"] == agent._EXHAUSTED_NUDGE
    assert all(m.get("role") != "tool" for m in out), "plain chat call — no orphaned tool roles"


def test_fit_for_finalize_no_tools_is_just_question_plus_nudge() -> None:
    msgs = [{"role": "user", "content": "hello"}]
    out = agent._fit_for_finalize(msgs, budget_chars=1000)
    assert [m["role"] for m in out] == ["user", "system"]
    assert out[-1]["content"] == agent._EXHAUSTED_NUDGE


def test_run_turn_emits_tool_events(monkeypatch) -> None:
    # A4: the loop narrates inline executions — start (with a redacted detail) and
    # done (with ok) per tool — and a listener that throws never breaks the turn.
    ctx, audit, approvals = _wired()
    _script(monkeypatch, [_toolcalls(("kb_search", {"query": "oolong"})), _text("done!")])
    events: list[dict] = []
    r = agent.run_turn(ctx, audit, approvals, messages=[{"role": "user", "content": "hi"}],
                       model="m", conversation_id=None, turn_id="t-ev",
                       on_event=events.append)
    assert r["status"] == "complete" and r["message"] == "done!"
    assert [e["state"] for e in events] == ["start", "done"]
    assert events[0]["tool"] == "kb_search" and events[0]["detail"] == "oolong"
    assert events[1]["ok"] is True


def test_run_turn_survives_broken_event_listener(monkeypatch) -> None:
    ctx, audit, approvals = _wired()
    _script(monkeypatch, [_toolcalls(("kb_search", {"query": "x"})), _text("fine")])

    def boom(_ev):
        raise RuntimeError("listener bug")

    r = agent.run_turn(ctx, audit, approvals, messages=[{"role": "user", "content": "hi"}],
                       model="m", conversation_id=None, turn_id="t-ev2", on_event=boom)
    assert r["status"] == "complete" and r["message"] == "fine"


def test_turn_stops_asking_for_tools_once_context_budget_reached(monkeypatch) -> None:
    # Full-window results stacked past the model's context made every later round-trip
    # re-prefill a huge prompt (a live turn ran 10+ minutes). Once gathered results
    # reach the finalize budget, the loop must answer instead of asking for more.
    ctx, audit, approvals = _wired()
    calls = {"tools": 0}

    def scripted(messages, model, spec, timeout=60.0):
        calls["tools"] += 1
        return _toolcalls(("kb_search", {"query": "x"}))

    monkeypatch.setattr(gateway, "chat_with_tools", scripted)
    monkeypatch.setattr(tools, "run", lambda *a, **k: {"pad": "x" * 900})  # ~1 result_cap each
    monkeypatch.setattr(gateway, "chat", lambda msgs, model, timeout=60.0: _text("synthesized"))
    r = agent.run_turn(ctx, audit, approvals, messages=[{"role": "user", "content": "hi"}],
                       model="m", conversation_id=None, turn_id="t-budget", result_cap=1000)
    assert r["status"] == "complete" and r["message"] == "synthesized"
    # budget = 2.4 * 1000 -> three ~910-char results trip it; the 4th round-trip never happens
    assert calls["tools"] == 3 < agent._MAX_STEPS


def test_citations_weighted_by_depth_of_use() -> None:
    # Broad searches surfaced every unrelated file as a chip (seen live). With a read
    # in the turn: search hits survive only as page links INTO read documents, the
    # same (doc, page) from two queries collapses, and the read doc's whole-document
    # chip yields to its page chips.
    def tcall(cid, name):
        return {"id": cid, "type": "function", "function": {"name": name, "arguments": "{}"}}

    def hit(doc, page):
        return {"id": doc, "title": doc, "source": f"{doc}.pdf", "page": page,
                "page_label": "page", "offset": 0}

    msgs = [
        {"role": "user", "content": "summarize the deal"},
        {"role": "assistant", "content": "", "tool_calls": [tcall("c1", "kb_search")]},
        {"role": "tool", "tool_call_id": "c1",
         "content": json.dumps({"results": [hit("s1", 6), hit("unrelated", 2)]})},
        {"role": "assistant", "content": "", "tool_calls": [tcall("c2", "kb_search")]},
        {"role": "tool", "tool_call_id": "c2",
         "content": json.dumps({"results": [hit("s1", 6), hit("s1", 29), hit("other", None)]})},
        {"role": "assistant", "content": "", "tool_calls": [tcall("c3", "read_document")]},
        {"role": "tool", "tool_call_id": "c3",
         "content": json.dumps({"id": "s1", "title": "s1", "content": "..."})},
    ]
    out = agent._collect_sources(msgs)
    assert [(s["id"], s["page"]) for s in out] == [("s1", 6), ("s1", 29)]
    assert all(s["id"] == "s1" for s in out), "unread docs are never cited once a read happened"


def test_citations_search_only_turn_still_cites_hits() -> None:
    def tcall(cid, name):
        return {"id": cid, "type": "function", "function": {"name": name, "arguments": "{}"}}

    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [tcall("c1", "kb_search")]},
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"results": [
            {"id": "a", "title": "A", "source": "", "page": None, "page_label": "page", "offset": 0}]})},
    ]
    out = agent._collect_sources(msgs)
    assert [s["id"] for s in out] == ["a"], "snippets were the whole evidence — keep them"


# --- SSE liveness: a slow first token must never look like a dead connection ----
# The 2026-07-29 failure: a local model took 8.02s to its first token and this stream
# emitted NOTHING for that whole window (no first bytes, no heartbeat, no Cache-Control,
# unlike the sibling /events endpoint). Safari dropped the idle body; the browser showed
# "Couldn't reach SmartBrain" for a turn the server had completed and recorded.


def test_stream_keeps_the_connection_warm_while_the_model_thinks(
    http_client: TestClient, monkeypatch
) -> None:
    http_client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    monkeypatch.setattr(agent_routes, "_SSE_HEARTBEAT_SECONDS", 0.15)

    def slow(messages, model, **kw):
        def gen():
            time.sleep(0.9)  # model prefill — the window that used to be silent
            yield {"delta": "hi there", "tool_calls": None, "finish_reason": "stop"}
        return gen()

    monkeypatch.setattr(gateway, "chat_stream", slow)
    r = http_client.post(
        "/api/agent/turn/stream",
        json={"messages": [{"role": "user", "content": "hi"}], "capability": "chat"},
    )
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"  # the sibling endpoint always had this
    body = r.text
    assert body.startswith(": open"), "bytes must hit the wire before the model is even called"
    head = body.split("event: delta", 1)[0]
    assert head.count(": keepalive") >= 3, f"heartbeats must fill the wait, got: {head!r}"
    # Comment frames must not disturb the protocol: the real frames still arrive in order.
    frames = _parse_sse(body)
    assert [e for e, _ in frames] == ["delta", "done"]
    assert frames[-1][1]["message"] == "hi there"
    assert body.rstrip().endswith("}"), "the stream must end on its terminal frame, not a heartbeat"


def test_stream_closes_its_producer_so_the_model_slot_is_released(
    http_client: TestClient, monkeypatch
) -> None:
    # The wrapper runs the producer in a thread; if it never closed that generator, the
    # local-model semaphore (held inside gateway.chat_stream) would leak and wedge every
    # later local call. Deterministic close is the guarantee.
    http_client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    cleaned: list[bool] = []

    def fake_producer(*args, **kwargs):
        try:
            yield agent_routes._sse_event("done", {"message": "ok", "conversation_id": None, "model": "m"})
        finally:
            cleaned.append(True)

    monkeypatch.setattr(agent_routes, "_stream_first_response", fake_producer)
    r = http_client.post(
        "/api/agent/turn/stream",
        json={"messages": [{"role": "user", "content": "hi"}], "capability": "chat"},
    )
    assert r.status_code == 200
    assert cleaned == [True], "the producer generator must be closed (releases model slot + client)"


# --- a transient server error must never strip the tools (trust-critical) ------
# Live evidence: the local model server returned 409 "is busy; cannot reload runtime
# settings variant" six times while reloading a model. 409 is not a tools rejection, but
# ANY GatewayError on the first call fell through to the plain-answer fallback — which
# re-asks with NO tools and flags the turn degraded. An action request then reaches a
# model that cannot act, and such a model narrates the action instead of performing it.


def test_transient_error_retries_with_tools_instead_of_degrading(monkeypatch) -> None:
    ctx, audit, approvals = _wired()
    monkeypatch.setattr(agent, "_TRANSIENT_RETRY_SECONDS", 0.0)
    attempts: list[list] = []

    def flaky(messages, model, tools_spec, **kw):
        attempts.append(tools_spec)
        if len(attempts) == 1:
            raise gateway.GatewayError(409, "Model 'x' is busy; cannot reload runtime settings variant")
        return _toolcalls(("add_task", {"title": "call the dentist"}))

    monkeypatch.setattr(gateway, "chat_with_tools", flaky)
    result = agent.run_turn(ctx, audit, approvals,
                            messages=[{"role": "user", "content": "add a task to call the dentist"}],
                            model="mlx/m", conversation_id=None, turn_id="t1")
    assert len(attempts) == 2, "a transient failure must be retried"
    assert attempts[1], "the RETRY must still carry the tools spec"
    assert not result.get("degraded"), "a retried turn is not a degraded turn"
    # The action parked for approval — only possible because the tools survived.
    assert result["status"] == "awaiting_approval", result
    assert [p["tool"] for p in result["pending"]] == ["add_task"]


def test_genuine_tools_rejection_still_falls_back_to_a_plain_answer(monkeypatch) -> None:
    # The forgiving path stays for models that truly cannot use tools: one attempt, no retry.
    ctx, audit, approvals = _wired()
    attempts: list[int] = []

    def refuses(messages, model, tools_spec, **kw):
        attempts.append(1)
        raise gateway.GatewayError(400, "tools are not supported by this model")

    monkeypatch.setattr(gateway, "chat_with_tools", refuses)
    monkeypatch.setattr(gateway, "chat", lambda *a, **k: _text("I can't use tools, but here's an answer."))
    result = agent.run_turn(ctx, audit, approvals,
                            messages=[{"role": "user", "content": "hi"}],
                            model="mlx/m", conversation_id=None, turn_id="t2")
    assert attempts == [1], "a non-transient rejection must NOT be retried"
    assert result["degraded"] is True and result["status"] == "complete"


# --- reassembling a streamed tool call (so an action turn stops paying twice) ---
# A streamed tool call arrives in fragments: the name in one chunk, the JSON arguments
# split across several more. Anything doubtful must return None so the caller falls back
# to re-running the turn — a mis-assembled call could run a tool with wrong arguments,
# and some tools run without asking.


def test_assembles_a_tool_call_split_across_chunks() -> None:
    calls = agent_routes._assemble_tool_calls([
        [{"index": 0, "id": "call_a", "function": {"name": "add_task", "arguments": ""}}],
        [{"index": 0, "function": {"arguments": '{"title": "call '}}],
        [{"index": 0, "function": {"arguments": 'the dentist"}'}}],
    ])
    assert calls == [{
        "id": "call_a", "type": "function",
        "function": {"name": "add_task", "arguments": '{"title": "call the dentist"}'},
    }]


def test_assembles_two_interleaved_calls_in_model_order() -> None:
    calls = agent_routes._assemble_tool_calls([
        [{"index": 0, "id": "a", "function": {"name": "kb_search", "arguments": '{"q'}},
         {"index": 1, "id": "b", "function": {"name": "list_tasks", "arguments": "{}"}}],
        [{"index": 0, "function": {"arguments": 'uery": "tea"}'}}],
    ])
    assert [c["function"]["name"] for c in calls] == ["kb_search", "list_tasks"]
    assert json.loads(calls[0]["function"]["arguments"]) == {"query": "tea"}


def test_doubtful_fragments_refuse_to_assemble() -> None:
    # Truncated JSON (the stream was cut) -> None, never a half-built call.
    assert agent_routes._assemble_tool_calls(
        [[{"index": 0, "id": "a", "function": {"name": "add_task", "arguments": '{"title": "ca'}}]]
    ) is None
    # No function name -> None.
    assert agent_routes._assemble_tool_calls(
        [[{"index": 0, "id": "a", "function": {"arguments": "{}"}}]]
    ) is None
    # Arguments that are not an object -> None.
    assert agent_routes._assemble_tool_calls(
        [[{"index": 0, "id": "a", "function": {"name": "add_task", "arguments": '"just a string"'}}]]
    ) is None
    # Nothing at all -> None.
    assert agent_routes._assemble_tool_calls([]) is None
    # Malformed shapes -> None, not an exception.
    assert agent_routes._assemble_tool_calls([["not-a-dict"]]) is None
    assert agent_routes._assemble_tool_calls([[{"index": "zero", "function": {"name": "x"}}]]) is None


def test_missing_id_is_synthesized_not_refused() -> None:
    # Some servers omit the id on the fragment that carries the name; that is not a defect.
    calls = agent_routes._assemble_tool_calls(
        [[{"index": 0, "function": {"name": "list_tasks", "arguments": "{}"}}]]
    )
    assert calls and calls[0]["id"].startswith("call_0_") and calls[0]["function"]["name"] == "list_tasks"


# --- an action turn must pay for its first model response ONCE -----------------


def _stream_chunks(*chunks):
    def fake(messages, model, **kw):
        return iter(chunks)
    return fake


def test_action_turn_calls_the_model_once_instead_of_twice(http_client: TestClient, monkeypatch) -> None:
    """The whole point: the streaming phase and the tool phase share one model response.

    Measured before this: an identical 4,007-token prompt was sent twice, 4.18s + 3.88s.
    """
    http_client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    monkeypatch.setattr(gateway, "chat_stream", _stream_chunks(
        {"delta": "", "tool_calls": [{"index": 0, "id": "c1",
                                      "function": {"name": "list_tasks", "arguments": ""}}],
         "finish_reason": None},
        {"delta": "", "tool_calls": [{"index": 0, "function": {"arguments": "{}"}}],
         "finish_reason": "tool_calls"},
    ))
    body = {"messages": [{"role": "user", "content": "what are my tasks?"}], "capability": "chat"}
    stream = http_client.post("/api/agent/turn/stream", json=body)
    assert stream.status_code == 200
    pending = [d for e, d in _parse_sse(stream.text) if e == "pending"]
    assert pending and pending[0].get("primed"), "the assembled first response must be offered"

    # The follow-up claims it: the model is NOT asked for that first response again.
    asked: list = []
    monkeypatch.setattr(gateway, "chat_with_tools",
                        lambda *a, **k: asked.append(1) or _text("you have no tasks"))
    events = http_client.post("/api/agent/turn/events", json={**body, "primed": pending[0]["primed"]})
    assert events.status_code == 200
    assert len(asked) == 1, f"one follow-up call after the tool ran, not a repeat of the first ({len(asked)})"


def test_a_claimed_token_cannot_be_reused_or_crossed(http_client: TestClient, monkeypatch) -> None:
    # Tokens are one-shot and conversation-bound: a replay, or a token from a DIFFERENT
    # conversation, must fall back to asking the model rather than answering from a stash.
    http_client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    first = {"role": "user", "content": "first question"}
    token = agent_routes._stash_primed([first], {"choices": [{"message": {"content": "cached"}}]})
    assert agent_routes._take_primed(token, [first]) is not None, "the right conversation claims it"
    assert agent_routes._take_primed(token, [first]) is None, "a second claim gets nothing"
    token2 = agent_routes._stash_primed([first], {"choices": [{"message": {"content": "cached"}}]})
    assert agent_routes._take_primed(token2, [{"role": "user", "content": "other"}]) is None
    assert agent_routes._take_primed(None, [first]) is None
    assert agent_routes._take_primed("nonsense", [first]) is None


def test_unassemblable_tool_calls_fall_back_to_asking_again(http_client: TestClient, monkeypatch) -> None:
    # Truncated arguments must NOT be handed on: no token, so the turn behaves exactly as before.
    http_client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    monkeypatch.setattr(gateway, "chat_stream", _stream_chunks(
        {"delta": "", "tool_calls": [{"index": 0, "id": "c1",
                                      "function": {"name": "add_task", "arguments": '{"title": "ca'}}],
         "finish_reason": "tool_calls"},
    ))
    stream = http_client.post("/api/agent/turn/stream", json={
        "messages": [{"role": "user", "content": "add a task"}], "capability": "chat"})
    pending = [d for e, d in _parse_sse(stream.text) if e == "pending"]
    assert pending and "primed" not in pending[0], "a doubtful assembly must never be offered"


def test_a_truncated_tool_stream_is_never_reused(http_client: TestClient, monkeypatch) -> None:
    """The completeness gate: no terminal finish_reason -> no reuse, ask the model again.

    Empty arguments assemble to a VALID {} — and seven tools (list_documents, list_tasks,
    email_list, read_schedule_output, …) require no arguments and run inline WITHOUT
    approval. A stream cut off mid-call must therefore never be handed on.
    """
    http_client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    monkeypatch.setattr(gateway, "chat_stream", _stream_chunks(
        # the model names a tool, then the connection dies: no finish_reason ever arrives
        {"delta": "", "tool_calls": [{"index": 0, "id": "c1",
                                      "function": {"name": "email_list", "arguments": ""}}],
         "finish_reason": None},
    ))
    stream = http_client.post("/api/agent/turn/stream", json={
        "messages": [{"role": "user", "content": "what did I get today?"}], "capability": "chat"})
    pending = [d for e, d in _parse_sse(stream.text) if e == "pending"]
    assert pending, "the turn still falls back as before"
    assert "primed" not in pending[0], "an unfinished stream must never be reused"


# --- a denial STICKS for the rest of the turn ---------------------------------
# Live UX failure: user denies a fetch, the model immediately re-requests it, new
# pending, deny — "a loop of deny, request, try again, deny". A denied action must
# short-circuit the second try instead of spawning another pending row.


def test_denied_repeat_returns_error_without_a_new_pending(monkeypatch) -> None:
    """Same (name, args) after deny -> inline tool error, NOT another pending row."""
    ctx, audit, approvals = _wired()
    # Turn 1: model requests remember_fact; it parks.
    _script(monkeypatch, [_toolcalls(("remember_fact", {"text": "one"}))])
    r1 = _run(ctx, audit, approvals, turn_id="t-deny")
    pid = r1["pending"][0]["id"]
    # User denies.
    assert approvals.deny(pid) is True
    # On resume, the model re-emits the IDENTICAL call (sorted-key JSON canonical)
    # THEN a plain answer. The repeat must NOT park; it must feed back an error
    # and let the turn complete.
    calls = _recorder(monkeypatch, [
        _toolcalls(("remember_fact", {"text": "one"})),
        _text("ok, skipping"),
    ])
    r2 = agent.resume_turn(ctx, audit, approvals, "t-deny")
    assert r2["status"] == "complete" and r2["message"] == "ok, skipping"
    # Nothing new was parked.
    assert approvals.list_pending() == []
    # The tool_result the model saw on the retry was an error (the short-circuit).
    tool_msgs = [m for m in calls[-1] if m.get("role") == "tool"]
    assert any("already denied" in (m.get("content") or "") for m in tool_msgs)


def test_denied_repeat_with_different_args_still_parks(monkeypatch) -> None:
    """The denial is exact-match: different args must still create a fresh pending."""
    ctx, audit, approvals = _wired()
    _script(monkeypatch, [_toolcalls(("remember_fact", {"text": "one"}))])
    pid = _run(ctx, audit, approvals, turn_id="t-diff")["pending"][0]["id"]
    approvals.deny(pid)
    # Different args -> the classifier doesn't recognize it and parks normally.
    _script(monkeypatch, [_toolcalls(("remember_fact", {"text": "two"}))])
    r2 = agent.resume_turn(ctx, audit, approvals, "t-diff")
    assert r2["status"] == "awaiting_approval"
    assert r2["pending"][0]["tool"] == "remember_fact"


def test_denied_tool_result_wording_reaches_the_model(monkeypatch) -> None:
    """The tool_result fed back on deny must say plainly: user denied; don't retry."""
    ctx, audit, approvals = _wired()
    _script(monkeypatch, [_toolcalls(("remember_fact", {"text": "x"}))])
    pid = _run(ctx, audit, approvals, turn_id="t-word")["pending"][0]["id"]
    approvals.deny(pid)
    calls = _recorder(monkeypatch, [_text("understood")])
    agent.resume_turn(ctx, audit, approvals, "t-word")
    tool_msg = next(m for m in calls[-1] if m.get("role") == "tool")
    content = tool_msg["content"]
    assert "denied" in content.lower()
    assert "again this turn" in content.lower()


def test_canonical_key_ignores_arg_key_order() -> None:
    """Two dicts with the same content in different order collapse to one key."""
    a = agent._canonical_key("web_fetch", {"url": "https://x", "b": 2})
    b = agent._canonical_key("web_fetch", {"b": 2, "url": "https://x"})
    assert a == b
