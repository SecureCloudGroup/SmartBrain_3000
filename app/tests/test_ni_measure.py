"""P4 (release plan, T 2026-10-24): the live measure. Every first verdict on a card is counted by how the card was
built (the Library, a source the user kept, research, a pasted link), and a kept card is checked seven days on:
still live, failing, paused or gone. Local only; the counts show on the Status page and export for the eval."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from test_ni_answers import lib  # noqa: F401  (a fixture)
from test_ni_intake import _researched
from test_ni_research_loop import (  # noqa: F401
    Script,
    _flow,
    _v2,  # an autouse fixture: research on, a local route
    _wrap,
    client,
)

from smartbrain_3000 import db, ni, ni_intake, ni_measure

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


def _item(item_id: str, path: str | None) -> dict:
    return {"id": item_id, "spec": {"_built_from": {"path": path, "host": "h", "title": ""}} if path else {}}


def _at(monkeypatch, moment: datetime) -> None:
    monkeypatch.setattr(ni, "_clock", lambda: moment)


def test_every_verdict_is_counted_by_how_the_card_was_built(lib, monkeypatch) -> None:  # noqa: F811
    store, _item_id, _ = _researched(monkeypatch, lib)
    _at(monkeypatch, NOW)
    for item_id, path, kept in (("a", "auto", True), ("b", "declared", False), ("c", "kept", True),
                                ("d", "research", True), ("e", "page", False), ("f", "mapping", True),
                                ("g", None, False), ("h", "made-up", True)):
        ni_measure.record(store, _item(item_id, path), kept=kept)
    view = ni_measure.view(store)
    by = view["by_source"]
    assert (by["library"]["kept"], by["library"]["dismissed"]) == (1, 1)
    assert by["own"]["kept"] == 1 and by["research"]["kept"] == 1
    assert (by["paste"]["kept"], by["paste"]["dismissed"]) == (1, 1)
    assert (by["other"]["kept"], by["other"]["dismissed"]) == (1, 1), "an unknown path is 'other', never trusted"
    assert view["total"]["kept"] == 5 and view["total"]["waiting"] == 5 and view["since"] == NOW.isoformat()
    assert [r["path"] for r in view["rows"]][-1] == "other"
    raw = db.meta_get(store.conn, ni_measure.META_KEY)
    assert '"h"' in raw and "host" not in raw and "title" not in raw, "a closed word and a random id, nothing else"


def test_a_kept_card_is_checked_seven_days_on(lib, monkeypatch) -> None:  # noqa: F811
    store, live, _ = _researched(monkeypatch, lib)
    _at(monkeypatch, NOW)
    ni_measure.record(store, store.get_item(live), kept=True)
    for item_id in ("gone-1",):
        ni_measure.record(store, _item(item_id, "research"), kept=True)
    ni_measure.record(store, _item("said-no", "auto"), kept=False)
    store.set_state(live, "live")
    _at(monkeypatch, NOW + timedelta(days=6, hours=23))
    assert ni_measure.check_due(store) == 0, "not before day 7"
    _at(monkeypatch, NOW + timedelta(days=7))
    assert ni_measure.check_due(store) == 2
    assert ni_measure.check_due(store) == 0, "stamped once"
    rows = {r["item"]: r["day7"] for r in ni_measure.view(store)["rows"]}
    assert rows == {live: "live", "gone-1": "gone", "said-no": None}
    total = ni_measure.view(store)["total"]
    assert (total["live"], total["gone"], total["waiting"]) == (1, 1, 0)


def test_a_kept_card_that_stopped_working_or_was_paused_says_so(lib, monkeypatch) -> None:  # noqa: F811
    store, failing, _ = _researched(monkeypatch, lib)
    store, paused, _ = _researched(monkeypatch, lib, store=store)
    _at(monkeypatch, NOW)
    for item_id in (failing, paused):
        ni_measure.record(store, store.get_item(item_id), kept=True)
    store.set_state(failing, "failing")
    store.set_state(paused, "paused")
    _at(monkeypatch, NOW + timedelta(days=8))
    assert ni_measure.check_due(store) == 2
    assert {r["item"]: r["day7"] for r in ni_measure.view(store)["rows"]} == {failing: "failing", paused: "paused"}


def test_the_measure_is_bounded_and_reads_past_a_damaged_row(lib, monkeypatch) -> None:  # noqa: F811
    store, _item_id, _ = _researched(monkeypatch, lib)
    _at(monkeypatch, NOW)
    good = {"item": "x", "path": "auto", "verdict": "kept", "at": NOW.isoformat(), "day7": None}
    db.meta_set(store.conn, ni_measure.META_KEY, json.dumps([good, {"item": 1}, {**good, "at": "soon"}, "junk"]))
    assert [r["item"] for r in ni_measure.view(store)["rows"]] == ["x"]
    db.meta_set(store.conn, ni_measure.META_KEY, "{not json")
    assert ni_measure.view(store)["rows"] == []
    for n in range(ni_measure.MAX_ROWS + 5):
        ni_measure.record(store, _item(f"i{n}", "auto"), kept=True)
    rows = ni_measure.view(store)["rows"]
    assert len(rows) == ni_measure.MAX_ROWS and rows[-1]["item"] == f"i{ni_measure.MAX_ROWS + 4}"
    _at(monkeypatch, NOW + timedelta(days=7))
    assert ni_measure.check_due(store) == ni_measure.MAX_CHECKS, "a pass stamps a bounded number"


# --- the taps that count, and the export --------------------------------------------------------------------------

def test_keep_no_and_deleting_a_held_card_are_counted(client, lib, monkeypatch) -> None:  # noqa: F811
    store = client.app.state.ni
    _store, kept, _ = _researched(monkeypatch, lib, store=store)
    _store, said_no, _ = _researched(monkeypatch, lib, store=store)
    _store, deleted, _ = _researched(monkeypatch, lib, store=store)
    assert client.post(f"/api/ni/items/{kept}/validate", json={"ok": True}).status_code == 200
    assert client.post(f"/api/ni/items/{said_no}/validate", json={"ok": False}).status_code == 200
    assert client.delete(f"/api/ni/items/{deleted}").status_code == 200
    got = client.get("/api/ni/measure").json()
    assert got["by_source"]["research"]["kept"] == 1 and got["by_source"]["research"]["dismissed"] == 2
    assert {r["item"]: r["verdict"] for r in got["rows"]} == {kept: "kept", said_no: "dismissed", deleted: "dismissed"}


def test_a_card_rebuilt_from_a_kept_source_says_so(lib, monkeypatch) -> None:  # noqa: F811
    """A reused kept source is its own way in (``kept``), so the measure can tell it from fresh research."""
    store, first, _ = _researched(monkeypatch, lib)
    assert ni_intake.remember(store, store.get_item(first))
    store, item_id, out, _net = _flow(monkeypatch, _wrap(lib), store=store, seam=Script(), cloud="anthropic/x",
                                      consented=True)
    item = store.get_item(item_id)
    assert out["state"] == "ready" and item["spec"]["_built_from"]["path"] == "kept" and ni.awaits_yes(item)
    assert ni_intake.remember(store, item), "keeping it again refreshes its entry"
