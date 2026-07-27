"""Tests for the Prompt Optimizer's shadow phase (self-improving framework, Phase 5)."""

from __future__ import annotations

import json

import duckdb

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import metrics, optimizer, selfreview
from smartbrain_3000.history import ChatHistory
from smartbrain_3000.secrets import gen_master_key


def _conn() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return conn


# --- classifier -------------------------------------------------------------

def test_classify_buckets() -> None:
    assert optimizer.classify("what is the capital of France") == "factual"
    assert optimizer.classify("how many pages does the S-1 have") == "factual"
    assert optimizer.classify("search my knowledge for the lease terms") == "retrieval"
    assert optimizer.classify("what does my Cantor document say about fees") == "retrieval"
    assert optimizer.classify("fix this:\n```python\ndef f(:\n```") == "code"
    assert optimizer.classify("I got a Traceback when running the installer") == "code"
    assert optimizer.classify("first draft the outline, then write each section, then review") == "multi_step"
    assert optimizer.classify("1. gather the data\n2. summarize it\n3. email me") == "multi_step"
    assert optimizer.classify("hello there") == "ambiguous"
    assert optimizer.classify("") == "ambiguous"
    # A long interrogative is a task in disguise, not a factual lookup:
    long_ask = "what would be the best way to " + "reorganize my entire document collection " * 3
    assert optimizer.classify(long_ask) == "ambiguous"


def test_kill_switch_fails_closed() -> None:
    conn = _conn()
    assert optimizer.enabled(conn) is False  # absent -> off
    dbmod.meta_set(conn, optimizer.ENABLED_META_KEY, "TRUE")  # not the exact literal -> off
    assert optimizer.enabled(conn) is False
    optimizer.set_enabled(conn, True)
    assert optimizer.enabled(conn) is True


# --- strategy store ---------------------------------------------------------

def test_strategy_roundtrip_caps_and_dedup() -> None:
    conn, key = _conn(), gen_master_key()
    store = optimizer.StrategyStore(conn, key)
    sid = store.add("multi_step", "Outline the steps before acting.", rationale="turns ran out of steps")
    assert sid is not None
    row = store.list()[0]
    assert row["status"] == "shadow" and row["fired"] == 0
    assert row["directive"] == "Outline the steps before acting."
    # The directive must not sit in any plaintext column:
    raw = conn.execute("SELECT * EXCLUDE (nonce, ciphertext) FROM optimizer_strategies;").fetchone()
    assert all("Outline" not in str(v) for v in raw)
    # Refusals: unknown type, junk, duplicate, per-type cap.
    assert store.add("nonsense", "Do better somehow please.") is None
    assert store.add("multi_step", "short") is None
    assert store.add("multi_step", "outline the steps BEFORE acting.") is None  # dup (case-insensitive)
    assert store.add("multi_step", "Confirm the goal before starting the steps.") is not None
    assert store.add("multi_step", "A third idea for the same bucket is too many.") is None  # per-type cap
    store.set_status(sid, "disabled")
    assert [r["status"] for r in store.list() if r["id"] == sid] == ["disabled"]


# --- shadow observation -----------------------------------------------------

def _ask(text: str) -> list[dict]:
    return [{"role": "system", "content": "sys"}, {"role": "user", "content": text}]


def test_observe_disabled_records_nothing() -> None:
    conn = _conn()
    optimizer.observe_turn(conn, _ask("what is the capital of France"), "c1")
    assert conn.execute("SELECT COUNT(*) FROM optimizer_events;").fetchone()[0] == 0


def test_observe_records_event_and_counts_would_have_fired() -> None:
    conn, key = _conn(), gen_master_key()
    optimizer.set_enabled(conn, True)
    optimizer.observe_turn(conn, _ask("what is the capital of France"), "c1")
    rows = conn.execute("SELECT request_type, strategy_id, conversation_id FROM optimizer_events;").fetchall()
    assert rows == [("factual", None, "c1")]  # classified; no strategy to match yet
    sid = optimizer.StrategyStore(conn, key).add("factual", "Answer directly, cite the source if known.")
    optimizer.observe_turn(conn, _ask("who wrote the Cantor letter"), "c1")
    fired = conn.execute("SELECT fired FROM optimizer_strategies WHERE id = ?;", [sid]).fetchone()[0]
    assert fired == 1  # the shadow strategy WOULD have fired on that turn
    match = conn.execute(
        "SELECT strategy_id FROM optimizer_events ORDER BY created_at DESC, id DESC LIMIT 1;"
    ).fetchone()[0]
    assert match == sid


def test_observe_never_raises_on_junk() -> None:
    conn = _conn()
    optimizer.set_enabled(conn, True)
    optimizer.observe_turn(conn, None, None)
    optimizer.observe_turn(conn, [{"bad": "shape"}, "not a dict"], None)
    optimizer.observe_turn(None, _ask("x"), None)
    assert conn.execute("SELECT COUNT(*) FROM optimizer_events;").fetchone()[0] == 0


# --- critique -> shadow strategy -------------------------------------------

_STRATEGY_FINDING = {"category": "prompt", "component": "chat", "request_type": "multi_step",
                     "description": "multi-step asks keep exhausting the step budget",
                     "payload": "Outline the plan before executing multi-step tasks.",
                     "confidence": 0.8}


def test_parse_findings_accepts_prompt_with_valid_type() -> None:
    good = selfreview._parse_findings(json.dumps([_STRATEGY_FINDING]))
    assert good == [_STRATEGY_FINDING]
    bad_type = dict(_STRATEGY_FINDING, request_type="jailbreak")
    assert selfreview._parse_findings(json.dumps([bad_type])) == []  # dropped, never coerced
    no_type = {k: v for k, v in _STRATEGY_FINDING.items() if k != "request_type"}
    assert selfreview._parse_findings(json.dumps([no_type])) == []


def test_run_review_learns_shadow_strategy(monkeypatch) -> None:
    conn, key = _conn(), gen_master_key()
    selfreview.set_enabled(conn, True)
    for _ in range(10):  # flagged window: every turn ran out of steps
        metrics.record_turn(conn, model="ollama/x", is_local=True, duration_ms=1000,
                            hit_max_steps=True, outcome="max_steps")
    cid = ChatHistory(conn, key).create_conversation("t")
    ChatHistory(conn, key).add_message(cid, "user", "do the whole month-end close")
    metrics.record_feedback(conn, kind="stop", conversation_id=cid)
    monkeypatch.setattr(selfreview, "_critique", lambda *a, **k: [dict(_STRATEGY_FINDING)])
    out = selfreview.run_review(conn, key)
    assert out is not None
    strategies = optimizer.StrategyStore(conn, key).list()
    assert len(strategies) == 1 and strategies[0]["status"] == "shadow"
    assert strategies[0]["request_type"] == "multi_step"
    # Shadow learning is invisible: no behavior change, so no memory fact and no
    # "What changed" digest line came from it (the flags-only digest is separate).
    from smartbrain_3000.memory import MemoryStore
    assert MemoryStore(conn, key).list_memories() == []


# --- HTTP API ---------------------------------------------------------------

def test_optimizer_api(tmp_path, monkeypatch) -> None:
    from fastapi.testclient import TestClient
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as client:
        assert client.get("/api/selfimprove/optimizer").status_code == 423  # locked-gated
        assert client.put("/api/selfimprove/optimizer", json={"enabled": True}).status_code == 423
        client.post("/api/account/setup", json={"passphrase": "correct-horse"})
        state = client.get("/api/selfimprove/optimizer").json()
        assert state == {"enabled": False, "strategies": []}  # default off, nothing learned
        assert client.put("/api/selfimprove/optimizer", json={"enabled": True}).json()["enabled"] is True
        app_state = client.app.state
        optimizer.StrategyStore(app_state.dbx, app_state.master_key).add(
            "factual", "Answer directly with the source named.")
        rows = client.get("/api/selfimprove/optimizer").json()["strategies"]
        assert len(rows) == 1 and rows[0]["status"] == "shadow" and rows[0]["fired"] == 0
        assert rows[0]["directive"].startswith("Answer directly")
