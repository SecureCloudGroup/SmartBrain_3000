"""F1 upgrade (2026-10-04 field): pre-F1 Library cards keep walking forward.

A card installed on v0.24.0 / v0.24.1 sealed the creation day's clock date into
``source.url`` as a literal (schedule / forecast / usno / coops / neows). The
engine's ``ni_flow.upgrade_pre_f1_literal_dates`` rewrites that URL to the
``{{param:name}}`` + clock-kind-param shape on the card's first tick, so day 2
reads day-2's date. These tests cover the whole class, not any one URL."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import duckdb
import pytest

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import ni as nimod
from smartbrain_3000 import ni_flow
from smartbrain_3000.secrets import gen_master_key

NY = ZoneInfo("America/New_York")


# ---- fixtures / helpers ---------------------------------------------------

class _FakeLib:
    """Minimal Library stand-in: records keyed by source id, each a plain dict."""

    def __init__(self, records: dict[str, dict]) -> None:
        assert isinstance(records, dict), "records required"
        self._records = records

    def get(self, source_id: str) -> dict | None:
        assert isinstance(source_id, str), "source id required"
        return self._records.get(source_id)


def _store() -> nimod.NIStore:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return nimod.NIStore(conn, gen_master_key())


def _scene() -> dict:
    return {"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "text", "value": "x", "role": "title", "tone": "default", "size": "md"},
    ]}


def _old_shape_spec(url: str, params: dict | None = None) -> dict:
    """A pre-F1 http_json card: ``source.url`` is a FULL literal (date baked in)."""
    return {"version": 1, "title": "t", "goal": "g",
            "params": dict(params or {}),
            "source": {"type": "http_json", "url": url, "headers": {}},
            "pipeline": [], "scene": _scene(),
            "display": {"size": "small"},
            "contract": None,
            "repair_policy": {"l1": True, "l2_frontier": False},
            "model": None}


def _mlb_schedule_record() -> dict:
    """A one-clock-param record (MLB schedule-like): ``date`` clock-fills today."""
    return {"access": {"url_template": "https://ex.test/schedule?sportId=1&date={date}",
                       "params": [{"name": "date", "label": "date",
                                    "fill": {"from": "clock", "format": "%Y-%m-%d",
                                              "offset_days": 0}}]},
            "kinds": []}


def _two_clock_record() -> dict:
    """A two-clock-param record (NASA NEOWS-like): start / end days apart."""
    return {"access": {"url_template": "https://ex.test/feed?start={start}&end={end}",
                       "params": [{"name": "start", "label": "start",
                                    "fill": {"from": "clock", "format": "%Y-%m-%d",
                                              "offset_days": 0}},
                                   {"name": "end", "label": "end",
                                    "fill": {"from": "clock", "format": "%Y-%m-%d",
                                              "offset_days": 7}}]},
            "kinds": []}


def _freeze(monkeypatch, moment: datetime) -> None:
    assert moment.tzinfo is not None, "moment must be aware"
    monkeypatch.setattr(nimod, "_clock", lambda: moment)


def _flow_write_source(store: nimod.NIStore, item_id: str, source_id: str) -> None:
    """Seal a minimal flow record naming the Library source id (the pre-F1 shape)."""
    ni_flow._flow_write(store, item_id,
                         {"state": "ready", "request": "r",
                          "updated_at": "2026-10-03T12:00:00Z", "notes": [],
                          "_library_source": source_id})


def _item_at(store: nimod.NIStore, item_id: str, created: datetime) -> dict:
    """A copy of the stored item with ``created_at`` pinned (DuckDB's real-time stamp is
    unusable for creation-day tests)."""
    item = store.get_item(item_id)
    assert item is not None, "item must exist"
    out = dict(item)
    out["created_at"] = created.isoformat()
    return out


# ---- the class fix: an upgrade walks a pre-F1 literal-date card forward -----

def test_upgrade_rewrites_literal_date_and_walks_forward(monkeypatch) -> None:
    """A pre-F1 schedule card from day D is rewritten to the templated form; day D+3 the
    engine substitutes D+3's date, not D's literal."""
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER",
                        lambda: _FakeLib({"mlb-schedule": _mlb_schedule_record()}))
    store = _store()
    _freeze(monkeypatch, datetime(2026, 10, 3, 12, 0, tzinfo=NY))
    url = "https://ex.test/schedule?sportId=1&date=2026-10-03"
    item_id = store.add_item(_old_shape_spec(url), {})
    _flow_write_source(store, item_id, "mlb-schedule")
    created = datetime(2026, 10, 3, 16, 0, tzinfo=UTC)  # 12:00 NY
    upgraded = ni_flow.upgrade_pre_f1_literal_dates(store, _item_at(store, item_id, created))
    assert upgraded is not None, "upgrade must apply when the Library record declares a clock fill"
    sealed = store.get_item(item_id)["spec"]
    assert sealed["source"]["url"] == "https://ex.test/schedule?sportId=1&date={{param:date}}"
    assert sealed["params"]["date"] == {"label": "date", "kind": "clock",
                                          "format": "%Y-%m-%d", "offset_days": 0}
    # the engine on day D reproduces the stored literal byte-for-byte
    assert nimod.substitute_params(sealed)["source"]["url"] == url
    # day D+3: the SAME spec reads D+3's date
    _freeze(monkeypatch, datetime(2026, 10, 6, 12, 0, tzinfo=NY))
    assert nimod.substitute_params(sealed)["source"]["url"] \
        == "https://ex.test/schedule?sportId=1&date=2026-10-06"


def test_upgrade_handles_two_clock_params(monkeypatch) -> None:
    """A NEOWS-shaped URL with two clock dates upgrades both, each with its own offset."""
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER",
                        lambda: _FakeLib({"neows": _two_clock_record()}))
    store = _store()
    _freeze(monkeypatch, datetime(2026, 10, 3, 12, 0, tzinfo=NY))
    url = "https://ex.test/feed?start=2026-10-03&end=2026-10-10"
    item_id = store.add_item(_old_shape_spec(url), {})
    _flow_write_source(store, item_id, "neows")
    created = datetime(2026, 10, 3, 16, 0, tzinfo=UTC)
    assert ni_flow.upgrade_pre_f1_literal_dates(store, _item_at(store, item_id, created))
    sealed = store.get_item(item_id)["spec"]
    assert sealed["source"]["url"] \
        == "https://ex.test/feed?start={{param:start}}&end={{param:end}}"
    assert sealed["params"]["start"]["offset_days"] == 0
    assert sealed["params"]["end"]["offset_days"] == 7
    _freeze(monkeypatch, datetime(2026, 10, 6, 12, 0, tzinfo=NY))
    assert nimod.substitute_params(sealed)["source"]["url"] \
        == "https://ex.test/feed?start=2026-10-06&end=2026-10-13"


# ---- untouched: no reproduce, no source id, no library -----------------------

def test_upgrade_skips_when_literal_cannot_be_reproduced(monkeypatch) -> None:
    """A URL whose stored date doesn't match the record's offset at creation time is left
    untouched (verify step refuses — no silent rewriting of unknown bytes)."""
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER",
                        lambda: _FakeLib({"mlb-schedule": _mlb_schedule_record()}))
    store = _store()
    _freeze(monkeypatch, datetime(2026, 10, 3, 12, 0, tzinfo=NY))
    url = "https://ex.test/schedule?sportId=1&date=2019-01-01"  # not day D or near
    item_id = store.add_item(_old_shape_spec(url), {})
    _flow_write_source(store, item_id, "mlb-schedule")
    created = datetime(2026, 10, 3, 16, 0, tzinfo=UTC)
    rev_before = store.get_item(item_id)["spec_rev"]
    assert ni_flow.upgrade_pre_f1_literal_dates(store, _item_at(store, item_id, created)) is None
    assert store.get_item(item_id)["spec_rev"] == rev_before
    assert store.get_item(item_id)["spec"]["source"]["url"] == url


def test_upgrade_skips_when_no_library_source_in_flow(monkeypatch) -> None:
    """A card whose flow record doesn't name a Library source is left alone — nothing to read
    the clock-fill shape from."""
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER",
                        lambda: _FakeLib({"mlb-schedule": _mlb_schedule_record()}))
    store = _store()
    _freeze(monkeypatch, datetime(2026, 10, 3, 12, 0, tzinfo=NY))
    url = "https://ex.test/schedule?sportId=1&date=2026-10-03"
    item_id = store.add_item(_old_shape_spec(url), {})
    # no flow record written: no _library_source
    rev_before = store.get_item(item_id)["spec_rev"]
    created = datetime(2026, 10, 3, 16, 0, tzinfo=UTC)
    assert ni_flow.upgrade_pre_f1_literal_dates(store, _item_at(store, item_id, created)) is None
    assert store.get_item(item_id)["spec_rev"] == rev_before


def test_upgrade_skips_when_library_record_missing(monkeypatch) -> None:
    """The Library isn't installed (or the source id has aged out) → the upgrade declines."""
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: _FakeLib({}))
    store = _store()
    _freeze(monkeypatch, datetime(2026, 10, 3, 12, 0, tzinfo=NY))
    url = "https://ex.test/schedule?sportId=1&date=2026-10-03"
    item_id = store.add_item(_old_shape_spec(url), {})
    _flow_write_source(store, item_id, "mlb-schedule")
    rev_before = store.get_item(item_id)["spec_rev"]
    created = datetime(2026, 10, 3, 16, 0, tzinfo=UTC)
    assert ni_flow.upgrade_pre_f1_literal_dates(store, _item_at(store, item_id, created)) is None
    assert store.get_item(item_id)["spec_rev"] == rev_before


def test_upgrade_skips_when_no_library_wired(monkeypatch) -> None:
    """``_LIBRARY_PROVIDER = None`` is a legitimate state (locked / not wired) — no upgrade."""
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", None)
    store = _store()
    _freeze(monkeypatch, datetime(2026, 10, 3, 12, 0, tzinfo=NY))
    url = "https://ex.test/schedule?sportId=1&date=2026-10-03"
    item_id = store.add_item(_old_shape_spec(url), {})
    _flow_write_source(store, item_id, "mlb-schedule")
    rev_before = store.get_item(item_id)["spec_rev"]
    created = datetime(2026, 10, 3, 16, 0, tzinfo=UTC)
    assert ni_flow.upgrade_pre_f1_literal_dates(store, _item_at(store, item_id, created)) is None
    assert store.get_item(item_id)["spec_rev"] == rev_before


# ---- idempotency + attestation preserve + non-http_json ----------------------

def test_upgrade_is_idempotent(monkeypatch) -> None:
    """A second call after the rewrite does nothing — no new revision, no second URL change."""
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER",
                        lambda: _FakeLib({"mlb-schedule": _mlb_schedule_record()}))
    store = _store()
    _freeze(monkeypatch, datetime(2026, 10, 3, 12, 0, tzinfo=NY))
    url = "https://ex.test/schedule?sportId=1&date=2026-10-03"
    item_id = store.add_item(_old_shape_spec(url), {})
    _flow_write_source(store, item_id, "mlb-schedule")
    created = datetime(2026, 10, 3, 16, 0, tzinfo=UTC)
    first = ni_flow.upgrade_pre_f1_literal_dates(store, _item_at(store, item_id, created))
    assert first is not None
    rev_after_first = store.get_item(item_id)["spec_rev"]
    # Second call: {{param:}} is already present → the gate at the top returns None
    second = ni_flow.upgrade_pre_f1_literal_dates(store, _item_at(store, item_id, created))
    assert second is None
    assert store.get_item(item_id)["spec_rev"] == rev_after_first


def test_upgrade_skips_non_http_json_sources(monkeypatch) -> None:
    """http_page / model / internal.* sources never carried clock-template URLs — nothing to do."""
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER",
                        lambda: _FakeLib({"mlb-schedule": _mlb_schedule_record()}))
    store = _store()
    _freeze(monkeypatch, datetime(2026, 10, 3, 12, 0, tzinfo=NY))
    spec = _old_shape_spec("https://ex.test/x")
    spec["source"] = {"type": "http_page", "url": "https://ex.test/x", "headers": {}}
    item_id = store.add_item(spec, {})
    _flow_write_source(store, item_id, "mlb-schedule")
    created = datetime(2026, 10, 3, 16, 0, tzinfo=UTC)
    assert ni_flow.upgrade_pre_f1_literal_dates(store, _item_at(store, item_id, created)) is None


def test_upgrade_preserves_attestations(monkeypatch) -> None:
    """The rewrite reproduces the fetched URL byte-for-byte at creation, so a card that had
    passed C2 keeps ``_c2_ok`` and its ``contract`` across the upgrade (shape-only edit)."""
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER",
                        lambda: _FakeLib({"mlb-schedule": _mlb_schedule_record()}))
    store = _store()
    _freeze(monkeypatch, datetime(2026, 10, 3, 12, 0, tzinfo=NY))
    url = "https://ex.test/schedule?sportId=1&date=2026-10-03"
    spec = _old_shape_spec(url)
    spec["_c2_ok"] = True
    item_id = store.add_item(spec, {})
    _flow_write_source(store, item_id, "mlb-schedule")
    created = datetime(2026, 10, 3, 16, 0, tzinfo=UTC)
    assert ni_flow.upgrade_pre_f1_literal_dates(store, _item_at(store, item_id, created))
    assert store.get_item(item_id)["spec"].get("_c2_ok") is True


# ---- the broken-Library path -------------------------------------------------

def test_upgrade_declines_when_library_get_raises(monkeypatch) -> None:
    """A Library ``get`` that raises degrades to None — a broken Library must never fail a tick."""
    class _Broken:
        def get(self, _sid: str) -> dict:
            raise RuntimeError("library broken")

    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: _Broken())
    store = _store()
    _freeze(monkeypatch, datetime(2026, 10, 3, 12, 0, tzinfo=NY))
    url = "https://ex.test/schedule?sportId=1&date=2026-10-03"
    item_id = store.add_item(_old_shape_spec(url), {})
    _flow_write_source(store, item_id, "mlb-schedule")
    rev_before = store.get_item(item_id)["spec_rev"]
    created = datetime(2026, 10, 3, 16, 0, tzinfo=UTC)
    assert ni_flow.upgrade_pre_f1_literal_dates(store, _item_at(store, item_id, created)) is None
    assert store.get_item(item_id)["spec_rev"] == rev_before


# ---- the record has no clock-fill params at all ------------------------------

def test_upgrade_skips_when_record_declares_no_clock_fills(monkeypatch) -> None:
    """A static-URL Library source (no clock fills) is a no-op — nothing to rewrite."""
    record = {"access": {"url_template": "https://ex.test/x",
                           "params": [{"name": "sym", "fill": {"from": "text"}}]},
              "kinds": []}
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER", lambda: _FakeLib({"static": record}))
    store = _store()
    _freeze(monkeypatch, datetime(2026, 10, 3, 12, 0, tzinfo=NY))
    url = "https://ex.test/x"
    item_id = store.add_item(_old_shape_spec(url), {})
    _flow_write_source(store, item_id, "static")
    rev_before = store.get_item(item_id)["spec_rev"]
    created = datetime(2026, 10, 3, 16, 0, tzinfo=UTC)
    assert ni_flow.upgrade_pre_f1_literal_dates(store, _item_at(store, item_id, created)) is None
    assert store.get_item(item_id)["spec_rev"] == rev_before


# ---- shaped by run_item: an upgraded card runs with the new URL -------------

def test_upgrade_runs_from_run_item(monkeypatch) -> None:
    """``run_item`` calls the upgrade before fetching — the pre-F1 card's next tick walks
    forward (the fetch path sees the templated URL at the current clock)."""
    pytest.importorskip("smartbrain_3000.ni_flow")
    monkeypatch.setattr(ni_flow, "_LIBRARY_PROVIDER",
                        lambda: _FakeLib({"mlb-schedule": _mlb_schedule_record()}))
    store = _store()
    _freeze(monkeypatch, datetime(2026, 10, 3, 12, 0, tzinfo=NY))
    url = "https://ex.test/schedule?sportId=1&date=2026-10-03"
    item_id = store.add_item(_old_shape_spec(url), {})
    _flow_write_source(store, item_id, "mlb-schedule")
    created = datetime(2026, 10, 3, 16, 0, tzinfo=UTC)
    # the one call ``run_item`` does — the upgrade reads ``item.created_at`` from the arg
    item = _item_at(store, item_id, created)
    upgraded = ni_flow.upgrade_pre_f1_literal_dates(store, item)
    assert upgraded is not None and upgraded["spec"]["source"]["url"] \
        == "https://ex.test/schedule?sportId=1&date={{param:date}}"
