"""Tests for the approval gateway: ApprovalStore + the state machine API (H4b)."""

from __future__ import annotations

from collections.abc import Iterator

import duckdb
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import tools
from smartbrain_3000.approvals import ApprovalStore
from smartbrain_3000.audit import AuditLog
from smartbrain_3000.secrets import gen_master_key


def _store(session: str = "s1") -> ApprovalStore:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return ApprovalStore(conn, gen_master_key(), session)


# --- ApprovalStore unit ---------------------------------------------------

def test_create_list_get_pending() -> None:
    s = _store()
    pid = s.create_pending("add_task", "reviewed", {"title": "x"})
    pend = s.list_pending()
    assert len(pend) == 1 and pend[0]["tool"] == "add_task" and pend[0]["args"] == {"title": "x"}
    row = s.get(pid)
    assert row["status"] == "pending" and row["args"] == {"title": "x"} and row["expired"] is False


def test_cas_single_use_claim() -> None:
    s = _store()
    pid = s.create_pending("delete_task", "irreversible", {"task_id": "t"})
    assert s.approve(pid) is True
    assert s.approve(pid) is False  # no longer pending
    assert s.claim(pid) is True  # approved -> executed
    assert s.claim(pid) is False  # single use


def test_deny_blocks_execution() -> None:
    s = _store()
    pid = s.create_pending("add_task", "reviewed", {"title": "x"})
    assert s.deny(pid) is True
    assert s.claim(pid) is False and s.approve(pid) is False


def test_concurrent_approve_exactly_one_wins() -> None:
    from concurrent.futures import ThreadPoolExecutor

    s = _store()
    pid = s.create_pending("delete_task", "irreversible", {"task_id": "t"})
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: s.approve(pid), range(64)))
    assert sum(1 for r in results if r) == 1  # the CAS lets exactly one win


def test_chat_park_ttl_expiry_blocks_approval(monkeypatch) -> None:
    from smartbrain_3000 import approvals as appmod

    monkeypatch.setattr(appmod, "_CHAT_TTL_SECONDS", -1)  # every chat park is already expired
    s = _store()
    pid = s.create_pending("add_task", "reviewed", {"title": "x"}, conversation_id="c1")
    assert s.get(pid)["expired"] is True
    assert s.approve(pid) is False and s.claim(pid) is False


def test_scheduled_park_outlives_the_chat_ttl(monkeypatch) -> None:
    # The bug found live: scheduled parks (conversation_id=None) died on the
    # 1-hour chat clock while the user was away. They live on the 30-day clock.
    from smartbrain_3000 import approvals as appmod

    monkeypatch.setattr(appmod, "_CHAT_TTL_SECONDS", -1)  # chat clock already expired
    s = _store()
    pid = s.create_pending("web_fetch", "reviewed", {"url": "https://example.com"})
    assert s.get(pid)["expired"] is False  # scheduled: still approvable
    assert s.approve(pid) is True and s.claim(pid) is True


def test_scheduled_park_expires_on_its_own_clock(monkeypatch) -> None:
    from smartbrain_3000 import approvals as appmod

    monkeypatch.setattr(appmod, "_SCHEDULED_TTL_SECONDS", -1)
    s = _store()
    pid = s.create_pending("web_fetch", "reviewed", {"url": "https://example.com"})
    assert s.get(pid)["expired"] is True
    assert s.approve(pid) is False


def test_list_pending_sweeps_expired_rows(monkeypatch) -> None:
    # The list and the Approve button must agree: a row past its TTL flips to
    # status 'expired' and stops being offered — no more dead Approve buttons
    # that 409 on every click.
    from smartbrain_3000 import approvals as appmod

    s = _store()
    stale = s.create_pending("web_fetch", "reviewed", {"url": "https://old.example"})
    fresh_chat = s.create_pending("add_task", "reviewed", {"title": "x"}, conversation_id="c1")
    monkeypatch.setattr(appmod, "_SCHEDULED_TTL_SECONDS", -1)  # scheduled rows now stale
    listed = s.list_pending()
    assert [p["id"] for p in listed] == [fresh_chat]  # the stale one dropped off
    assert s.get(stale)["status"] == "expired"  # swept, not deleted — audit trail intact
    assert s.approve(stale) is False


def test_cross_session_invisible() -> None:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    a = ApprovalStore(conn, key, "sessA")
    pid = a.create_pending("add_task", "reviewed", {"title": "x"})
    b = ApprovalStore(conn, key, "sessB")  # a later unlock
    assert b.get(pid) is None and b.list_pending() == []


def test_store_result_no_op_unless_executed() -> None:
    s = _store()
    pid = s.create_pending("add_task", "reviewed", {"title": "x"})
    s.store_result(pid, {"ok": True})  # pending status -> no-op
    assert s.get(pid).get("result") is None
    assert s.approve(pid) and s.claim(pid)  # advance to executed
    s.store_result(pid, {"ok": True, "first": True})
    assert s.get(pid)["result"] == {"ok": True, "first": True}
    s.store_result(pid, {"ok": True, "second": True})  # double-call must not overwrite
    assert s.get(pid)["result"] == {"ok": True, "first": True}


def test_pending_encrypted_and_wrong_key() -> None:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    a = ApprovalStore(conn, gen_master_key(), "s")
    pid = a.create_pending("add_task", "reviewed", {"title": "secret-title"})
    raw = b"".join(bytes(r[0]) for r in conn.execute("SELECT ciphertext FROM pending_actions;").fetchall())
    assert b"secret-title" not in raw
    with pytest.raises(Exception):
        ApprovalStore(conn, gen_master_key(), "s").get(pid)


# --- executor claim gate --------------------------------------------------

def test_executor_reviewed_requires_claim() -> None:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    audit = AuditLog(conn, gen_master_key())
    ctx = tools.ToolContext()
    with pytest.raises(AssertionError):
        tools.run(ctx, audit, "remember_fact", {"text": "x"}, actor="user")  # no claim
    with pytest.raises(PermissionError):
        tools.run(ctx, audit, "remember_fact", {"text": "x"}, actor="user", claim=lambda: False)


# --- HTTP state machine ---------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        test_client.post("/api/account/setup", json={"passphrase": "correct-horse"})
        yield test_client


def test_agent_routes_require_unlock(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "locked.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as c:
        assert c.get("/api/agent/pending").status_code == 423
        assert c.post("/api/agent/pending/x/approve", json={}).status_code == 423
        assert c.post("/api/agent/pending/x/deny").status_code == 423


def test_reviewed_tool_parks_not_executes(client: TestClient) -> None:
    r = client.post("/api/tools/invoke", json={"name": "remember_fact", "args": {"text": "I like tea"}})
    assert r.json()["status"] == "awaiting_approval"
    assert client.get("/api/memories").json()["memories"] == []  # not executed
    pend = client.get("/api/agent/pending").json()["pending"]
    assert len(pend) == 1 and pend[0]["tool"] == "remember_fact"
    assert any(e["decision"] == "proposed" for e in client.get("/api/audit").json()["entries"])


def test_approve_executes_once(client: TestClient) -> None:
    pid = client.post("/api/tools/invoke", json={"name": "remember_fact", "args": {"text": "I like tea"}}).json()["pending_id"]
    r = client.post(f"/api/agent/pending/{pid}/approve", json={})
    assert r.status_code == 200 and r.json()["status"] == "executed"
    assert client.get("/api/memories").json()["memories"][0]["text"] == "I like tea"
    decisions = {e["decision"] for e in client.get("/api/audit").json()["entries"]}
    assert {"proposed", "approved", "executed"} <= decisions
    assert client.post(f"/api/agent/pending/{pid}/approve", json={}).status_code == 409  # already executed


def test_deny_no_execution(client: TestClient) -> None:
    pid = client.post("/api/tools/invoke", json={"name": "remember_fact", "args": {"text": "x"}}).json()["pending_id"]
    assert client.post(f"/api/agent/pending/{pid}/deny").json() == {"ok": True}
    assert client.get("/api/memories").json()["memories"] == []
    assert any(e["decision"] == "denied" for e in client.get("/api/audit").json()["entries"])
    assert client.post(f"/api/agent/pending/{pid}/approve", json={}).status_code == 409


def test_irreversible_requires_confirm(client: TestClient) -> None:
    tid = client.post("/api/tasks", json={"title": "doomed"}).json()["id"]
    pid = client.post("/api/tools/invoke", json={"name": "delete_task", "args": {"task_id": tid}}).json()["pending_id"]
    assert client.post(f"/api/agent/pending/{pid}/approve", json={}).status_code == 409  # no confirm
    assert len(client.get("/api/tasks").json()["tasks"]) == 1  # still there
    r = client.post(f"/api/agent/pending/{pid}/approve", json={"confirm_tool": "delete_task"})
    assert r.status_code == 200
    assert client.get("/api/tasks").json()["tasks"] == []  # now deleted


def test_pending_rejected_after_relock(client: TestClient) -> None:
    pid = client.post("/api/tools/invoke", json={"name": "remember_fact", "args": {"text": "x"}}).json()["pending_id"]
    client.post("/api/account/lock")
    client.post("/api/account/unlock", json={"passphrase": "correct-horse"})  # new session
    assert client.post(f"/api/agent/pending/{pid}/approve", json={}).status_code == 404  # cross-session


def test_pending_says_whether_always_allow_would_stick(client: TestClient) -> None:
    """The UI hides "Always allow" on this flag, so it must match what consent does.

    Regression: consent began refusing model-addressed egress while the button was
    still offered for every REVIEWED tool, so clicking it approved once and then kept
    asking — indistinguishable from a broken button.
    """
    pid = client.post("/api/tools/invoke",
                      json={"name": "remember_fact", "args": {"text": "tea"}}).json()["pending_id"]
    pend = {p["id"]: p for p in client.get("/api/agent/pending").json()["pending"]}
    assert pend[pid]["rememberable"] is True
    client.post(f"/api/agent/pending/{pid}/deny")

    # Every pending action must carry the flag, and it must agree with consent.
    from smartbrain_3000 import consent
    for name, expected in (("web_fetch", False), ("kb_ingest_url", False),
                           ("web_search", True), ("remember_fact", True)):
        assert consent.is_rememberable(name) is expected, name


def test_always_allow_actually_stops_asking(client: TestClient) -> None:
    """End to end: the button's promise holds for a tool that advertises it."""
    pid = client.post("/api/tools/invoke",
                      json={"name": "remember_fact", "args": {"text": "one"}}).json()["pending_id"]
    assert client.post(f"/api/agent/pending/{pid}/approve", json={"remember": True}).status_code == 200
    assert "remember_fact" in client.get("/api/agent/remembered").json()["tools"]
