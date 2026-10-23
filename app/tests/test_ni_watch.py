"""NI oversight plane v0 (G1) — W-CREATE and the findings store."""

from __future__ import annotations

from collections.abc import Iterator

import duckdb
import pytest

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import ni, ni_flow, ni_watch
from smartbrain_3000.secrets import gen_master_key


@pytest.fixture()
def store() -> Iterator[ni.NIStore]:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    yield ni.NIStore(conn, gen_master_key())
    conn.close()


# --- findings store ----------------------------------------------------------

def test_file_finding_dedupes_open_twins(store: ni.NIStore) -> None:
    conn = store.conn
    first = ni_watch.file_finding(conn, "create", "warn", "same title")
    assert first is not None
    assert ni_watch.file_finding(conn, "create", "warn", "same title") is None, \
        "an identical open finding must not double-file (edge trigger)"
    assert len(ni_watch.list_findings(conn)) == 1
    # Resolving reopens the edge: the same title may file again later.
    assert ni_watch.resolve_finding(conn, first) is True
    assert ni_watch.file_finding(conn, "create", "warn", "same title") is not None


def test_findings_scoped_by_item_dedupe(store: ni.NIStore) -> None:
    conn = store.conn
    assert ni_watch.file_finding(conn, "create", "high", "t", item_id="a")
    assert ni_watch.file_finding(conn, "create", "high", "t", item_id="b"), \
        "different items are different findings"
    assert ni_watch.file_finding(conn, "create", "high", "t", item_id="a") is None


def test_resolve_missing_finding_returns_false(store: ni.NIStore) -> None:
    assert ni_watch.resolve_finding(store.conn, "nope") is False


def test_severity_is_validated(store: ni.NIStore) -> None:
    with pytest.raises(AssertionError):
        ni_watch.file_finding(store.conn, "create", "critical", "t")


# --- W-CREATE pass -------------------------------------------------------------

def test_watch_create_clusters_repeated_failures(store: ni.NIStore) -> None:
    for n in range(3):
        iid = ni_flow.create_shell_item(store, f"cluster case {n}")
        ni_flow._fail(store, iid, "mapping", "mapping stage failed")
    summary = ni_watch.watch_create(store)
    assert summary["filed"] >= 1
    titles = [f["title"] for f in ni_watch.list_findings(store.conn)]
    assert any("mapping" in t and "class break" in t for t in titles), titles
    # Second pass: the open finding dedupes — no spam while the cluster holds.
    again = ni_watch.watch_create(store)
    assert again["filed"] == 0


def test_watch_create_ignores_small_clusters_and_declines(store: ni.NIStore) -> None:
    iid = ni_flow.create_shell_item(store, "one failure only")
    ni_flow._fail(store, iid, "fetch", "down")
    iid2 = ni_flow.create_shell_item(store, "declined by user")
    ni_flow._fail(store, iid2, "declined", "user declined the source")
    summary = ni_watch.watch_create(store)
    titles = [f["title"] for f in ni_watch.list_findings(store.conn)]
    assert not any("class break" in t for t in titles), titles
    assert summary["high"] == 0


def test_watch_create_absorbs_the_stale_sweep(store: ni.NIStore, monkeypatch) -> None:
    monkeypatch.setattr(ni_flow, "sweep_stranded_flows", lambda s: 2)
    summary = ni_watch.watch_create(store)
    assert summary["swept"] == 2
    titles = [f["title"] for f in ni_watch.list_findings(store.conn)]
    assert any("stalled" in t for t in titles), titles


def test_watch_create_survives_a_poisoned_record(store: ni.NIStore, monkeypatch) -> None:
    """Feeds-contract isolation: one unreadable record never stops the pass."""
    good = ni_flow.create_shell_item(store, "healthy terminal")
    ni_flow._fail(store, good, "fetch", "down")
    real_read = ni_flow._flow_read

    def poisoned(s, item_id):
        if item_id == good:
            return real_read(s, item_id)
        raise RuntimeError("sealed slot corrupt")

    ni_flow.create_shell_item(store, "poisoned record")
    monkeypatch.setattr(ni_flow, "_flow_read", poisoned)
    summary = ni_watch.watch_create(store)  # must not raise
    assert isinstance(summary, dict)


def test_watch_create_dead_end_check_fires_on_what_the_board_returns(
        store: ni.NIStore, monkeypatch) -> None:
    iid = ni_flow.create_shell_item(store, "dead end case")
    ni_flow._fail(store, iid, "mapping", "mapping stage failed")
    # A normal terminal card offers a reason and a way out: the check stays quiet.
    quiet = ni_watch.watch_create(store)
    assert quiet["high"] == 0
    assert not [f for f in ni_watch.list_findings(store.conn) if f["severity"] == "high"]
    # The board surface loses its way out: the check must now fire.
    monkeypatch.setattr(ni_flow, "board_flow_field",
                        lambda _store, _iid: {"state": "failed", "reason": "it failed"})
    fired = ni_watch.watch_create(store)
    assert fired["high"] == 1 and fired["filed"] >= 1
    titles = [f["title"] for f in ni_watch.list_findings(store.conn)]
    assert any("offers no reason or way out" in t for t in titles), titles


def test_the_audited_row_is_the_row_the_board_serves(store: ni.NIStore) -> None:
    """The audit reads ``ni_routes._board_row`` itself, so a field the route adds (or drops) is seen by the checks."""
    from smartbrain_3000 import ni_routes

    iid = ni_flow.create_shell_item(store, "parity case")
    ni_flow._fail(store, iid, "mapping", "mapping stage failed")
    item = store.get_item(iid)
    assert ni_watch.served_row(store, item) == ni_routes._board_row(store, item)
    assert "actions" in ni_watch.served_row(store, item)


def test_watch_create_flags_a_served_row_with_no_actions(store: ni.NIStore, monkeypatch) -> None:
    from smartbrain_3000 import ni_routes

    ni_flow.create_shell_item(store, "no actions case")
    real = ni_routes._board_row

    def stripped(s, item, *a, **k):
        row = real(s, item, *a, **k)
        row["actions"] = []
        return row

    monkeypatch.setattr(ni_routes, "_board_row", stripped)
    fired = ni_watch.watch_create(store)
    assert fired["high"] >= 1
    assert any("no actions" in f["title"] for f in ni_watch.list_findings(store.conn))
