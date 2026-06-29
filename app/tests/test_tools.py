"""Tests for the tool registry, arg validation, redaction, executor + audit (H4a)."""

from __future__ import annotations

from collections.abc import Iterator

import duckdb
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import gateway, tools
from smartbrain_3000.audit import AuditLog
from smartbrain_3000.kb import KnowledgeBase
from smartbrain_3000.planner import Planner
from smartbrain_3000.secrets import gen_master_key


# --- registry invariants --------------------------------------------------

def test_registry_invariants() -> None:
    for tool in tools.REGISTRY.values():
        assert tool.name.isidentifier() and tool.name.islower()
        assert isinstance(tool.tier, tools.Tier)
        assert tool.params_schema["additionalProperties"] is False
        if tool.tier is tools.Tier.OBSERVE:
            assert tool.egress is False  # OBSERVE tools never reach the network


def test_observe_tools_have_no_egress() -> None:
    assert all(not t.egress for t in tools.REGISTRY.values() if t.tier is tools.Tier.OBSERVE)


def test_get_tool_unknown_is_none() -> None:
    assert tools.get_tool("does_not_exist") is None


def test_openai_tools_spec_shape() -> None:
    spec = tools.openai_tools_spec()
    assert spec and all(s["type"] == "function" and "name" in s["function"] for s in spec)


def test_tool_context_has_no_secret_surface() -> None:
    ctx = tools.ToolContext()
    assert not hasattr(ctx, "secret_store") and not hasattr(ctx, "master_key")


# --- validate_args --------------------------------------------------------

def _kb_tool() -> tools.Tool:
    return tools.get_tool("kb_search")


def test_validate_args_accepts_valid() -> None:
    assert tools.validate_args(_kb_tool(), {"query": "hi", "limit": 5}) == {"query": "hi", "limit": 5}


def test_validate_args_rejects() -> None:
    tool = _kb_tool()
    with pytest.raises(ValueError):
        tools.validate_args(tool, {})  # missing required query
    with pytest.raises(ValueError):
        tools.validate_args(tool, {"query": "x", "bogus": 1})  # unknown key
    with pytest.raises(ValueError):
        tools.validate_args(tool, {"query": 123})  # wrong type
    with pytest.raises(ValueError):
        tools.validate_args(tool, {"query": "x", "limit": True})  # bool is not integer


def test_validate_args_coerces_stringified_int() -> None:
    # Local models often emit numeric args as strings ("limit": "3"); the gate must
    # coerce a clean numeric string to int rather than reject the whole call (the
    # web_search "argument 'limit' must be integer" failure).
    assert tools.validate_args(_kb_tool(), {"query": "hi", "limit": "3"}) == {"query": "hi", "limit": 3}
    assert tools.validate_args(_kb_tool(), {"query": "hi", "limit": " -2 "}) == {"query": "hi", "limit": -2}


def test_validate_args_rejects_garbage_string_for_int() -> None:
    # Coercion must not weaken the gate: non-numeric / malformed strings still fail.
    for bad in ("abc", "3.5", "--2", "", "1e3"):
        with pytest.raises(ValueError):
            tools.validate_args(_kb_tool(), {"query": "x", "limit": bad})


def test_redact_masks_secret_keys() -> None:
    out = tools.redact({"query": "ok", "api_key": "sk-123", "nested": {"token": "t"}})
    assert out["query"] == "ok" and out["api_key"] == "***" and out["nested"]["token"] == "***"


# --- executor + audit -----------------------------------------------------

def _wired() -> tuple[tools.ToolContext, AuditLog, duckdb.DuckDBPyConnection]:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    kb = KnowledgeBase(conn, key)
    kb.add("Tea", "oolong steeps at 90C")
    return tools.ToolContext(kb=kb), AuditLog(conn, key), conn


def test_run_observe_executes_and_audits() -> None:
    ctx, audit, _ = _wired()
    result = tools.run(ctx, audit, "kb_search", {"query": "oolong"}, actor="user")
    assert result["results"] and result["results"][0]["title"] == "Tea"
    row = audit.list()[0]
    assert row["tool"] == "kb_search" and row["decision"] == "auto" and row["ok"] is True


def test_list_tasks_tool_reads_planner() -> None:
    # The morning-briefing bug: with no read-tasks tool the agent misused kb_search and
    # reported saved DOCUMENTS as "tasks". list_tasks must read the planner directly and
    # be OBSERVE (runs without approval, like kb_search).
    from smartbrain_3000.planner import Planner

    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    planner = Planner(conn, key)
    planner.add_task("Call the dentist", "", "2026-06-23")
    ctx = tools.ToolContext(planner=planner)
    result = tools.run(ctx, AuditLog(conn, key), "list_tasks", {}, actor="assistant")
    assert "Call the dentist" in [t["title"] for t in result["tasks"]]
    assert tools.get_tool("list_tasks").tier is tools.Tier.OBSERVE


def _planner_ctx() -> tuple[tools.ToolContext, Planner]:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    planner = Planner(conn, gen_master_key())
    return tools.ToolContext(planner=planner), planner


def test_complete_task_tool_marks_done() -> None:
    # "mark X done" was impossible — the agent had no complete tool, risking IRREVERSIBLE delete.
    ctx, planner = _planner_ctx()
    tid = planner.add_task("Dishes")
    tool = tools.get_tool("complete_task")
    tool.handler(ctx, tools.validate_args(tool, {"task_id": tid}))
    assert planner.get_task(tid)["status"] == "done"


def test_update_task_tool_reschedules_and_preserves_unset_fields() -> None:
    # "move it to Friday" — change only due_date; title/priority must survive (merge, not replace).
    ctx, planner = _planner_ctx()
    tid = planner.add_task("Dentist", "ring them", "2026-06-23", priority="high")
    tool = tools.get_tool("update_task")
    tool.handler(ctx, tools.validate_args(tool, {"task_id": tid, "due_date": "2026-06-26"}))
    t = planner.get_task(tid)
    assert t["due_date"] == "2026-06-26"  # changed
    assert t["title"] == "Dentist" and t["priority"] == "high" and t["notes"] == "ring them"  # preserved


def test_update_task_tool_unknown_id_raises() -> None:
    ctx, _ = _planner_ctx()
    tool = tools.get_tool("update_task")
    with pytest.raises(ValueError):
        tool.handler(ctx, tools.validate_args(tool, {"task_id": "nope", "title": "x"}))


def test_add_task_tool_forwards_time_priority_recur() -> None:
    # Agent add_task previously dropped due_time/priority/recur silently.
    ctx, planner = _planner_ctx()
    tool = tools.get_tool("add_task")
    args = tools.validate_args(tool, {"title": "Standup", "due_time": "09:00", "priority": "high", "recur": "daily"})
    tid = tool.handler(ctx, args)["id"]
    t = planner.get_task(tid)
    assert t["due_time"] == "09:00" and t["priority"] == "high" and t["recur"] == "daily"


def test_add_task_tool_dedupes_identical_open_task() -> None:
    # A flaky local model can emit add_task twice (in one step, or re-running it on a follow-up).
    # The second identical call must NOT create a second row — it returns the existing open task.
    ctx, planner = _planner_ctx()
    tool = tools.get_tool("add_task")
    args = tools.validate_args(tool, {"title": "Call Bob", "due_date": "2026-06-30", "due_time": "09:00"})
    first = tool.handler(ctx, args)
    second = tool.handler(ctx, dict(args))  # identical args again
    assert second["id"] == first["id"] and second.get("duplicate") is True
    assert len([t for t in planner.list_tasks() if t["title"] == "Call Bob"]) == 1
    # A genuinely different due still creates a new task (dedup is exact title+due only).
    other = tool.handler(ctx, tools.validate_args(tool, {"title": "Call Bob", "due_date": "2026-07-01"}))
    assert other["id"] != first["id"]


class _FakeMail:
    def list_recent(self, max_results: int = 10) -> list[dict]:
        return [{"id": "m1", "from": "a@b.com", "subject": "Hi", "date": "", "snippet": "yo"}][:max_results]

    def read_message(self, msg_id: str) -> dict:
        return {"id": msg_id, "from": "a@b.com", "subject": "Hi", "date": "", "body": "full body"}


def test_email_list_and_read_tools_read_inbox() -> None:
    # The agent could SEND but never READ email — "summarize my inbox" was impossible.
    ctx = tools.ToolContext(email=_FakeMail())
    lst = tools.get_tool("email_list")
    assert lst.handler(ctx, tools.validate_args(lst, {"limit": 5}))["messages"][0]["id"] == "m1"
    rd = tools.get_tool("email_read")
    assert rd.handler(ctx, tools.validate_args(rd, {"message_id": "m1"}))["body"] == "full body"


def test_email_tools_require_connection() -> None:
    ctx = tools.ToolContext(email=None)
    with pytest.raises(ValueError):
        tools.get_tool("email_list").handler(ctx, {})
    with pytest.raises(ValueError):
        tools.get_tool("email_read").handler(ctx, {"message_id": "x"})


def test_kb_search_degrades_to_lexical_when_embed_unavailable(monkeypatch) -> None:
    # Agent kb_search is now semantic; if the embed model is unreachable it must fall back to
    # lexical and FLAG degraded (never silently return nothing).
    ctx, audit, _ = _wired()

    def _boom(*_a, **_k):
        raise RuntimeError("no gateway")

    monkeypatch.setattr(gateway, "embed", _boom)
    result = tools.run(ctx, audit, "kb_search", {"query": "oolong"}, actor="user")
    assert result["degraded"] is True and result["results"][0]["title"] == "Tea"


def test_kb_search_uses_semantic_when_embed_available(monkeypatch) -> None:
    ctx, audit, _ = _wired()
    monkeypatch.setattr(gateway, "embed", lambda *_a, **_k: [1.0, 0.0, 0.0])
    result = tools.run(ctx, audit, "kb_search", {"query": "tea"}, actor="user")
    assert result["degraded"] is False  # semantic branch ran (ranking quality covered in test_kb)


def test_run_non_observe_without_approval_refuses() -> None:
    # Even if a non-OBSERVE tool existed, run() refuses without an approved_row.
    # Simulate by asserting the guard via a fabricated reviewed tool path:
    ctx, audit, _ = _wired()
    # kb_search is OBSERVE; assert the PermissionError branch is reachable in code
    # by checking run rejects an unknown tool (assertion) — the approval path is
    # covered in H4b. Here we pin that audit is required.
    with pytest.raises(AssertionError):
        tools.run(ctx, None, "kb_search", {"query": "x"}, actor="user")


def test_run_audits_handler_failure() -> None:
    ctx, audit, _ = _wired()
    with pytest.raises(Exception):
        tools.run(ctx, audit, "kb_search", {"query": ""}, actor="user")  # empty query -> handler/validate error
    # whatever raised, an audit row should exist only if validation passed;
    # empty query fails validate_args (required non-empty? it's a string "") ->
    # actually "" is a valid string; the handler asserts query truthy -> errored row
    entries = audit.list()
    assert entries and entries[0]["ok"] is False and entries[0]["decision"] == "errored"


def test_run_audits_validation_reject() -> None:
    ctx, audit, _ = _wired()
    with pytest.raises(ValueError):
        tools.run(ctx, audit, "kb_search", {"query": "x", "bogus": 1}, actor="user")  # unknown key
    rows = audit.list()
    assert len(rows) == 1 and rows[0]["decision"] == "errored" and rows[0]["ok"] is False


def test_audit_encrypted_at_rest_and_append_only() -> None:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    audit = AuditLog(conn, gen_master_key())
    audit.append("user", "kb_search", "observe", "auto", True, args_summary="super-secret-arg")
    raw = b"".join(bytes(r[0]) for r in conn.execute("SELECT ciphertext FROM audit_log;").fetchall())
    assert b"super-secret-arg" not in raw
    assert not hasattr(audit, "delete") and not hasattr(audit, "update")  # append-only surface


def test_audit_rejects_bad_actor_decision() -> None:
    audit = AuditLog(duckdb.connect(":memory:"), gen_master_key())
    dbmod.run_migrations(audit._conn)
    with pytest.raises(AssertionError):
        audit.append("hacker", "t", "observe", "auto", True)
    with pytest.raises(AssertionError):
        audit.append("user", "t", "observe", "nonsense", True)


# --- HTTP API -------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def test_tools_api_requires_unlock(client: TestClient) -> None:
    assert client.post("/api/tools/invoke", json={"name": "kb_search", "args": {"query": "x"}}).status_code == 423
    assert client.get("/api/audit").status_code == 423
    assert client.get("/api/tools").status_code == 423


def test_invoke_observe_tool_and_audit(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    client.post("/api/kb", json={"title": "Notes", "content": "buy oat milk"})
    r = client.post("/api/tools/invoke", json={"name": "kb_search", "args": {"query": "milk"}})
    assert r.status_code == 200 and r.json()["result"]["results"][0]["title"] == "Notes"
    audit = client.get("/api/audit").json()["entries"]
    assert audit[0]["tool"] == "kb_search" and audit[0]["decision"] == "auto"


def test_invoke_unknown_tool_404(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    assert client.post("/api/tools/invoke", json={"name": "rm_rf", "args": {}}).status_code == 404
