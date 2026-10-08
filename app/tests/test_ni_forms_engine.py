"""Engine-run matrix (R19 phase 1a-2): every shape the Library-answers path makes,
through the REAL flow build and the REAL engine.

For each of value answers, a dated list, a records list and columns: build a card
through ``ni_flow.build_from_answers`` + ``_handoff`` + ``_finalize`` (a fake model
that answers PRESENT with a valid DesignChoice, and the model-off rules floor),
then run ``ni.run_item`` with a fake fetcher returning the same sample. The item
reaches ``live``, the bound payload is a form node with two lint-clean CLIRs, the
hash is deterministic across two binds of one run's outputs, ``spec.history`` is
declared for the measure, ``display.size`` follows the sealed span.

No network, no model (the PRESENT fake parses the menu it is shown), no clock
(``ni._clock`` is frozen so the window transforms cut the same rows every run).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import duckdb
import pytest

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import ni as nimod
from smartbrain_3000 import ni_flow
from smartbrain_3000.ni_forms.form_scene import display_size_for_span, history_track_for
from smartbrain_3000.scheduler import ScheduleStore
from smartbrain_3000.secrets import SecretStore, gen_master_key

_NOW = datetime(2026, 10, 6, 18, 21, tzinfo=UTC)
_URL = "https://api.example.org/x"


# -------- fixtures: each shape, as a Library source declares it + a recorded-style sample -------
def _value_answers() -> tuple[list, dict, str, str]:
    chosen = [{"kind": "value", "name": "price", "label": "Price", "type": "number", "path": "quote.price",
               "unit": "$", "words": ["price"], "primary": True},
              {"kind": "value", "name": "change", "label": "Change", "type": "number", "path": "quote.change",
               "unit": "$", "words": ["change"], "primary": False},
              {"kind": "value", "name": "as_of", "label": "As of", "type": "time", "path": "quote.t",
               "words": ["as of"], "primary": False, "window": "latest", "utc": True}]
    sample = {"quote": {"price": 223.86, "change": -1.65, "t": "2026-10-06T18:21:00"}}
    return chosen, sample, "NVDA stock price", "price of NVDA"


def _dated_list_answers() -> tuple[list, dict, str, str]:
    chosen = [{"kind": "list", "name": "tides", "label": "Tides", "path": "predictions", "words": ["tides"],
               "primary": True, "axis": {"cell": "t", "step": "hour"},
               "cells": [{"path": "t", "type": "time", "label": "Time", "utc": True},
                         {"path": "v", "type": "number", "label": "Height", "unit": "ft"},
                         {"path": "type", "type": "text", "label": "Tide"}]}]
    rows = []
    for i, (h, kind) in enumerate(((5.5, "H"), (0.8, "L"), (6.0, "H"), (0.5, "L"), (5.7, "H"), (0.7, "L"))):
        at = _NOW + timedelta(hours=1 + 6 * i)
        rows.append({"t": at.strftime("%Y-%m-%d %H:%M"), "v": str(h), "type": kind})
    return chosen, {"predictions": rows}, "Charleston tides", "tides in Charleston today"


def _records_list_answers() -> tuple[list, dict, str, str]:
    chosen = [{"kind": "list", "name": "storms", "label": "Active storms", "path": "activeStorms",
               "words": ["storms"], "primary": True,
               "cells": [{"path": "name", "type": "text", "label": "Name"},
                         {"path": "intensity", "type": "number", "label": "Wind", "unit": "kt"},
                         {"path": "classification", "type": "text", "label": "Status"}]}]
    sample = {"activeStorms": [{"name": "Fay", "intensity": "40", "classification": "TS"},
                               {"name": "Odalys", "intensity": "45", "classification": "TS"},
                               {"name": "Polo", "intensity": "75", "classification": "HU"}]}
    return chosen, sample, "Active storms", "active storms right now"


def _columns_answers() -> tuple[list, dict, str, str]:
    chosen = [{"kind": "columns", "name": "hourly", "label": "Hourly forecast", "words": ["hourly"],
               "primary": False, "axis": {"cell": "hourly.time", "step": "hour"},
               "cells": [{"path": "hourly.time", "type": "time", "label": "Time"},
                         {"path": "hourly.temperature_2m", "type": "number", "label": "Temperature",
                          "unit": "°F"}]}]
    start = _NOW.replace(minute=0)
    sample = {"timezone": "UTC",
              "hourly": {"time": [(start + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(24)],
                         "temperature_2m": [60 + h * 0.5 for h in range(24)]}}
    return chosen, sample, "Hourly temperature", "hourly temperature for the next day"


_MATRIX = {
    "value": (_value_answers, {"stat", "conditions", "kv_grid"}, None),
    "dated_list": (_dated_list_answers, {"agenda", "day_table", "event_curve", "next_event"}, "upcoming"),
    "records_list": (_records_list_answers, {"entity_list", "ranked_list", "table"}, None),
    "columns": (_columns_answers, {"series_line"}, "next_hours:24"),
}


# -------- the fake model: a valid DesignChoice read off the PRESENT menu it is shown ----------
def _design_choice(prompt: str) -> str:
    """PRESENT's reply schema is enums only; the fake picks the first option on the
    menu and labels every field ``key`` — a valid choice, never a word on screen."""
    assert isinstance(prompt, str), "prompt required"
    ids = re.findall(r'"id": "(c\d)"', prompt)
    fields: list[str] = []
    # the system prompt NAMES the fence; the sample rows sit inside the last fenced block
    for block in re.findall(r"<untrusted_data>(\[.*?\])</untrusted_data>", prompt, re.DOTALL):
        try:
            rows = json.loads(block)
        except ValueError:
            continue
        fields = list(rows[0].keys()) if rows else []
    if not ids:
        return "{}"
    return json.dumps({"intent": "now", "fits": [{"cand": i, "answers_ask": "yes"} for i in ids],
                       "pick": ids[0], "second": None, "primary_field": None,
                       "labels": {f: "key" for f in fields[:6]}, "uncovered_wants": [],
                       "none_fits": False})


class _GW:
    """Gateway stand-in for run_item: no llm stage runs on these cards."""

    class GatewayError(Exception):
        status_code = 500

    def load_routes(self, _conn) -> dict:
        return {"ni": "mlx/local"}

    def resolve_model(self, capability: str, routes: dict) -> str | None:
        return routes.get(capability)

    def is_local(self, _model: str) -> bool:
        return True

    def local_available(self) -> bool:
        return True

    def chat(self, _messages, _model, **_kw) -> dict:
        return {"choices": [{"message": {"content": "{}"}}]}

    def completion_text(self, data: dict) -> str:
        return data["choices"][0]["message"]["content"]


def _store():
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    return nimod.NIStore(conn, key), conn, key


def _build(store, shape: str, call_model) -> tuple[str, dict]:
    """The REAL path: build_from_answers → _handoff → _finalize on a shell item."""
    make, _forms, window = _MATRIX[shape]
    chosen, sample, title, ask = make()
    item_id = ni_flow.create_shell_item(store, ask)
    intent = {"kind": "external_data", "subject": title, "cadence_minutes": 15, "wants": [],
              "threshold": None, "display_hint": "value", "window": window}
    ni_flow._transition(store, item_id, "intent", intent=intent)
    fb = ni_flow.FormBuild(now=_NOW, ask=ask, source_url=_URL, cadence_s=900, call_model=call_model)
    built = ni_flow.build_from_answers(chosen, sample, title, window=window, next_event=shape == "dated_list",
                                       frame_kind="schedule" if shape == "dated_list" else None, form=fb)
    result = ni_flow._handoff(store, item_id, ask, intent, _URL, built, built["fields"], built["klass"],
                              converted=[], judge=None, degrade_note=None, remap=False,
                              keep_source=None, keep_params=None, fetch_now=_NOW, path="declared")
    assert result["state"] == "ready", result
    return item_id, sample


def _run(store, conn, key, item_id: str) -> dict:
    return nimod.run_item(store, item_id, gateway_mod=_GW(), secrets_store=SecretStore(conn, key),
                          schedules_store=ScheduleStore(conn, key))


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    monkeypatch.setattr(nimod, "_clock", lambda: _NOW)
    monkeypatch.setattr(ni_flow, "start_flow_worker", lambda *a, **kw: True)


@pytest.mark.parametrize("shape", sorted(_MATRIX))
@pytest.mark.parametrize("designer", ["model", "rules"])
def test_engine_matrix_builds_runs_and_goes_live(monkeypatch, shape: str, designer: str) -> None:
    store, conn, key = _store()
    item_id, sample = _build(store, shape, _design_choice if designer == "model" else None)
    item = store.get_item(item_id)
    spec, scene = item["spec"], item["spec"]["scene"]
    assert item["state"] == "commissioning"
    assert scene["type"] == "form" and scene["form"] in _MATRIX[shape][1], scene["form"]
    assert scene["design"]["designer"] in ("model", "rules")
    assert spec["display"]["size"] == display_size_for_span(scene["spans"]["desktop"])
    track = history_track_for(scene["record"]["fields"], scene["record"]["rows"])
    assert spec.get("history") == track
    if shape == "value":
        assert track == {"track": {"price_h": "price"}, "max_points": 200}
    else:
        assert track is None
    # the preview the flow wrote is the bound form (what the client paints)
    preview = store.read_snapshot(item_id, "preview")["payload"]
    assert preview["type"] == "form" and set(preview["clir"]) == {"desktop", "phone"}
    assert preview["lint"]["red"] == 0
    # C2 YES, then the engine: C1 captures the contract, the next clean run goes live
    store.record_validation(item_id, True)
    monkeypatch.setattr(nimod, "_fetch_http_json", lambda source, item_id, secrets: sample)
    assert _run(store, conn, key, item_id)["status"] == "ok"
    assert store.get_item(item_id)["spec"]["contract"] is not None
    assert _run(store, conn, key, item_id)["status"] == "ok"
    assert store.get_item(item_id)["state"] == "live"
    latest = store.read_snapshot(item_id, "latest")["payload"]
    assert latest["type"] == "form" and latest["form"] == scene["form"]
    assert latest["lint"]["red"] == 0 and "design_needs_attention" not in latest
    for side in ("desktop", "phone"):
        clir = latest["clir"][side]
        assert clir["form"] == scene["form"] and clir["prims"]
        assert all(p["src"] in ("data", "lexicon", "ask", "title", "key", "code")
                   for p in clir["prims"] if p["k"] == "text")
    if shape == "value":   # the sparkline's history accrues from the card's own runs
        points = store.read_snapshot(item_id, "history")["payload"]["price_h"]
        assert [p["v"] for p in points] == [223.86, 223.86]


def test_engine_matrix_hash_is_deterministic_for_one_runs_outputs() -> None:
    """Two binds of one run's outputs at one fetch instant hash the same (the monitor-
    hash / cache key); the run-to-run hash moves only when the data or the history does."""
    store, _conn, _key = _store()
    item_id, sample = _build(store, "records_list", None)
    spec = store.get_item(item_id)["spec"]
    outputs = nimod.run_pipeline(spec["pipeline"], sample)
    ctx = nimod._form_bind_context(spec, _NOW)
    first = nimod.bind_scene(spec["scene"], outputs, history={}, form_ctx=ctx)
    second = nimod.bind_scene(spec["scene"], outputs, history={}, form_ctx=ctx)
    assert first["hash"] == second["hash"]
    assert first["clir"]["desktop"] == second["clir"]["desktop"]


def test_engine_matrix_model_pick_is_recorded_when_a_menu_exists() -> None:
    """The records shape yields a menu (entity_list / table / ranked_list): the fake
    PRESENT reply is a valid DesignChoice, so the seal says ``designer: model`` and the
    picked candidate is the one it named; nothing it said reaches the CLIR."""
    chosen, sample, title, ask = _records_list_answers()
    seen: list[str] = []

    def spy(prompt: str) -> str:
        seen.append(prompt)
        return _design_choice(prompt)

    fb = ni_flow.FormBuild(now=_NOW, ask=ask, source_url=_URL, cadence_s=900, call_model=spy)
    built = ni_flow.build_from_answers(chosen, sample, title, form=fb)
    node = built["scene"]
    assert seen and "Options" in seen[0]
    assert node["design"]["designer"] == "model"
    rules = ni_flow.build_from_answers(chosen, sample, title,
                                       form=ni_flow.FormBuild(now=_NOW, ask=ask, source_url=_URL,
                                                              cadence_s=900)).get("scene")
    assert rules["design"]["designer"] == "rules"
    assert rules["record"] == node["record"]   # the record never depends on the designer


def test_engine_matrix_a_list_that_shrinks_on_refresh_binds_its_designed_count_state() -> None:
    """Sealed over 8 rows; a refresh with 1 row (or none) binds the designed one / empty state
    with no red lint — the bind judges the count state exactly as the build's forced-state
    check did, so nothing the server ships carries red lint."""
    chosen, _sample, title, ask = _records_list_answers()
    rows = [{"name": f"Storm {i}", "intensity": str(30 + 5 * i), "classification": "TS" if i % 2 else "HU"}
            for i in range(8)]
    fb = ni_flow.FormBuild(now=_NOW, ask=ask, source_url=_URL, cadence_s=900)
    built = ni_flow.build_from_answers(chosen, {"activeStorms": rows}, title, form=fb)
    scene = built["scene"]
    spec = ni_flow.build_final_spec(ask, {"subject": title, "cadence_minutes": 15}, {"type": "http_json", "url": _URL},
                                    15, built["pipeline"], scene)
    ctx = nimod._form_bind_context(spec, _NOW)
    full = nimod.bind_scene(scene, built["preview_payload"], history={}, form_ctx=ctx)
    assert full["lint"]["red"] == 0
    one = nimod.bind_scene(scene, nimod.run_pipeline(spec["pipeline"], {"activeStorms": rows[:1]}),
                           history={}, form_ctx=ctx)
    assert one["lint"]["red"] == 0 and one["clir"]["desktop"]["state"] == "one", one["clir"]["desktop"]["state"]
    none = nimod.bind_scene(scene, {"rows": []}, history={}, form_ctx=ctx)
    assert none["lint"]["red"] == 0 and none["clir"]["desktop"]["state"] == "empty"


def test_display_size_derivation() -> None:
    """§34 display size: 1×1 → small, 2×1 → wide, 2×2 → large; phone → small."""
    assert display_size_for_span("d1x1") == "small"
    assert display_size_for_span("d2x1") == "wide"
    assert display_size_for_span("d1x2") == "tall"
    assert display_size_for_span("d2x2") == "large"
    assert display_size_for_span("d4x3") == "large"
    assert display_size_for_span("p2x1") == "small"


def test_history_track_absent_for_list_shape() -> None:
    """Rows-shaped records do not seed a measure history track."""
    fields = [{"name": "time", "label": "Time", "path": "t", "type": "datetime", "role": "time"},
              {"name": "height", "label": "Height", "path": "v", "type": "quantity", "role": "value"}]
    assert history_track_for(fields, "rows") is None
    assert history_track_for([{"name": "price", "label": "Price", "path": "price", "type": "currency",
                               "role": "measure"}], None) == {"track": {"price_h": "price"}, "max_points": 200}


# ============================================================================================
# R19 Phase 1b: run_item's `outputs` snapshot + `next_clock`, and tick()'s clock pass.

def _fake_app(conn, key: bytes):
    """Minimal app.state shim for tick() (mirrors test_ni.py's)."""
    return SimpleNamespace(state=SimpleNamespace(master_key=key, db=SimpleNamespace(cursor=conn.cursor)))


def _form_choice(form_name: str):
    """A fake PRESENT reply that picks the FIRST menu option named `form_name` when the
    menu offers one, else the first option — a valid DesignChoice either way, so the
    model-forcing itself never shows up on screen."""

    def pick(prompt: str) -> str:
        ids = re.findall(r'"id": "(c\d)"', prompt)
        forms = re.findall(r'"form": "(\w+)"', prompt)
        fields: list[str] = []
        for block in re.findall(r"<untrusted_data>(\[.*?\])</untrusted_data>", prompt, re.DOTALL):
            try:
                rows = json.loads(block)
            except ValueError:
                continue
            fields = list(rows[0].keys()) if rows else []
        if not ids:
            return "{}"
        want = next((i for i, f in zip(ids, forms, strict=False) if f == form_name), ids[0])
        return json.dumps({"intent": "now", "fits": [{"cand": i, "answers_ask": "yes"} for i in ids],
                           "pick": want, "second": None, "primary_field": None,
                           "labels": {f: "key" for f in fields[:6]}, "uncovered_wants": [],
                           "none_fits": False})

    return pick


def _day_table_answers() -> tuple[list, dict, str, str]:
    """A date-axis list spanning yesterday..+3 days, relative to REAL wall-clock `now`
    (never the module's frozen `_NOW`): the clock-pass test needs its rows' calendar
    days to agree with whatever instant `run_item`'s own `datetime.now(UTC)` fetches at."""
    base = datetime.now(UTC).date()
    chosen = [{"kind": "list", "name": "days", "label": "Days", "path": "days", "words": ["days"],
               "primary": True, "axis": {"cell": "d", "step": "day"},
               "cells": [{"path": "d", "type": "date", "label": "Date"},
                         {"path": "name", "type": "text", "label": "Event"}]}]
    days = [base + timedelta(days=i) for i in range(-1, 4)]
    rows = [{"d": d.isoformat(), "name": f"Event {i}"} for i, d in enumerate(days)]
    return chosen, {"days": rows}, "Daily events", "daily events this week"


def _build_day_table_item(store, call_model):
    """``build_from_answers`` -> ``_handoff`` on a REAL wall-clock `now` (see
    ``_day_table_answers``) -- a local twin of ``_build`` for a shape that is not in
    the shared ``_MATRIX`` (its rows must track real time, not the module's `_NOW`)."""
    chosen, sample, title, ask = _day_table_answers()
    real_now = datetime.now(UTC)
    item_id = ni_flow.create_shell_item(store, ask)
    intent = {"kind": "external_data", "subject": title, "cadence_minutes": 15, "wants": [],
              "threshold": None, "display_hint": "value", "window": None}
    ni_flow._transition(store, item_id, "intent", intent=intent)
    fb = ni_flow.FormBuild(now=real_now, ask=ask, source_url=_URL, cadence_s=900, call_model=call_model)
    built = ni_flow.build_from_answers(chosen, sample, title, window=None, next_event=False,
                                       frame_kind="schedule", form=fb)
    result = ni_flow._handoff(store, item_id, ask, intent, _URL, built, built["fields"], built["klass"],
                              converted=[], judge=None, degrade_note=None, remap=False,
                              keep_source=None, keep_params=None, fetch_now=real_now, path="declared")
    assert result["state"] == "ready", result
    return item_id, sample


def test_run_item_writes_outputs_snapshot_and_sets_next_clock(monkeypatch) -> None:
    """R19 Phase 1b step 4: every successful run seals the exact outputs the bind
    consumed beside `latest`, and sets `next_clock` from the bound node's `clock.next`."""
    store, conn, key = _store()
    item_id, sample = _build(store, "records_list", None)
    store.record_validation(item_id, True)
    monkeypatch.setattr(nimod, "_fetch_http_json", lambda source, item_id, secrets: sample)
    assert _run(store, conn, key, item_id)["status"] == "ok"
    assert _run(store, conn, key, item_id)["status"] == "ok"
    outputs_snap = store.read_snapshot(item_id, "outputs")
    assert outputs_snap is not None and outputs_snap["ok"]
    recomputed = nimod.run_pipeline(store.get_item(item_id)["spec"]["pipeline"], sample)
    assert outputs_snap["payload"] == recomputed
    # a plain records list carries no time field anywhere; the item's own stale
    # threshold (fetched_at + 2x cadence) is still a real future instant.
    assert store.get_item(item_id)["next_clock"] is not None


def test_clock_pass_skipped_and_next_clock_cleared_without_outputs_snapshot() -> None:
    """A card that never actually ran (no `outputs` snapshot -- e.g. one last run before
    this change shipped) declines the clock pass instead of raising, and clears
    `next_clock` so it stops being clock-due until a real run sets it again."""
    store, conn, key = _store()
    item_id, _sample = _build(store, "value", None)
    store.mark_checked(item_id, "ok")  # last_checked = now(): NOT fetch-due
    store.set_next_clock(item_id, datetime.now(UTC) - timedelta(seconds=1))  # clock-due
    result = nimod.tick(_fake_app(conn, key))
    assert result["checked"] == 1
    assert result["alerts"] == [] and result["broken"] == []
    assert store.get_item(item_id)["next_clock"] is None


def _clock_pass_fixture(monkeypatch):
    """A live day_table card after two real runs, with its clock frozen a day ahead and a
    past next_clock, ready for tick() to run one clock pass."""
    store, conn, key = _store()
    item_id, sample = _build_day_table_item(store, _form_choice("day_table"))
    store.record_validation(item_id, True)
    monkeypatch.setattr(nimod, "_fetch_http_json", lambda source, item_id, secrets: sample)
    assert _run(store, conn, key, item_id)["status"] == "ok"
    assert _run(store, conn, key, item_id)["status"] == "ok"
    tomorrow_0005 = datetime.combine(datetime.now(UTC).date() + timedelta(days=1),
                                     datetime.min.time(), tzinfo=UTC) + timedelta(minutes=5)
    monkeypatch.setattr(nimod, "_clock", lambda: tomorrow_0005)
    store.set_next_clock(item_id, datetime.now(UTC) - timedelta(seconds=1))
    return store, conn, key, item_id, tomorrow_0005


def test_clock_pass_keeps_the_snapshot_instant_the_board_reads_as_payload_at(monkeypatch) -> None:
    """Lead review 2026-10-08: the board's payload_at / as-of / stale clock is the `latest`
    slot's created_at. A clock pass re-lays out the SAME data, so that instant must not
    move — on the first pass, nor on a second one (which must still read the fetch
    instant, not the first pass's write time)."""
    store, conn, key, item_id, tomorrow_0005 = _clock_pass_fixture(monkeypatch)
    fetch_instant = store.read_snapshot(item_id, "latest")["created_at"]
    good_instant = store.read_snapshot(item_id, "last_good")["created_at"]   # its own write, ms later
    assert nimod.tick(_fake_app(conn, key))["checked"] == 1
    assert store.read_snapshot(item_id, "latest")["created_at"] == fetch_instant
    assert store.read_snapshot(item_id, "last_good")["created_at"] == good_instant
    # a second clock pass, another day on: still the fetch instant
    day_after = tomorrow_0005 + timedelta(days=1)
    monkeypatch.setattr(nimod, "_clock", lambda: day_after)
    store.set_next_clock(item_id, datetime.now(UTC) - timedelta(seconds=1))
    assert nimod.tick(_fake_app(conn, key))["checked"] == 1
    assert store.read_snapshot(item_id, "latest")["created_at"] == fetch_instant
    assert store.get_item(item_id)["next_clock"] > datetime.now(UTC)


def test_clock_pass_never_overwrites_a_failure_marker(monkeypatch) -> None:
    """Lead review 2026-10-08: a failing card keeps its G3 `latest` marker (ok=False, {});
    the clock pass re-lays out only `last_good` — the slot the board shows for it."""
    store, conn, key, item_id, _ = _clock_pass_fixture(monkeypatch)
    before_good = store.read_snapshot(item_id, "last_good")
    store.write_snapshot(item_id, "latest", {}, ok=False)   # what _handle_failure writes (G3)
    assert nimod.tick(_fake_app(conn, key))["checked"] == 1
    latest = store.read_snapshot(item_id, "latest")
    assert latest["ok"] is False and latest["payload"] == {}
    after_good = store.read_snapshot(item_id, "last_good")
    assert after_good["payload"]["hash"] != before_good["payload"]["hash"]   # re-laid out
    assert after_good["created_at"] == before_good["created_at"]            # same data instant


def test_an_outputs_snapshot_failure_never_fails_the_run(monkeypatch) -> None:
    """Lead review 2026-10-08: the outputs slot exists only for the optional clock pass. When
    sealing it raises (an outputs tree past json_instants' node ceiling), the run is still
    ok, an ok=False marker replaces any earlier outputs (a later clock pass must never re-lay
    out STALE outputs under a fresh clock), and next_clock is cleared."""
    store, conn, key = _store()
    item_id, sample = _build(store, "records_list", None)
    store.record_validation(item_id, True)
    monkeypatch.setattr(nimod, "_fetch_http_json", lambda source, item_id, secrets: sample)
    assert _run(store, conn, key, item_id)["status"] == "ok"
    assert store.read_snapshot(item_id, "outputs")["ok"]

    def boom(payload):
        raise ValueError("too many nodes")
    monkeypatch.setattr(nimod, "json_instants", boom)
    assert _run(store, conn, key, item_id)["status"] == "ok"
    marker = store.read_snapshot(item_id, "outputs")
    assert marker["ok"] is False and marker["payload"] == {}
    assert store.get_item(item_id)["next_clock"] is None
    assert store.read_snapshot(item_id, "latest")["ok"]


def test_clock_pass_through_tick_relayouts_with_no_fetch_no_model(monkeypatch) -> None:
    """R19 Phase 1b end to end: build -> _finalize -> run_item (x2, a fetch stub) -> live;
    freeze the app clock a day past the card-zone midnight; run tick() -> the latest
    snapshot's CLIR changes (the Today label moves to the new day) with no fetch and no
    model call, last_checked / alerts are untouched, and next_clock advances. A second
    tick before the new boundary does nothing more to this item."""
    store, conn, key = _store()
    item_id, sample = _build_day_table_item(store, _form_choice("day_table"))
    scene = store.get_item(item_id)["spec"]["scene"]
    assert scene["form"] == "day_table", scene["form"]
    store.record_validation(item_id, True)
    monkeypatch.setattr(nimod, "_fetch_http_json", lambda source, item_id, secrets: sample)
    assert _run(store, conn, key, item_id)["status"] == "ok"
    assert _run(store, conn, key, item_id)["status"] == "ok"

    item = store.get_item(item_id)
    assert item["state"] == "live"
    before_checked = item["last_checked"]
    before_latest = store.read_snapshot(item_id, "latest")["payload"]
    before_texts = [ln for p in before_latest["clir"]["desktop"]["prims"] if p["k"] == "text" for ln in p["lines"]]
    assert "Today" in before_texts

    # a day has passed, card-tz midnight included (card_tz is UTC: no `zone` output here)
    tomorrow_0005 = datetime.combine(datetime.now(UTC).date() + timedelta(days=1),
                                     datetime.min.time(), tzinfo=UTC) + timedelta(minutes=5)
    monkeypatch.setattr(nimod, "_clock", lambda: tomorrow_0005)
    forced_past = datetime.now(UTC) - timedelta(seconds=1)
    store.set_next_clock(item_id, forced_past)

    result = nimod.tick(_fake_app(conn, key))
    assert result["checked"] == 1
    assert result["alerts"] == [] and result["broken"] == [] and result["repaired"] == []

    after_item = store.get_item(item_id)
    assert after_item["last_checked"] == before_checked         # never touched by a clock pass
    assert after_item["last_status"] == item["last_status"]
    assert after_item["consecutive_failures"] == 0
    assert after_item["next_clock"] is not None and after_item["next_clock"] > datetime.now(UTC)

    after_latest = store.read_snapshot(item_id, "latest")["payload"]
    after_texts = [ln for p in after_latest["clir"]["desktop"]["prims"] if p["k"] == "text" for ln in p["lines"]]
    assert after_texts != before_texts
    assert "Today" in after_texts
    assert after_latest["hash"] != before_latest["hash"]
    last_good = store.read_snapshot(item_id, "last_good")["payload"]
    assert last_good["hash"] == after_latest["hash"]

    # a second tick before the new boundary touches nothing more for this item
    again = nimod.tick(_fake_app(conn, key))
    assert again["checked"] == 0
    assert store.read_snapshot(item_id, "latest")["payload"]["hash"] == after_latest["hash"]
