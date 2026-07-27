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


# --- Phase 6: go-live gating + live directives -------------------------------

def _seed_cohort(conn, key, sid: str, *, turns: int, bad: int, since_event=None) -> None:
    """Seed matched event+metric pairs for a strategy's cohort (same conversation)."""
    import uuid as _uuid
    for i in range(turns):
        cid = f"conv-{sid[:6]}-{i}"
        conn.execute(
            "INSERT INTO optimizer_events (id, conversation_id, request_type, strategy_id) "
            "VALUES (?, ?, 'multi_step', ?);", [_uuid.uuid4().hex, cid, sid])
        metrics.record_turn(conn, model="ollama/x", is_local=True, duration_ms=900,
                            conversation_id=cid, hit_max_steps=i < bad,
                            outcome="max_steps" if i < bad else "complete")


def _shadow(conn, key, fired: int = 10) -> str:
    sid = optimizer.StrategyStore(conn, key).add(
        "multi_step", "Outline the plan before executing multi-step tasks.")
    conn.execute("UPDATE optimizer_strategies SET fired = ? WHERE id = ?;", [fired, sid])
    return sid


def test_cohort_metrics_joins_events_to_turns() -> None:
    conn, key = _conn(), gen_master_key()
    sid = _shadow(conn, key)
    _seed_cohort(conn, key, sid, turns=8, bad=4)
    cohort = optimizer.cohort_metrics(conn, sid)
    assert cohort == {"turns": 8, "bad": 4, "bad_rate": 0.5}


def test_promotion_needs_relevance_and_persistent_problem() -> None:
    conn, key = _conn(), gen_master_key()
    store = optimizer.StrategyStore(conn, key)
    # Healthy cohort: relevant but the problem resolved itself -> stays shadow.
    sid = _shadow(conn, key)
    _seed_cohort(conn, key, sid, turns=10, bad=1)
    assert optimizer.promote_and_judge(conn, key) == []
    assert store.list()[0]["status"] == "shadow"
    # Bad cohort -> promoted, baseline recorded, announced.
    conn.execute("DELETE FROM optimizer_events;")
    conn.execute("DELETE FROM turn_metrics;")
    _seed_cohort(conn, key, sid, turns=10, bad=5)
    lines = optimizer.promote_and_judge(conn, key)
    assert len(lines) == 1 and "activated guidance for multi_step" in lines[0]
    row = store.list()[0]
    assert row["status"] == "active" and row["baseline_bad"] == 0.5
    assert row["activated_at"] is not None and row["evaluated_at"] is None
    assert optimizer.open_trial(conn) is True


def test_promotion_blocked_while_other_trial_open() -> None:
    conn, key = _conn(), gen_master_key()
    sid = _shadow(conn, key)
    _seed_cohort(conn, key, sid, turns=10, bad=5)
    assert optimizer.promote_and_judge(conn, key, other_trial_open=True) == []
    assert optimizer.StrategyStore(conn, key).list()[0]["status"] == "shadow"


def test_trial_kept_on_improvement_disabled_on_no_help_or_regression() -> None:
    conn, key = _conn(), gen_master_key()
    store = optimizer.StrategyStore(conn, key)
    # KEPT: bad-rate halves after activation.
    sid = _shadow(conn, key)
    _seed_cohort(conn, key, sid, turns=10, bad=5)
    optimizer.promote_and_judge(conn, key)
    _seed_cohort(conn, key, sid, turns=8, bad=1)  # post-activation cohort: 12.5% bad
    assert optimizer.promote_and_judge(conn, key) == []  # kept quietly
    row = store.list()[0]
    assert row["status"] == "active" and row["evaluated_at"] is not None
    assert optimizer.open_trial(conn) is False
    # DISABLED (didn't help): fresh strategy, post-activation rate stays at baseline.
    conn.execute("DELETE FROM optimizer_events;")
    conn.execute("DELETE FROM turn_metrics;")
    conn.execute("DELETE FROM optimizer_strategies;")
    sid2 = _shadow(conn, key)
    _seed_cohort(conn, key, sid2, turns=10, bad=5)
    optimizer.promote_and_judge(conn, key)
    _seed_cohort(conn, key, sid2, turns=8, bad=4)
    lines = optimizer.promote_and_judge(conn, key)
    assert len(lines) == 1 and "didn't help" in lines[0]
    assert store.list()[0]["status"] == "disabled"
    # DISABLED early (made things worse): regression clears at 4 turns.
    conn.execute("DELETE FROM optimizer_events;")
    conn.execute("DELETE FROM turn_metrics;")
    conn.execute("DELETE FROM optimizer_strategies;")
    sid3 = _shadow(conn, key)
    _seed_cohort(conn, key, sid3, turns=10, bad=3)  # baseline 30%
    optimizer.promote_and_judge(conn, key)
    _seed_cohort(conn, key, sid3, turns=4, bad=3)  # 75% bad after
    lines = optimizer.promote_and_judge(conn, key)
    assert len(lines) == 1 and "made them worse" in lines[0]
    assert store.list()[0]["status"] == "disabled"


def test_apply_directive_only_for_active_matching_type() -> None:
    conn, key = _conn(), gen_master_key()
    optimizer.set_enabled(conn, True)
    sid = _shadow(conn, key)
    ask = _ask("first gather the data, then summarize it, then email me the result")
    assert optimizer.apply_directive(conn, key, ask) is None  # shadow: never live
    optimizer.StrategyStore(conn, key).set_status(sid, "active")
    hit = optimizer.apply_directive(conn, key, ask)
    assert hit is not None and hit["request_type"] == "multi_step"
    assert hit["note"]["role"] == "system"
    assert "Outline the plan" in hit["note"]["content"]
    assert optimizer.apply_directive(conn, key, _ask("what is the capital of France")) is None
    assert optimizer.apply_directive(conn, None, ask) is None  # locked -> baseline
    optimizer.set_enabled(conn, False)
    assert optimizer.apply_directive(conn, key, ask) is None  # kill-switch -> baseline


def test_review_gate_announces_activation(monkeypatch) -> None:
    conn, key = _conn(), gen_master_key()
    selfreview.set_enabled(conn, True)
    optimizer.set_enabled(conn, True)
    sid = _shadow(conn, key)
    _seed_cohort(conn, key, sid, turns=10, bad=5)
    monkeypatch.setattr(selfreview, "_critique", lambda *a, **k: [])
    notified: list = []
    out = selfreview.run_review(conn, key, notify=lambda s, m: notified.append(m))
    assert out is not None
    assert "activated guidance for multi_step" in notified[0]  # a behavior change: announced
    assert optimizer.StrategyStore(conn, key).list()[0]["status"] == "active"


def test_memory_apply_blocked_while_strategy_trial_open(monkeypatch) -> None:
    conn, key = _conn(), gen_master_key()
    sid = _shadow(conn, key)
    optimizer.StrategyStore(conn, key).set_status(sid, "active")  # open trial (unevaluated)
    finding = {"category": "preference", "component": "chat",
               "description": "wants brevity", "payload": "Prefers concise answers always.",
               "confidence": 0.9}
    assert selfreview._maybe_apply(conn, key, [finding], {"turns": 10, "stops": 0,
                                                          "regenerations": 0}) is None
    from smartbrain_3000.memory import MemoryStore
    assert MemoryStore(conn, key).list_memories() == []  # one trial at a time, framework-wide
