"""Tests for per-turn speed/quality telemetry (self-improving framework, Phase 1)."""

from __future__ import annotations

import duckdb

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import metrics


def _conn() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return conn


def test_tables_exist_after_migration() -> None:
    conn = _conn()
    names = {r[0] for r in conn.execute(
        "SELECT table_name FROM information_schema.tables;").fetchall()}
    assert {"turn_metrics", "feedback_events"} <= names


def test_record_turn_and_summary() -> None:
    conn = _conn()
    metrics.record_turn(conn, model="ollama/x", is_local=True, duration_ms=100,
                        prompt_tokens=10, completion_tokens=5, steps=2, ttft_ms=40,
                        conversation_id="c1", outcome="complete")
    metrics.record_turn(conn, model="openai/gpt", is_local=False, duration_ms=300,
                        prompt_tokens=20, completion_tokens=0, steps=8,
                        degraded=True, hit_max_steps=True, outcome="max_steps")
    s = metrics.summary(conn)
    assert s["turns"] == 2
    assert s["degraded"] == 1
    assert s["hit_max_steps"] == 1
    assert s["total_tokens"] == 35
    assert s["local_turns"] == 1
    assert s["median_ms"] == 200.0  # median of {100, 300}


def test_record_feedback_kinds_and_counts() -> None:
    conn = _conn()
    metrics.record_feedback(conn, kind="stop", conversation_id="c1")
    metrics.record_feedback(conn, kind="regenerate")
    metrics.record_feedback(conn, kind="bogus")  # unknown kind -> ignored
    s = metrics.summary(conn)
    assert s["stops"] == 1 and s["regenerations"] == 1
    assert conn.execute("SELECT COUNT(*) FROM feedback_events;").fetchone()[0] == 2


def test_best_effort_never_raises() -> None:
    # No conn / empty model / bad kind must be silent no-ops — telemetry never fails a turn.
    metrics.record_turn(None, model="x", is_local=False, duration_ms=1)
    metrics.record_turn(_conn(), model="", is_local=False, duration_ms=1)
    metrics.record_feedback(None, kind="stop")
    metrics.record_feedback(_conn(), kind="nope")


def test_token_tally_accumulates_and_forwards() -> None:
    seen: list[tuple] = []
    tally = metrics._TokenTally(lambda m, r: seen.append((m, r)))
    tally("ollama/x", {"usage": {"prompt_tokens": 3, "completion_tokens": 7}})
    tally("ollama/x", {"usage": {"prompt_tokens": 1, "completion_tokens": 2}})
    tally("ollama/x", {"no": "usage"})  # no usage block -> not counted, still forwarded
    assert tally.prompt_tokens == 4 and tally.completion_tokens == 9
    assert len(seen) == 3  # every call reaches the real spend sink


def test_empty_summary_is_zeros() -> None:
    s = metrics.summary(_conn())
    assert s["turns"] == 0 and s["median_ms"] == 0.0 and s["p90_ms"] == 0.0
    assert s["total_tokens"] == 0 and s["stops"] == 0 and s["regenerations"] == 0
