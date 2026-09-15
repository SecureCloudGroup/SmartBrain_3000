"""§29 NI Flow Engine — recorded-corpus tests + stage unit tests.

Every stage runs against fixtures in ``app/tests/fixtures/ni_flow/`` (the
recorded 10-source corpus); the two model calls are injected as pure Python
callables so the suite is model-free + deterministic on every PR. The live
matrix at ``tools/ni-flow-eval.py`` (owned by a parallel agent) exercises the
same code with real bifrost + real sources — this file is the fast path.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import duckdb
import pytest

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import ni as nimod
from smartbrain_3000 import ni_flow, tools
from smartbrain_3000.secrets import gen_master_key

FIXTURES = Path(__file__).parent / "fixtures" / "ni_flow"


def _load(name: str) -> object:
    """Read one recorded fixture (JSON body)."""
    assert (FIXTURES / f"{name}.json").exists(), f"missing fixture {name}"
    with (FIXTURES / f"{name}.json").open() as fp:
        return json.load(fp)


def _store() -> tuple[nimod.NIStore, duckdb.DuckDBPyConnection]:
    """Fresh in-memory NIStore for one test — bounded by pytest's process."""
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    return nimod.NIStore(conn, key), conn


def _scripted_model(replies: list[str]):
    """Return a `(model, prompt) -> reply` callable that pops from a scripted list."""
    calls: list[dict] = []

    def _call(model: str, prompt: str) -> str:
        assert isinstance(model, str) and isinstance(prompt, str), "args required"
        calls.append({"model": model, "prompt": prompt})
        assert replies, "scripted model exhausted"
        return replies.pop(0)

    _call.calls = calls  # type: ignore[attr-defined]
    return _call


# ---- helpers unit tests --------------------------------------------------

def test_downsample_trims_lists_to_two() -> None:
    """POC parity: every list is trimmed to the first N items (default 2)."""
    node = {"features": [{"a": i} for i in range(10)]}
    out = ni_flow.downsample(node)
    assert isinstance(out, dict), "out is dict"
    assert len(out["features"]) == 2 and out["features"][0]["a"] == 0


def test_downsample_bigger_list_keep_one() -> None:
    """The 30KB guard trims further when the first pass still overflows."""
    big = {"rows": [{"k": "x" * 2000} for _ in range(5)]}
    out = ni_flow.downsample(big, list_keep=1)
    assert isinstance(out, dict), "dict out"
    assert len(out["rows"]) == 1


def test_infer_fields_string_vs_number() -> None:
    """Wants like `title`/`place` become string; every other word defaults to number."""
    fields = ni_flow.infer_fields({"wants": ["temperature", "wind speed", "title"]})
    assert fields["title"] == "string"
    assert fields["temperature"] == "number"
    # snake_case slug
    assert any(k == "wind_speed" for k in fields), f"got {sorted(fields)}"


def test_infer_fields_caps_at_four() -> None:
    """More than 4 wants get truncated (cap is _MAX_INTENT_FIELDS)."""
    fields = ni_flow.infer_fields({"wants": ["a", "b", "c", "d", "e", "f"]})
    assert len(fields) == 4


def test_build_mapping_menu_filters_by_type() -> None:
    """The units-string case (kc_weather has `current_weather_units.temperature`=°C):
    when wants are numeric, no string paths appear in the menu — a guarantee that
    turns the POC's ``°C over 25.7`` failure into a build-time constraint."""
    sample = _load("kc_weather")
    cands = ni_flow.derive_paths(sample)
    assert cands, "walker returned candidates"
    usable, menu = ni_flow.build_mapping_menu(cands, {"temperature": "number"})
    # Menu offers only numeric paths — the °C UNITS string is NOT in it.
    assert usable, "at least one numeric candidate"
    for entry in usable:
        assert entry["type"] == "number", f"non-number leaked in: {entry}"
    assert "°C" not in menu and "°C" not in menu, "units string in menu"


def test_generalize_list_path_repeat_root() -> None:
    """`hits[0].title` → (`hits`, `item.title`)."""
    items_path, item_field = ni_flow._generalize_list_path("hits[0].title")
    assert items_path == "hits" and item_field == "item.title"


def test_generalize_list_path_rejects_non_exemplar() -> None:
    """A non-list exemplar path is rejected (would produce a broken repeat root)."""
    with pytest.raises(ValueError):
        ni_flow._generalize_list_path("current.temperature")


# ---- intent stage --------------------------------------------------------

def test_stage_intent_parses_closed_schema() -> None:
    """A well-formed reply is returned as-is."""
    reply = json.dumps({
        "kind": "external_data", "subject": "Bitcoin", "cadence_minutes": 15,
        "wants": ["price"], "threshold": None, "display_hint": "value",
    })
    model = _scripted_model([reply])
    intent = ni_flow.stage_intent("what's bitcoin worth", lambda p: model("m", p))
    assert intent["kind"] == "external_data" and intent["cadence_minutes"] == 15


def test_stage_intent_retries_once_on_malformed() -> None:
    """A garbled reply retries once; the second good reply is accepted."""
    good = json.dumps({
        "kind": "external_data", "subject": "x", "cadence_minutes": 15,
        "wants": ["price"], "threshold": None, "display_hint": "value",
    })
    replies = ["<think>oops</think>{ not json", good]
    model = _scripted_model(replies)
    intent = ni_flow.stage_intent("q", lambda p: model("m", p))
    assert intent["kind"] == "external_data"
    assert len(model.calls) == 2, "retry consumed the second reply"


def test_stage_intent_gives_up_after_retry() -> None:
    """Two malformed replies → a ValueError bubbles out of the stage."""
    model = _scripted_model(["garbage", "still garbage"])
    with pytest.raises(ValueError):
        ni_flow.stage_intent("q", lambda p: model("m", p))


# ---- recipe matching -----------------------------------------------------

def test_match_recipe_ticker_needs_category_corroboration() -> None:
    """C2 (audit 2026-09-13): a bare ticker no longer wins on its own.

    "show me AAPL every 5 minutes" carries no "finance" / "stock" / "price" /
    "quote" word — the ticker bump is category-gated, so nothing scores past
    the threshold and the flow pauses at ``source`` instead of silently
    matching fx-usd-eur (the audit's reproduction).
    """
    catalog = [
        {"id": "fx", "title": "USD to EUR exchange rate", "category": "finance"},
        {"id": "quakes", "title": "USGS earthquakes", "category": "misc"},
        {"id": "stock", "title": "Stock quote", "category": "finance"},
    ]
    # Bare ticker + no corroborating word: nothing wins.
    got = ni_flow.match_recipe(catalog, "show me AAPL every 5 minutes",
                                {"wants": ["price"]})
    assert got is None, f"bare ticker must NOT match; got {got}"


def test_match_recipe_ticker_matches_when_stock_word_appears() -> None:
    """C2 (audit 2026-09-13): a corroborated ticker matches the stock recipe."""
    catalog = [
        {"id": "fx", "title": "USD to EUR exchange rate", "category": "finance"},
        {"id": "stock", "title": "Stock quote", "category": "finance"},
    ]
    got = ni_flow.match_recipe(catalog, "show me AAPL stock every 5 minutes",
                                {"wants": ["price"]})
    assert got is not None and got["id"] == "stock", f"expected stock, got {got}"


def test_match_recipe_audit_reproductions_return_none() -> None:
    """C2 (audit 2026-09-13): the audit's stated wrong-match reproductions
    now return None (no false-positive fx / stock match, no ticker leak).
    """
    catalog = [
        {"id": "fx", "title": "USD to EUR exchange rate", "category": "finance"},
        {"id": "quakes", "title": "USGS earthquakes", "category": "misc"},
        {"id": "stock", "title": "Stock quote", "category": "finance"},
        {"id": "weather", "title": "Weather", "category": "weather"},
    ]
    for req in ("days until I retire",
                "track the S&P 500",
                "show me what I spent this month"):
        assert ni_flow.match_recipe(catalog, req, {"wants": ["value"]}) is None, \
            f"audit reproduction should match nothing: {req!r}"


def test_match_recipe_no_hit_returns_none() -> None:
    """Below the score minimum → None (the flow pauses at ``source``)."""
    catalog = [{"id": "x", "title": "totally unrelated", "category": "z"}]
    assert ni_flow.match_recipe(catalog, "some vague request", {"wants": ["q"]}) is None


# ---- mapping stage guarantees -------------------------------------------

def test_stage_mapping_verifies_membership_and_type() -> None:
    """A model that answers with a path not in the menu is refused after retry."""
    cands = [{"path": "current.temperature", "type": "number", "example": 25.1}]
    fields = {"temperature": "number"}
    good_reply = json.dumps({"temperature": "current.temperature"})
    # First reply picks a non-offered path; retry picks the offered one.
    model = _scripted_model([json.dumps({"temperature": "nope"}), good_reply])
    out = ni_flow.stage_mapping({"wants": ["temperature"]}, cands, fields,
                                 lambda p: model("m", p))
    assert out == {"temperature": "current.temperature"}


def test_stage_mapping_rejects_wrong_type() -> None:
    """Even after retry, a wrong-type pick fails cleanly."""
    cands = [{"path": "current.units", "type": "string", "example": "°C"}]
    fields = {"temperature": "number"}
    with pytest.raises(ValueError):
        # Zero numeric candidates in the menu — the stage refuses before any call.
        ni_flow.stage_mapping({"wants": ["temperature"]}, cands, fields,
                               lambda p: "unused")


# ---- end-to-end (recorded) -----------------------------------------------

def _empty_catalog() -> list[dict]:
    """An empty catalog forces the freeform path in end-to-end recorded cases."""
    return []


def test_flow_end_to_end_aapl_value_card() -> None:
    """AAPL fixture → shell → mapping → ready draft/commissioning with a working preview."""
    store, _conn = _store()
    fixture = _load("aapl")
    request = "show me AAPL every 5 minutes"
    item_id = ni_flow.create_shell_item(store, request)
    intent_reply = json.dumps({
        "kind": "external_data", "subject": "AAPL", "cadence_minutes": 5,
        "wants": ["price", "prev_close"], "threshold": None, "display_hint": "value",
    })
    # The walker returns candidates like ``chart.result[0].meta.regularMarketPrice``
    # (numeric) — pin the mapping reply to two known-good numeric paths from the
    # recorded response so the test is deterministic without a real model.
    mapping_reply = json.dumps({
        "price": "chart.result[0].meta.regularMarketPrice",
        "prev_close": "chart.result[0].meta.fulldayPrice",
    })
    model = _scripted_model([intent_reply, mapping_reply])
    result = ni_flow.run_flow(
        store, item_id,
        gateway_call=model,
        fetcher=lambda url: fixture,
        catalog=_empty_catalog(),
        source_url="https://query1.finance.yahoo.com/v8/finance/chart/AAPL",
    )
    assert result["state"] == "ready", f"got {result}"
    item = store.get_item(item_id)
    assert item is not None and item["state"] == "commissioning"
    # Frozen source (spec.source.url) is EXACTLY the consented URL (the C2
    # invariant — never a prefix match) and the preview data reflects the
    # extracted numeric values.
    assert item["spec"]["source"]["url"] == \
        "https://query1.finance.yahoo.com/v8/finance/chart/AAPL"
    preview_data = store.read_snapshot(item_id, "preview_data")
    assert preview_data is not None
    values = preview_data["payload"]
    assert isinstance(values.get("price"), (int, float))
    assert isinstance(values.get("prev_close"), (int, float))


def test_flow_paused_at_source_when_no_recipe_no_url() -> None:
    """No recipe hit + no user URL ⇒ flow pauses at ``source`` state."""
    store, _conn = _store()
    request = "custom feed I have not named yet"
    item_id = ni_flow.create_shell_item(store, request)
    intent_reply = json.dumps({
        "kind": "external_data", "subject": "feed", "cadence_minutes": 15,
        "wants": ["value"], "threshold": None, "display_hint": "value",
    })
    model = _scripted_model([intent_reply])
    ni_flow.run_flow(
        store, item_id,
        gateway_call=model,
        fetcher=lambda url: {},   # unused — no fetch happens on the pause
        catalog=_empty_catalog(),
    )
    record = ni_flow._flow_read(store, item_id)
    assert record is not None and record["state"] == "source"


def test_flow_resume_via_tool_completes(monkeypatch) -> None:
    """After pausing, resume_ni_flow (tool) fires the worker with the user's URL."""
    from smartbrain_3000 import tools as tmod

    store, _conn = _store()
    ctx = tmod.ToolContext(ni=store)
    # Kick off in the paused state.
    request = "some sensor readings I picked a URL for"
    item_id = ni_flow.create_shell_item(store, request)
    ni_flow._flow_write(store, item_id, ni_flow._make_record(
        request, "source", notes=["paused"]))
    fixture = _load("btc")
    intent_reply = json.dumps({
        "kind": "external_data", "subject": "BTC", "cadence_minutes": 15,
        "wants": ["price"], "threshold": None, "display_hint": "value",
    })
    mapping_reply = json.dumps({"price": "bitcoin.usd"})
    model = _scripted_model([intent_reply, mapping_reply])
    # Override the worker spawn to run synchronously so the assertion sees a final state.
    def _sync_worker(store_arg, iid, *, source_url=None, **_):
        ni_flow.run_flow(store_arg, iid, gateway_call=model,
                         fetcher=lambda url: fixture, catalog=_empty_catalog(),
                         source_url=source_url)
        return True
    monkeypatch.setattr(ni_flow, "start_flow_worker", _sync_worker)
    out = tmod.get_tool("resume_ni_flow").handler(
        ctx, {"item_id": item_id,
              "source_url": "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"})
    assert out["started"] is True
    record = ni_flow._flow_read(store, item_id)
    assert record is not None and record["state"] == "ready"


def test_flow_hn_list_end_to_end() -> None:
    """HN fixture → array-of-objects → repeat/list scene ends ``ready``."""
    store, _conn = _store()
    fixture = _load("hn")
    request = "top stories on Hacker News"
    item_id = ni_flow.create_shell_item(store, request)
    intent_reply = json.dumps({
        "kind": "external_data", "subject": "HN", "cadence_minutes": 15,
        "wants": ["title"], "threshold": None, "display_hint": "list",
    })
    mapping_reply = json.dumps({"title": "hits[0].title"})
    model = _scripted_model([intent_reply, mapping_reply])
    result = ni_flow.run_flow(
        store, item_id, gateway_call=model,
        fetcher=lambda url: fixture, catalog=_empty_catalog(),
        source_url="https://hn.algolia.com/api/v1/search?tags=front_page",
    )
    assert result["state"] == "ready"
    item = store.get_item(item_id)
    assert item is not None
    # Scene has a repeat root over ``rows`` with template binding ``item.title``.
    scene = item["spec"]["scene"]
    repeat = scene["children"][1]
    assert repeat["type"] == "repeat"


def test_flow_iss_map_degrades_to_value_with_note() -> None:
    """The ISS `map` display_hint is unsupported ⇒ value card + honest degradation note."""
    store, _conn = _store()
    fixture = _load("iss")
    request = "ISS location on a map"
    item_id = ni_flow.create_shell_item(store, request)
    intent_reply = json.dumps({
        "kind": "external_data", "subject": "ISS", "cadence_minutes": 5,
        "wants": ["latitude", "longitude"], "threshold": None, "display_hint": "map",
    })
    mapping_reply = json.dumps({
        "latitude": "latitude", "longitude": "longitude",
    })
    model = _scripted_model([intent_reply, mapping_reply])
    result = ni_flow.run_flow(
        store, item_id, gateway_call=model,
        fetcher=lambda url: fixture, catalog=_empty_catalog(),
        source_url="https://api.wheretheiss.at/v1/satellites/25544",
    )
    assert result["state"] == "ready"
    record = ni_flow._flow_read(store, item_id)
    # The degradation note rides on the flow record's notes list.
    notes = " ".join(record.get("notes") or [])
    assert "map" in notes.lower() and "unsupported" in notes.lower()


def test_flow_computed_unsupported_or_supported() -> None:
    """Countdown requests refuse cleanly OR route to the computed source if landed.

    Feature-detect: without the parallel agent's ``computed`` source type the
    flow terminates ``unsupported`` honestly; with it, the spec is built + the
    flow reaches ``ready``. This test tolerates both branches so it stays green
    across the parallel-agent merge boundary.
    """
    store, _conn = _store()
    request = "days until 2027-12-25"
    item_id = ni_flow.create_shell_item(store, request)
    intent_reply = json.dumps({
        "kind": "computed_only", "subject": "Christmas countdown",
        "cadence_minutes": 60, "wants": ["days"],
        "threshold": None, "display_hint": "value",
    })
    model = _scripted_model([intent_reply])
    result = ni_flow.run_flow(
        store, item_id, gateway_call=model,
        fetcher=lambda url: {}, catalog=_empty_catalog(),
    )
    if "computed" in nimod._SOURCE_TYPES:
        assert result["state"] == "ready"
    else:
        assert result["state"] == "unsupported"
        assert "computed" in (result.get("error") or "").lower()


def test_flow_chaos_drill_field_renamed_reports_honestly() -> None:
    """Rename a field in a fixture — mapping verification fails, flow reports ``failed(mapping)``."""
    store, _conn = _store()
    request = "show me AAPL"
    item_id = ni_flow.create_shell_item(store, request)
    # Original walker offers ``chart.result[0].meta.regularMarketPrice``; drop that key
    # so the model's requested exemplar path is absent → verification refuses.
    fixture = json.loads(json.dumps(_load("aapl")))
    del fixture["chart"]["result"][0]["meta"]["regularMarketPrice"]
    intent_reply = json.dumps({
        "kind": "external_data", "subject": "AAPL", "cadence_minutes": 5,
        "wants": ["price"], "threshold": None, "display_hint": "value",
    })
    # Both retries pick a path that isn't in the menu — mapping stage exhausts.
    bad = json.dumps({"price": "chart.result[0].meta.regularMarketPrice"})
    model = _scripted_model([intent_reply, bad, bad])
    result = ni_flow.run_flow(
        store, item_id, gateway_call=model,
        fetcher=lambda url: fixture, catalog=_empty_catalog(),
        source_url="https://query1.finance.yahoo.com/v8/finance/chart/AAPL",
    )
    assert result["state"] == "failed"
    assert (result.get("error") or "").startswith("mapping")


# ---- single-flight registry ---------------------------------------------

def test_worker_single_flight_per_item() -> None:
    """A second claim for the same item id is refused while one is in flight."""
    ni_flow._release("dup-1")  # ensure a clean slot
    assert ni_flow._claim("dup-1") is True
    try:
        assert ni_flow._claim("dup-1") is False
    finally:
        ni_flow._release("dup-1")


def test_worker_bounded_concurrency() -> None:
    """At most _MAX_CONCURRENT distinct claims outstanding at once."""
    ids = [f"conc-{i}" for i in range(ni_flow._MAX_CONCURRENT + 1)]
    for iid in ids[:ni_flow._MAX_CONCURRENT]:
        assert ni_flow._claim(iid) is True
    try:
        assert ni_flow._claim(ids[-1]) is False
    finally:
        for iid in ids[:ni_flow._MAX_CONCURRENT]:
            ni_flow._release(iid)


# ---- tool surface + door closure ----------------------------------------

def test_start_ni_flow_tool_registers_write_egress() -> None:
    """The tool is REVIEWED, egress=True, and joins NI_WRITE_TOOLS / UNATTENDED_NEVER_AUTO."""
    tool = tools.get_tool("start_ni_flow")
    assert tool is not None
    assert tool.tier is tools.Tier.REVIEWED and tool.egress is True
    assert "start_ni_flow" in tools.NI_WRITE_TOOLS
    assert "start_ni_flow" in tools.UNATTENDED_NEVER_AUTO


def test_start_ni_flow_rejects_bad_url() -> None:
    """A non-http source_url is refused inline by the prevalidate hook."""
    with pytest.raises(ValueError):
        tools._prevalidate_start_ni_flow({"request": "hi", "source_url": "ftp://x/y"})


def test_start_ni_flow_creates_shell_and_flow_record(monkeypatch) -> None:
    """The handler creates a shell item, seals a flow slot, and spawns a worker."""
    store, _conn = _store()
    ctx = tools.ToolContext(ni=store)
    fired: dict = {}

    def _fake_spawn(_store, iid, **kwargs) -> bool:
        fired["id"] = iid
        fired["source_url"] = kwargs.get("source_url")
        return True

    monkeypatch.setattr(ni_flow, "start_flow_worker", _fake_spawn)
    monkeypatch.setattr(tools, "_FLOW_WAIT_SECONDS", 0.05)
    out = tools.get_tool("start_ni_flow").handler(
        ctx, {"request": "show me AAPL",
              "source_url": "https://query1.finance.yahoo.com/v8/finance/chart/AAPL"})
    assert out["state"] == "intent" and out["id"]
    assert "read_ni_item" in out["next_step"], "timeout directive must say how to poll"
    assert fired["id"] == out["id"]
    assert fired["source_url"] == \
        "https://query1.finance.yahoo.com/v8/finance/chart/AAPL"
    record = ni_flow._flow_read(store, out["id"])
    assert record is not None and record["state"] == "intent"


def test_update_ni_item_refuses_source_change_on_flow_born(monkeypatch) -> None:
    """§29 door: a flow-born item refuses freeform source changes and points at remap."""
    store, _conn = _store()
    ctx = tools.ToolContext(ni=store)
    # Manually stamp a flow-born item so we don't depend on the worker running.
    item_id = ni_flow.create_shell_item(store, "some flow request")
    # ``create_shell_item`` already writes a "created via flow" journal entry.
    assert ni_flow.is_flow_or_recipe_born(store, item_id)
    with pytest.raises(ValueError, match="remap"):
        tools.get_tool("update_ni_item").handler(
            ctx, {"item_id": item_id,
                  "source": {"type": "model", "instruction": "different"}})


def test_remap_ni_item_re_enters_flow(monkeypatch) -> None:
    """`remap_ni_item` re-enters at Sampling using the item's existing consented URL."""
    store, _conn = _store()
    ctx = tools.ToolContext(ni=store)
    # Build a flow-born item WITH an http_json source.
    request = "AAPL card"
    item_id = ni_flow.create_shell_item(store, request)
    spec = dict(store.get_item(item_id)["spec"])
    spec["source"] = {"type": "http_json",
                      "url": "https://query1.finance.yahoo.com/v8/finance/chart/AAPL"}
    spec["scene"] = {"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "text", "value": "AAPL", "role": "title",
         "tone": "default", "size": "md"},
    ]}
    store.update_spec(item_id, spec, origin="agent")
    fired: dict = {}

    def _fake_spawn(_store, iid, **kwargs) -> bool:
        fired["id"] = iid
        fired["source_url"] = kwargs.get("source_url")
        return True

    monkeypatch.setattr(ni_flow, "start_flow_worker", _fake_spawn)
    monkeypatch.setattr(tools, "_FLOW_WAIT_SECONDS", 0.05)
    out = tools.get_tool("remap_ni_item").handler(ctx, {"item_id": item_id})
    # H2 (audit 2026-09-13): remap re-enters at Sampling, never Intent — the
    # tool's returned state reports the true restart stage.
    assert out["state"] == "sampling"
    assert fired["id"] == item_id
    assert fired["source_url"] == \
        "https://query1.finance.yahoo.com/v8/finance/chart/AAPL"


def test_board_flow_field_visible_and_hides_when_ready() -> None:
    """board_flow_field surfaces active/failed states; None after ``ready``."""
    store, _conn = _store()
    item_id = ni_flow.create_shell_item(store, "test request")
    field = ni_flow.board_flow_field(store, item_id)
    assert field is not None and field["state"] == "intent"
    # Force ``ready`` on the flow slot and verify it hides.
    ni_flow._transition(store, item_id, "ready")
    assert ni_flow.board_flow_field(store, item_id) is None


# ---- audit 2026-09-13: verified adversarial findings --------------------


def test_c1_run_flow_resolves_model_via_gateway_no_placeholder(monkeypatch) -> None:
    """C1 (audit 2026-09-13): the flow resolves its model via
    ``gateway.resolve_model("ni", ...)`` (with chat/agent fallbacks + local
    preference). No caller sees the literal placeholder ``flow-model``.
    """
    store, _conn = _store()
    item_id = ni_flow.create_shell_item(store, "reindex a source")
    seen: list[str] = []

    def _fake_gateway(model: str, prompt: str) -> str:
        seen.append(model)
        return json.dumps({
            "kind": "external_data", "subject": "x", "cadence_minutes": 15,
            "wants": ["price"], "threshold": None, "display_hint": "value",
        })

    # Fake load_routes to return an explicit ``ni`` route so the resolver
    # returns something predictable — the assertion is that THAT value hits
    # the fake gateway, not the old "flow-model" placeholder.
    from smartbrain_3000 import gateway as _gwmod
    monkeypatch.setattr(_gwmod, "load_routes",
                        lambda conn: {"ni": "mlx/qwen-local", "chat": "openai/gpt-4o"})
    result = ni_flow.run_flow(
        store, item_id, gateway_call=_fake_gateway,
        fetcher=lambda url: {"price": 1.0}, catalog=[],
    )
    assert result is not None, "run_flow must return a record"
    # First call hit the resolved model, never the placeholder.
    assert seen, "gateway_call was never invoked"
    assert seen[0] == "mlx/qwen-local", f"expected resolved ni route, got {seen[0]!r}"
    assert "flow-model" not in seen, "the placeholder must never reach the gateway"


def test_c1_local_preferred_over_cloud_ni_route(monkeypatch) -> None:
    """C1 (audit 2026-09-13): when the ni route is CLOUD but chat/agent points
    at a local model, the flow prefers the local one — flows are frequent +
    cheap and cloud stays fine when it's all the user has.
    """
    store, _conn = _store()
    from smartbrain_3000 import gateway as _gwmod
    monkeypatch.setattr(_gwmod, "load_routes", lambda conn: {
        "ni": "openai/gpt-4o", "chat": "mlx/qwen-local"})
    resolved = ni_flow._resolve_flow_model(store)
    assert resolved == "mlx/qwen-local", f"local chat preferred; got {resolved!r}"


def test_c1_no_placeholder_grep_in_source() -> None:
    """C1 (audit 2026-09-13): grep-test — no literal ``"flow-model"`` string
    anywhere in the flow engine or its tool surface.
    """
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "smartbrain_3000"
    for name in ("ni_flow.py", "tools.py", "ni_routes.py"):
        text = (root / name).read_text(encoding="utf-8")
        assert '"flow-model"' not in text and "'flow-model'" not in text, \
            f"placeholder 'flow-model' still present in {name}"


def test_c2_known_url_always_wins_and_freezes_verbatim(monkeypatch) -> None:
    """C2 (audit 2026-09-13): a user-consented ``source_url`` skips recipe
    matching entirely AND the frozen spec.source.url equals the approved URL.

    Fetch-capture asserts the fetcher was called with the exact URL the
    caller consented to — no silent rewrite between approval and seal.
    """
    store, _conn = _store()
    request = "AAPL data from my API"
    approved = "https://api.example.com/v1/quote?symbol=AAPL"
    item_id = ni_flow.create_shell_item(store, request)
    fetched: list[str] = []

    def _cap(url: str) -> dict:
        fetched.append(url)
        return {"price": 123.5}

    intent_reply = json.dumps({
        "kind": "external_data", "subject": "AAPL", "cadence_minutes": 5,
        "wants": ["price"], "threshold": None, "display_hint": "value",
    })
    mapping_reply = json.dumps({"price": "price"})
    model = _scripted_model([intent_reply, mapping_reply])
    # A permissive catalog: even with a "stock" recipe, known_url must win.
    catalog = [{"id": "stock", "title": "Stock quote", "category": "finance",
                "spec_template": {"source": {"type": "http_json",
                                              "url": "https://other/host"}}}]
    result = ni_flow.run_flow(
        store, item_id, gateway_call=lambda m, p: model(m, p),
        fetcher=_cap, catalog=catalog, source_url=approved,
    )
    assert result["state"] == "ready", f"got {result}"
    item = store.get_item(item_id)
    assert item is not None
    assert item["spec"]["source"]["url"] == approved, \
        "the frozen source URL must equal the approved URL (C2 invariant)"
    assert fetched == [approved], f"fetcher must be called with the approved URL only; got {fetched}"


def test_c2_matcher_rejects_audit_reproductions() -> None:
    """C2 (audit 2026-09-13): the audit's specific misclassifications are
    now clean refusals — "days until I retire", "track the S&P 500", "show
    me what I spent this month" all return None.
    """
    from smartbrain_3000 import ni_catalog
    catalog = ni_catalog.entries()
    for req in ("days until I retire",
                "track the S&P 500",
                "show me what I spent this month"):
        got = ni_flow.match_recipe(catalog, req, {"wants": ["value"]})
        assert got is None, f"expected no match for {req!r}; got {got and got['id']!r}"


def test_c3_recipe_hit_pauses_at_confirm_source_no_fetch(monkeypatch) -> None:
    """C3 (audit 2026-09-13): recipe match + no user URL ⇒ pause in
    ``confirm_source`` with the recipe URL in the flow record; no fetch runs.
    """
    store, _conn = _store()
    request = "show me AAPL stock every 5 minutes"
    item_id = ni_flow.create_shell_item(store, request)
    intent_reply = json.dumps({
        "kind": "external_data", "subject": "AAPL", "cadence_minutes": 5,
        "wants": ["price"], "threshold": None, "display_hint": "value",
    })
    model = _scripted_model([intent_reply])
    fetched: list[str] = []

    def _refuse(url: str) -> dict:
        fetched.append(url)
        raise AssertionError(f"fetch attempted before confirmation: {url}")

    catalog = [{"id": "stock-quote-finnhub", "title": "Stock quote",
                "category": "finance",
                "spec_template": {"source": {
                    "type": "http_json",
                    "url": "https://finnhub.io/api/v1/quote?symbol={{param:symbol}}"}},
                "url_template": "https://finnhub.io/api/v1/quote?symbol={{param:symbol}}"}]
    ni_flow.run_flow(
        store, item_id, gateway_call=lambda m, p: model(m, p),
        fetcher=_refuse, catalog=catalog,
    )
    record = ni_flow._flow_read(store, item_id)
    assert record is not None
    assert record["state"] == "confirm_source", f"got {record['state']!r}"
    assert record.get("error") == ni_flow.AWAITING_SOURCE_CONFIRM
    assert record.get("_recipe_id") == "stock-quote-finnhub"
    assert record.get("source_url") == \
        "https://finnhub.io/api/v1/quote?symbol={{param:symbol}}"
    assert fetched == [], "no fetch until one is confirmed (§29 promoted line)"


def test_c3_confirm_tool_resumes_with_that_url() -> None:
    """C3 (audit 2026-09-13): confirm_ni_flow_source resumes with the pending
    URL — mismatched URLs are refused; matching URL runs handoff against the
    shipped recipe.

    Uses ``fx-usd-eur`` (no secret params) so the resumed flow lands READY.
    """
    from smartbrain_3000 import tools as tmod
    store, _conn = _store()
    ctx = tmod.ToolContext(ni=store)
    request = "USD to EUR exchange rate hourly"
    item_id = ni_flow.create_shell_item(store, request)
    intent_reply = json.dumps({
        "kind": "external_data", "subject": "USD to EUR", "cadence_minutes": 60,
        "wants": ["rate"], "threshold": None, "display_hint": "value",
    })
    model = _scripted_model([intent_reply])
    # Let match_recipe read the shipped catalog so the confirm-tool's later
    # ni_catalog.get_recipe(...) lookup sees the same recipe.
    ni_flow.run_flow(
        store, item_id, gateway_call=lambda m, p: model(m, p),
        fetcher=lambda url: {"rates": {"EUR": 0.9}, "date": "2026-09-13"},
    )
    record = ni_flow._flow_read(store, item_id)
    assert record is not None and record["state"] == "confirm_source", \
        f"expected confirm_source; got {record!r}"
    pending_url = record.get("source_url")
    # A mismatched URL refuses (never seals a source the user didn't see).
    with pytest.raises(ValueError, match="does not match"):
        tmod.get_tool("confirm_ni_flow_source").handler(
            ctx, {"item_id": item_id,
                  "source_url": "https://api.example.com/other"})
    # The correct URL runs the handoff (item lands ready — no secret param).
    out = tmod.get_tool("confirm_ni_flow_source").handler(
        ctx, {"item_id": item_id, "source_url": pending_url})
    assert out["state"] == "ready", f"expected ready; got {out}"


def test_h1_credential_put_clears_flow_slot_when_secrets_filled() -> None:
    """H1 (audit 2026-09-13): with the flow record in awaiting_credential,
    a credential PUT that satisfies every declared secret clears the slot.
    """
    from smartbrain_3000 import ni_routes
    from smartbrain_3000.secrets import SecretStore

    store, conn = _store()
    # Build an item with one secret param + a flow record in awaiting_credential.
    item_id = ni_flow.create_shell_item(store, "secret-bearing card")
    spec = dict(store.get_item(item_id)["spec"])
    spec["params"] = {"api_key": {"label": "API key", "kind": "secret",
                                    "value": "ni:self:api_key"}}
    spec["source"] = {"type": "http_json",
                       "url": "https://api.example.com/x",
                       "headers": {"X-Key": {"$secret": f"ni:{item_id}:api_key"}}}
    store.update_spec(item_id, spec, origin="agent")
    ni_flow._transition(store, item_id, "awaiting_credential")
    # Simulate the PUT: put the credential, then run the clearing helper.
    key = gen_master_key()
    secrets = SecretStore(conn, key)
    from smartbrain_3000 import ni as _ni
    _ni.put_credential(secrets, item_id, "api_key", "s3cret", "api.example.com")
    item = store.get_item(item_id)
    ni_routes._clear_flow_when_credentials_satisfied(store, item, secrets)
    assert ni_flow.board_flow_field(store, item_id) is None, \
        "flow slot must be cleared once every secret is filled"


def test_h2_remap_never_re_enters_recipe_matching(monkeypatch) -> None:
    """H2 (audit 2026-09-13): remap re-enters at Sampling on the item's own
    frozen source — the fetcher sees THAT URL exactly and no recipe scoring
    happens.
    """
    store, _conn = _store()
    item_id = ni_flow.create_shell_item(store, "AAPL")
    spec = dict(store.get_item(item_id)["spec"])
    frozen_url = "https://query1.finance.yahoo.com/v8/finance/chart/AAPL"
    spec["source"] = {"type": "http_json", "url": frozen_url}
    spec["pipeline"] = [{"op": "extract", "paths": {"price": "chart.result[0].meta.regularMarketPrice"}}]
    store.update_spec(item_id, spec, origin="agent")
    record = ni_flow._make_record("AAPL", "sampling", source_url=frozen_url,
                                    notes=["remap re-entering"])
    record["_remap"] = True
    ni_flow._flow_write(store, item_id, record)
    fixture = _load("aapl")
    fetched: list[str] = []
    mapping_reply = json.dumps({
        "price": "chart.result[0].meta.regularMarketPrice",
    })
    model = _scripted_model([mapping_reply])
    # A populated catalog would normally match — the remap path skips it.
    catalog = [{"id": "always-wins", "title": "AAPL Stock", "category": "finance",
                "spec_template": {"source": {"type": "http_json",
                                              "url": "https://other/host"}}}]

    def _cap(url: str) -> object:
        fetched.append(url)
        return fixture

    result = ni_flow.run_flow(
        store, item_id, gateway_call=lambda m, p: model(m, p),
        fetcher=_cap, catalog=catalog,
    )
    assert result["state"] in ("ready", "failed"), f"got {result}"
    assert fetched == [frozen_url], \
        f"remap must fetch the item's own URL only; got {fetched}"


def test_h2_terminal_flow_slot_hidden_when_payload_exists() -> None:
    """H2 (audit 2026-09-13): a failed flow slot on an item with a
    renderable payload does NOT mask the tile — board_flow_field returns None.
    """
    store, _conn = _store()
    item_id = ni_flow.create_shell_item(store, "some card")
    # create_shell_item wrote a ``preview`` snapshot at add_item time, so the
    # item already has a renderable payload the board can fall back to.
    ni_flow._transition(store, item_id, "failed", error="mapping: no match")
    assert ni_flow.board_flow_field(store, item_id) is None, \
        "terminal slot must not mask the working card when a payload exists"


def test_h3_awaiting_pick_marker_on_source_state() -> None:
    """H3 (audit 2026-09-13): a source-state pause carries the AWAITING_SOURCE_PICK
    marker (the frontend labels it distinctly from an autonomous search).
    """
    store, _conn = _store()
    item_id = ni_flow.create_shell_item(store, "unknown feed")
    intent_reply = json.dumps({
        "kind": "external_data", "subject": "x", "cadence_minutes": 15,
        "wants": ["value"], "threshold": None, "display_hint": "value",
    })
    model = _scripted_model([intent_reply])
    ni_flow.run_flow(
        store, item_id, gateway_call=lambda m, p: model(m, p),
        fetcher=lambda url: {}, catalog=[],
    )
    record = ni_flow._flow_read(store, item_id)
    assert record is not None and record["state"] == "source"
    assert record.get("error") == ni_flow.AWAITING_SOURCE_PICK, \
        f"expected awaiting_pick marker; got {record.get('error')!r}"


def test_m1_born_marker_survives_journal_pruning() -> None:
    """M1 (audit 2026-09-13): the sealed ``_born`` key closes the §29 door
    even after 25 journal entries have pushed the ``created via flow`` line
    out of the (20-entry) journal history.
    """
    store, _conn = _store()
    item_id = ni_flow.create_shell_item(store, "flow request")
    for i in range(25):
        store.append_journal(item_id, "updated", f"noise-{i}")
    entries = store.read_journal(item_id)
    summaries = [e.get("summary") for e in entries]
    assert not any("via flow" in (s or "") for s in summaries), \
        "the door test only means something when the journal cue is gone"
    # And yet: the door still holds because ``_born`` rides the sealed spec.
    assert ni_flow.is_flow_or_recipe_born(store, item_id)


def test_m3_worker_refusal_writes_failed_state(monkeypatch) -> None:
    """M3 (audit 2026-09-13): a claim collision writes ``failed(busy)`` on
    the flow record instead of leaving a silent shell.
    """
    store, _conn = _store()
    item_id = ni_flow.create_shell_item(store, "busy-drill")
    ni_flow._claim(item_id)
    try:
        started = ni_flow.start_flow_worker(store, item_id, fetcher=lambda u: {})
    finally:
        ni_flow._release(item_id)
    assert started is False
    record = ni_flow._flow_read(store, item_id)
    assert record is not None
    assert record["state"] == "failed"
    assert (record.get("error") or "").startswith("busy")


def test_m3_boot_sweep_fails_stranded_flows() -> None:
    """M3 (audit 2026-09-13): sweep_stranded_flows fails records older than
    1 hour that are non-terminal + not awaiting_credential.
    """
    store, _conn = _store()
    item_id = ni_flow.create_shell_item(store, "stranded")
    # Backdate the record so the sweep sees it as older than 1h.
    record = ni_flow._flow_read(store, item_id) or {}
    stale = "2020-01-01T00:00:00+00:00"
    record["updated_at"] = stale
    ni_flow._flow_write(store, item_id, record)
    swept = ni_flow.sweep_stranded_flows(store)
    assert swept == 1
    after = ni_flow._flow_read(store, item_id)
    assert after is not None and after["state"] == "failed"


def test_m4_shell_duplicate_title_guard() -> None:
    """M4 (audit 2026-09-13): create_shell_item refuses a case-insensitive
    title match unless allow_duplicate=True.
    """
    store, _conn = _store()
    # Both requests derive the title from the first line — force a match.
    ni_flow.create_shell_item(store, "Weather")
    with pytest.raises(ValueError, match="already exists"):
        ni_flow.create_shell_item(store, "weather")
    # allow_duplicate escapes the guard.
    ni_flow.create_shell_item(store, "weather", allow_duplicate=True)


def test_minor_infer_fields_slugs_digit_leading_wants() -> None:
    """Minor (audit 2026-09-13): a want that starts with a digit slugs to
    ``f_<...>`` so the resulting extract output name passes ni._KEY_RE.
    """
    fields = ni_flow.infer_fields({"wants": ["24h volume"]})
    assert "f_24h_volume" in fields, f"got {sorted(fields)}"


def test_minor_menu_neutralizes_newlines_in_examples() -> None:
    """Minor (audit 2026-09-13): a fetched example with embedded newlines
    collapses to spaces in the M#2 menu (a fake candidate line cannot ride
    a JSON body's ``\\n``).
    """
    _cands = [{"path": "notes", "type": "string",
               "example": "line1\nfake-candidate line2"}]
    _usable, menu = ni_flow.build_mapping_menu(_cands, {"notes": "string"})
    assert "\n" not in menu.split("- ")[1], f"newline leaked into menu: {menu!r}"


def test_minor_computed_preview_uses_real_days() -> None:
    """Minor (audit 2026-09-13): the computed preview computes the real days
    at finalize (never a bare 0 for a real future date).
    """
    if "computed" not in nimod._SOURCE_TYPES:
        pytest.skip("computed source not registered in this build")
    store, _conn = _store()
    request = "days until 2099-12-25"
    item_id = ni_flow.create_shell_item(store, request)
    intent_reply = json.dumps({
        "kind": "computed_only", "subject": "Christmas",
        "cadence_minutes": 60, "wants": ["days"],
        "threshold": None, "display_hint": "value",
    })
    model = _scripted_model([intent_reply])
    result = ni_flow.run_flow(
        store, item_id, gateway_call=lambda m, p: model(m, p),
        fetcher=lambda url: {}, catalog=[],
    )
    assert result["state"] == "ready"
    preview = store.read_snapshot(item_id, "preview_data")
    assert preview is not None
    assert preview["payload"]["days"] > 0, f"got {preview['payload']}"


# ---- engine-gate 2026-09-13: field-type reconciliation against the sample ----

def test_reconcile_flips_time_to_number_on_quakes_fixture() -> None:
    """Engine gate (2026-09-13): USGS serves ``properties.time`` as an epoch
    NUMBER but the word table types ``time`` as string — the flow died at
    mapping ("is number, want string" twice). Reconcile must flip the field's
    type from the actual sampled payload.
    """
    cands = ni_flow.derive_paths(_load("quakes"))
    fields = ni_flow.reconcile_field_types(
        {"magnitude": "number", "location": "string", "time": "string"}, cands)
    assert fields["time"] == "number", f"time not flipped: {fields}"
    # magnitude has numeric name-matches (properties.mag under a contains rule
    # only when >=3 chars both ways — 'mag' in 'magnitude' qualifies); either
    # way it must stay a number.
    assert fields["magnitude"] == "number"
    # 'location' name-matches nothing in either type set — untouched.
    assert fields["location"] == "string"


def test_reconcile_keeps_type_when_wanted_type_has_a_name_match() -> None:
    """A field whose inferred type IS satisfiable by name never flips, even
    when the other type also carries a name-match."""
    cands = [
        {"path": "data.time", "type": "string", "example": "2026-09-13"},
        {"path": "data.time_ms", "type": "number", "example": 1757.0},
    ]
    fields = ni_flow.reconcile_field_types({"time": "string"}, cands)
    assert fields == {"time": "string"}


def test_reconcile_no_name_match_anywhere_keeps_inferred_type() -> None:
    """No name affinity on either side ⇒ the word-table type stands (the menu
    still offers all candidates of that type)."""
    cands = [
        {"path": "a.foo", "type": "number", "example": 1},
        {"path": "a.bar", "type": "string", "example": "x"},
    ]
    fields = ni_flow.reconcile_field_types({"temperature": "number"}, cands)
    assert fields == {"temperature": "number"}


def test_quakes_flow_reaches_ready_on_recorded_fixture() -> None:
    """End-to-end regression for the engine-gate failure: the quakes request
    now survives mapping with the model picking the epoch-number time path.
    """
    sample = _load("quakes")
    store, _conn = _store()
    request = "show me the latest earthquakes above magnitude 5"
    url = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson"
    item_id = ni_flow.create_shell_item(store, request)
    intent_reply = json.dumps({
        "kind": "external_data", "subject": "earthquakes",
        "cadence_minutes": 15, "wants": ["magnitude", "location", "time"],
        "threshold": 5, "display_hint": "list",
    })

    def mapping_reply(prompt: str) -> str:
        # Deterministic scripted pick: mirror what the live model chose —
        # the menu must now OFFER the number-typed time path for 'time'.
        assert "features[0].properties.time" in prompt, "time path missing from menu"
        return json.dumps({
            "magnitude": "features[0].properties.mag",
            "location": "features[0].properties.place",
            "time": "features[0].properties.time",
        })

    replies = iter([intent_reply, None])

    def gateway(_model: str, prompt: str) -> str:
        first = next(replies, None)
        return first if first is not None else mapping_reply(prompt)

    result = ni_flow.run_flow(
        store, item_id, gateway_call=gateway,
        fetcher=lambda _u: sample, source_url=url, catalog=[],
    )
    assert result["state"] == "ready", f"state={result['state']} rec={result}"
    item = store.get_item(item_id)
    assert item["spec"]["source"]["url"] == url


# ---- one-door law + needs_params wave (2026-09-14) -----------------------
# Field failure: "30 min AAPL card" — the chat model started a flow, wandered
# off during the async gap (web-searching sources), bypassed the flow via
# create_ni_item_from_recipe with an EMPTY symbol param, and commissioned a
# card rendering $0.00 "ok" (Finnhub returns sentinel zeros for empty symbol).

def test_create_ni_item_refuses_http_json_at_prevalidate() -> None:
    """D1: an http_json source bounces at propose time with a flow pointer —
    the whole model-authored-external-fetch class closes before any card parks.
    """
    tool = tools.get_tool("create_ni_item")
    args = {
        "title": "X", "goal": "g",
        "source": {"type": "http_json", "url": "https://api.example.com/q",
                   "headers": {}},
        "pipeline": [], "scene": {"type": "stack", "dir": "v", "gap": "sm",
                                   "children": [{"type": "text", "value": "hi",
                                                 "role": "title", "tone": "default",
                                                 "size": "md"}]},
        "display": {"size": "small"}, "interval_minutes": 30,
        "preview_payload": {},
    }
    with pytest.raises(ValueError, match="start_ni_flow"):
        tool.prevalidate(args)
    store, _conn = _store()
    with pytest.raises(ValueError, match="start_ni_flow"):
        tool.handler(tools.ToolContext(ni=store), args)


def test_recipe_tool_is_retired_from_model_registry() -> None:
    """D1: create_ni_item_from_recipe no longer exists as a model tool —
    recipes ride inside the flow behind the confirm_source pause."""
    assert "create_ni_item_from_recipe" not in {t.name for t in tools._TOOLS}
    assert tools.get_tool("create_ni_item_from_recipe") is None


def test_flow_next_step_directives_cover_every_settled_state() -> None:
    """D1: every settled flow state maps to a specific directive; unknown /
    still-running states direct a read_ni_item poll and forbid side quests."""
    for state, needle in [
        ("ready", "commissioning"),
        ("confirm_source", "confirm_ni_flow_source"),
        ("source", "resume_ni_flow"),
        ("awaiting_credential", "Add key"),
        ("failed", "failed"),
        ("unsupported", "declined"),
    ]:
        text = tools._flow_next_step({"state": state, "error": "boom"})
        assert needle in text, f"{state}: {text}"
    still = tools._flow_next_step({"state": "sampling"})
    assert "read_ni_item" in still and "Do NOT" in still


def test_start_ni_flow_returns_settled_state_when_worker_finishes(monkeypatch) -> None:
    """D1: the bounded wait returns the flow's REAL resulting state (no async
    gap in the common case) — a synchronous worker that settles to
    ``confirm_source`` is reported as such, with the matching directive."""
    store, _conn = _store()
    ctx = tools.ToolContext(ni=store)

    def _sync_worker(store_arg, iid, **_kwargs) -> bool:
        ni_flow._transition(store_arg, iid, "confirm_source",
                             source_url="https://api.example.com/vetted")
        return True

    monkeypatch.setattr(ni_flow, "start_flow_worker", _sync_worker)
    out = tools.get_tool("start_ni_flow").handler(
        ctx, {"request": "watch the example number"})
    assert out["state"] == "confirm_source"
    assert out["source_url"] == "https://api.example.com/vetted"
    assert "confirm_ni_flow_source" in out["next_step"]


def test_item_id_shape_prevalidate_bounces_invented_ids() -> None:
    """D5: a slugged title or non-UUID id bounces at prevalidate on every
    item-addressed tool — BEFORE any approval card parks."""
    for name in ("run_ni_item_now", "set_ni_item_enabled", "delete_ni_item",
                 "read_ni_item", "update_ni_item", "remap_ni_item",
                 "resume_ni_flow", "confirm_ni_flow_source"):
        tool = tools.get_tool(name)
        assert tool.prevalidate is not None, f"{name} must carry a prevalidate"
        with pytest.raises(ValueError, match="not a card id"):
            tool.prevalidate({"item_id": "aapl-quote-every-30m",
                              "source_url": "https://api.example.com/q",
                              "enabled": True})


def test_item_not_found_names_existing_cards() -> None:
    """D5: an execute-time miss returns the real card list so the model
    self-corrects in one step instead of guessing again."""
    store, _conn = _store()
    ctx = tools.ToolContext(ni=store)
    item_id = ni_flow.create_shell_item(store, "my real card")
    with pytest.raises(ValueError) as excinfo:
        tools.get_tool("run_ni_item_now").handler(
            ctx, {"item_id": "12345678-1234-1234-1234-1234567890ab"})
    msg = str(excinfo.value)
    assert item_id in msg and "existing cards" in msg


def test_intent_place_field_validated() -> None:
    """geocode-consent (2026-09-15): ``place`` is optional, string-or-null,
    length-bounded."""
    base = {"kind": "external_data", "subject": "x", "cadence_minutes": 15,
            "wants": ["temp"], "threshold": None, "display_hint": "value"}
    assert ni_flow._validate_intent(dict(base))["kind"] == "external_data"
    ok = ni_flow._validate_intent({**base, "place": "Kansas City"})
    assert ok["place"] == "Kansas City"
    long = ni_flow._validate_intent({**base, "place": "x" * 500})
    assert len(long["place"]) == 120
    with pytest.raises(ValueError, match="place"):
        ni_flow._validate_intent({**base, "place": 42})


# --- A12 / A13 (case matrix) — deterministic authoring hooks --------------

def test_wants_fahrenheit_regex_hits_and_misses() -> None:
    """Case-insensitive; the ``°F`` branch requires the degree glyph so a bare
    ticker like ``F`` never trips the units cue."""
    assert ni_flow._wants_fahrenheit("temperature in Kansas City in Fahrenheit")
    assert ni_flow._wants_fahrenheit("show me the temp in °F please")
    assert ni_flow._wants_fahrenheit("weather in °  F right now")
    assert not ni_flow._wants_fahrenheit("track F stock")
    assert not ni_flow._wants_fahrenheit("temperature in Celsius")


def test_detect_alert_op_direction_words() -> None:
    """Word-bounded lookup; lt wins when both classes appear (drops below)."""
    assert ni_flow._detect_alert_op("alert me when bitcoin drops below 50000") == "lt"
    assert ni_flow._detect_alert_op("bitcoin exceeds 60000") == "gt"
    assert ni_flow._detect_alert_op("bitcoin more than 60000") == "gt"
    assert ni_flow._detect_alert_op("bitcoin less than 50000") == "lt"
    assert ni_flow._detect_alert_op("bitcoin overhead") is None
    assert ni_flow._detect_alert_op("bitcoin price") is None


def test_flow_fahrenheit_composes_scale_and_offset(monkeypatch) -> None:
    """A12: kc_weather + a °F request ends ``ready`` with a pipeline carrying
    scale 1.8 then offset 32 for the temperature field; preview > 60."""
    store, _conn = _store()
    fixture = _load("kc_weather")
    request = "temperature in Kansas City in Fahrenheit"
    item_id = ni_flow.create_shell_item(store, request)
    intent_reply = json.dumps({
        "kind": "external_data", "subject": "Kansas City temperature",
        "cadence_minutes": 15, "wants": ["temperature"],
        "threshold": None, "display_hint": "value",
    })
    mapping_reply = json.dumps({"temperature": "current_weather.temperature"})
    model = _scripted_model([intent_reply, mapping_reply])
    result = ni_flow.run_flow(
        store, item_id, gateway_call=model,
        fetcher=lambda _u: fixture, catalog=_empty_catalog(),
        source_url="https://api.open-meteo.com/v1/forecast?latitude=39.1&longitude=-94.6&current_weather=true",
    )
    assert result["state"] == "ready", f"got {result}"
    item = store.get_item(item_id)
    assert item is not None
    pipeline = item["spec"]["pipeline"]
    # Extract stage plus one transform stage with scale then offset in that order.
    assert len(pipeline) == 2, f"pipeline: {pipeline}"
    assert pipeline[0]["op"] == "extract"
    apply = pipeline[1]["apply"]
    scale_op = next(o for o in apply if o["fn"] == "scale")
    offset_op = next(o for o in apply if o["fn"] == "offset")
    assert scale_op == {"fn": "scale", "field": "temperature", "factor": 1.8}
    assert offset_op == {"fn": "offset", "field": "temperature", "value": 32}
    # scale index < offset index — order matters (F = C*1.8 + 32).
    assert apply.index(scale_op) < apply.index(offset_op)
    preview_data = store.read_snapshot(item_id, "preview_data")
    assert preview_data is not None
    assert preview_data["payload"]["temperature"] > 60


def test_flow_no_fahrenheit_conversion_when_not_asked(monkeypatch) -> None:
    """A12: a non-°F temperature request receives NO scale/offset stages."""
    store, _conn = _store()
    fixture = _load("kc_weather")
    request = "temperature in Kansas City"  # no fahrenheit / °F
    item_id = ni_flow.create_shell_item(store, request)
    intent_reply = json.dumps({
        "kind": "external_data", "subject": "Kansas City temperature",
        "cadence_minutes": 15, "wants": ["temperature"],
        "threshold": None, "display_hint": "value",
    })
    mapping_reply = json.dumps({"temperature": "current_weather.temperature"})
    model = _scripted_model([intent_reply, mapping_reply])
    result = ni_flow.run_flow(
        store, item_id, gateway_call=model,
        fetcher=lambda _u: fixture, catalog=_empty_catalog(),
        source_url="https://api.open-meteo.com/v1/forecast?latitude=39.1&longitude=-94.6&current_weather=true",
    )
    assert result["state"] == "ready"
    item = store.get_item(item_id)
    pipeline = item["spec"]["pipeline"]
    # Only the extract stage — no transform authored.
    assert len(pipeline) == 1 and pipeline[0]["op"] == "extract"


def test_flow_alert_authored_on_value_card_with_threshold_direction(monkeypatch) -> None:
    """A13: btc fixture + "alert me when bitcoin drops below 50000" ends
    ``ready`` with exactly one lt alert bound to the mapped numeric field."""
    store, _conn = _store()
    fixture = _load("btc")
    request = "alert me when bitcoin drops below 50000"
    item_id = ni_flow.create_shell_item(store, request)
    intent_reply = json.dumps({
        "kind": "external_data", "subject": "Bitcoin",
        "cadence_minutes": 15, "wants": ["price"],
        "threshold": 50000, "display_hint": "value",
    })
    mapping_reply = json.dumps({"price": "bitcoin.usd"})
    model = _scripted_model([intent_reply, mapping_reply])
    result = ni_flow.run_flow(
        store, item_id, gateway_call=model,
        fetcher=lambda _u: fixture, catalog=_empty_catalog(),
        source_url="https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
    )
    assert result["state"] == "ready", f"got {result}"
    item = store.get_item(item_id)
    alerts = item["spec"].get("alerts")
    assert isinstance(alerts, list) and len(alerts) == 1, f"alerts: {alerts}"
    rule = alerts[0]
    assert rule["left"] == {"$bind": "price"}
    assert rule["op"] == "lt"
    assert rule["right"] == 50000
    assert isinstance(rule["message"], str) and rule["message"]
    assert re.match(r"^[a-z0-9-]{1,40}$", rule["name"]) is not None


def test_flow_no_alert_without_threshold(monkeypatch) -> None:
    """A13: no threshold ⇒ no alert (even when direction words are present)."""
    store, _conn = _store()
    fixture = _load("btc")
    request = "bitcoin price"  # no threshold, no direction word
    item_id = ni_flow.create_shell_item(store, request)
    intent_reply = json.dumps({
        "kind": "external_data", "subject": "Bitcoin",
        "cadence_minutes": 15, "wants": ["price"],
        "threshold": None, "display_hint": "value",
    })
    mapping_reply = json.dumps({"price": "bitcoin.usd"})
    model = _scripted_model([intent_reply, mapping_reply])
    result = ni_flow.run_flow(
        store, item_id, gateway_call=model,
        fetcher=lambda _u: fixture, catalog=_empty_catalog(),
        source_url="https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
    )
    assert result["state"] == "ready"
    item = store.get_item(item_id)
    assert item["spec"].get("alerts") in (None, [], )


def test_flow_list_class_threshold_never_authors_alert() -> None:
    """A13 guard: the quakes list case (threshold + list class) still ends
    ``ready`` without an alert — alerts are value-card-only. `where` filter
    authoring belongs to a parallel wave; this test only owns the alert gate.
    """
    sample = _load("quakes")
    store, _conn = _store()
    request = "show me the latest earthquakes above magnitude 5"
    url = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson"
    item_id = ni_flow.create_shell_item(store, request)
    intent_reply = json.dumps({
        "kind": "external_data", "subject": "earthquakes",
        "cadence_minutes": 15, "wants": ["magnitude", "location", "time"],
        "threshold": 5, "display_hint": "list",
    })
    mapping_reply = json.dumps({
        "magnitude": "features[0].properties.mag",
        "location": "features[0].properties.place",
        "time": "features[0].properties.time",
    })
    replies = iter([intent_reply, mapping_reply])

    def gateway(_model: str, _prompt: str) -> str:
        nxt = next(replies)
        assert isinstance(nxt, str), "reply present"
        return nxt

    result = ni_flow.run_flow(
        store, item_id, gateway_call=gateway,
        fetcher=lambda _u: sample, source_url=url, catalog=_empty_catalog(),
    )
    assert result["state"] == "ready", f"got {result}"
    item = store.get_item(item_id)
    assert item["spec"].get("alerts") in (None, [], )


def test_maybe_author_alert_slug_and_message_bound() -> None:
    """The authored slug matches the §12 name regex and message stays ≤500."""
    spec = {"alerts": None}
    fields = {"price": "number"}
    intent = {"subject": "Bitcoin", "threshold": 50000}
    field = ni_flow._maybe_author_alert(
        spec, fields, ni_flow._DISPLAY_VALUE,
        "alert me when bitcoin drops below 50000", intent,
    )
    assert field == "price"
    rule = spec["alerts"][0]
    assert re.match(r"^[a-z0-9-]{1,40}$", rule["name"]) is not None
    assert len(rule["message"]) <= 500
    # Shape passes the §12 validator too — the invariant the flow assumes.
    nimod._validate_alerts_spec(spec["alerts"])


def test_list_hint_with_scalar_paths_degrades_to_value_card() -> None:
    """A9/A11 (case matrix): a 'list' display hint whose picked mapping paths
    carry no [N] step degrades to the value card deterministically — the data
    decides, not the hint (crypto-pair / sunrise-times live-gate gap)."""
    store, _conn = _store()
    fixture = _load("btc")
    request = "track bitcoin and ethereum prices in USD"
    item_id = ni_flow.create_shell_item(store, request)
    intent_reply = json.dumps({
        "kind": "external_data", "subject": "BTC+ETH", "cadence_minutes": 15,
        "wants": ["bitcoin price"], "threshold": None,
        "place": None, "display_hint": "list",
    })
    mapping_reply = json.dumps({"bitcoin_price": "bitcoin.usd"})
    model = _scripted_model([intent_reply, mapping_reply])
    result = ni_flow.run_flow(
        store, item_id, gateway_call=model,
        fetcher=lambda _u: fixture, catalog=_empty_catalog(),
        source_url="https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
    )
    assert result["state"] == "ready", f"got {result}"
    item = store.get_item(item_id)
    # Value scene, not a repeat/list scene.
    scene_types = json.dumps(item["spec"]["scene"])
    assert '"repeat"' not in scene_types
    record = ni_flow._flow_read(store, item_id) or {}
    assert any("no list-shaped data" in n for n in record.get("notes") or []), record


def test_cadence_from_text_parses_the_phrase_classes() -> None:
    """Deterministic cadence extraction (2026-09-15): code owns the textual
    cadence; the model's value is only the fallback."""
    cases = {
        "AAPL every 5 minutes": 5,
        "hourly EUR to USD exchange rate": 60,
        "rate updated every hour": 60,
        "ISS minute by minute": 1,
        "refresh each minute": 1,
        "every 2 hours": 120,
        "every 10 seconds": 1,      # floor clamp, honest
        "bitcoin twice a day": 720,
        "news every morning": 1440,
        "summary once a week": 10080,
        "just show me bitcoin": None,
    }
    for text, want in cases.items():
        assert ni_flow._cadence_from_text(text) == want, text


def test_stage_intent_code_cadence_overrides_model_value() -> None:
    """'hourly X' with a model that wrongly answers 15 still lands 60."""
    reply = json.dumps({
        "kind": "external_data", "subject": "EUR/USD", "cadence_minutes": 15,
        "wants": ["rate"], "threshold": None, "place": None,
        "display_hint": "value",
    })
    model = _scripted_model([reply])
    intent = ni_flow.stage_intent("hourly EUR to USD exchange rate",
                                   lambda p: model("m", p))
    assert intent["cadence_minutes"] == 60


def test_uncovered_wants_disclosed_on_recipe_confirm() -> None:
    """F3 (C2-feedback wave): the NVDA field run asked for volume; the Finnhub
    /quote recipe serves price/o/h/l/prev — the gap must be sealed on the
    confirm record and noted, never a silent partial fulfillment."""
    from smartbrain_3000 import ni_catalog
    store, _conn = _store()
    recipe = ni_catalog.get_recipe("stock-quote-finnhub")
    assert recipe is not None
    intent = {"subject": "NVDA", "cadence_minutes": 30, "place": None,
              "wants": ["price", "open", "high", "low", "close", "volume"]}
    item_id = ni_flow.create_shell_item(store, "NVDA every 30 minutes with OHLCV")
    ni_flow._pause_for_recipe_confirm(store, item_id, intent, recipe)
    record = ni_flow._flow_read(store, item_id)
    assert record is not None
    uncovered = record.get("_uncovered_wants")
    assert uncovered and "volume" in uncovered, f"got {uncovered}"
    assert "price" not in uncovered and "high" not in uncovered
    assert any("does not appear to cover" in n for n in record.get("notes") or [])


def test_covered_wants_stamp_nothing() -> None:
    """A fully-served intent seals no coverage field — no noise on the happy path."""
    from smartbrain_3000 import ni_catalog
    store, _conn = _store()
    recipe = ni_catalog.get_recipe("stock-quote-finnhub")
    intent = {"subject": "AAPL", "cadence_minutes": 30, "place": None,
              "wants": ["price", "high", "low"]}
    item_id = ni_flow.create_shell_item(store, "AAPL stock price")
    ni_flow._pause_for_recipe_confirm(store, item_id, intent, recipe)
    record = ni_flow._flow_read(store, item_id)
    assert record is not None and "_uncovered_wants" not in record


def test_flow_tool_result_carries_not_covered(monkeypatch) -> None:
    """The start_ni_flow result names the gap so the chat can disclose it."""
    from smartbrain_3000 import ni_catalog
    store, _conn = _store()
    ctx = tools.ToolContext(ni=store)
    recipe = ni_catalog.get_recipe("stock-quote-finnhub")

    def _sync_worker(store_arg, iid, **_kwargs) -> bool:
        ni_flow._pause_for_recipe_confirm(
            store_arg, iid,
            {"subject": "NVDA", "cadence_minutes": 30, "place": None,
             "wants": ["price", "volume"]}, recipe)
        return True

    monkeypatch.setattr(ni_flow, "start_flow_worker", _sync_worker)
    out = tools.get_tool("start_ni_flow").handler(
        ctx, {"request": "NVDA price and volume every 30 minutes"})
    assert out["state"] == "confirm_source"
    assert out.get("not_covered") == ["volume"]
    assert "does not cover" in out["next_step"]
