"""Deterministic-authoring backend (§26/§27/§28) tests.

Covers:
  * the flow's recipe handoff end-to-end (deterministic spec equality, always-draft
    for secret recipes, symbol param fill, needs_params draft landing, journal);
  * every recipe's sample_response actually round-trips its pipeline + scene bind;
  * derive_ni_paths (nested walk, unaddressable-keys reporting, caps, JSON-string
    input via the programmatic path);
  * journal append + prune + delete-cascade + writer sites;
  * read_ni_item ``state_explanation`` / ``user_next_action`` strings per state;
  * board rows carry ``needs_credentials`` as [{name, label}];
  * read_ni_item exposes the sealed last_failure excerpt (http sources only).
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import duckdb
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import ni as nimod
from smartbrain_3000 import ni_catalog, tools
from smartbrain_3000.secrets import gen_master_key

# --- helpers -----------------------------------------------------------------

def _tool_ctx() -> tuple[tools.ToolContext, duckdb.DuckDBPyConnection, bytes]:
    """Fresh NIStore wrapped in a ToolContext (mirrors test_ni._tool_ctx)."""
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    return tools.ToolContext(ni=nimod.NIStore(conn, key)), conn, key


def _tool_call(name: str, ctx: tools.ToolContext, args: dict) -> dict:
    tool = tools.get_tool(name)
    assert tool is not None, f"tool {name!r} not registered"
    return tool.handler(ctx, tools.validate_args(tool, args))


# --- recipe catalog end-to-end determinism ----------------------------------

def test_every_recipe_with_a_sample_response_runs_pipeline_and_binds() -> None:
    """Named again here so a determinism regression fails in the recipes suite too,
    not just at ni_catalog import."""
    for src in ni_catalog.entries():
        sample = src.get("sample_response")
        if sample is None:
            continue
        spec = src["spec_template"]
        outputs = nimod.run_pipeline(spec.get("pipeline") or [], sample)
        nimod.bind_scene(spec["scene"], outputs,
                         history=nimod._seed_history(spec),
                         image_ref=nimod._preview_image_ref(spec, src["id"]))


# --- recipe handoff via the flow (one-door law 2026-09-14) ------------------
# create_ni_item_from_recipe is RETIRED from the model registry; the recipe
# build now happens ONLY inside the flow (confirm_source pause ->
# continue_from_recipe_confirm -> _handoff_from_recipe). These tests drive
# that path directly (it is model-free after the intent is sealed).

def _confirm_recipe(store, recipe_id: str, request: str, intent: dict) -> str:
    """Shell + sealed confirm_source record + operator confirmation."""
    from smartbrain_3000 import ni_flow
    recipe = ni_catalog.get_recipe(recipe_id)
    assert recipe is not None, recipe_id
    item_id = ni_flow.create_shell_item(store, request)
    record = ni_flow._make_record(request, "confirm_source",
                                   source_url=recipe["url_template"],
                                   notes=["test: awaiting confirm"])
    record["_recipe_id"] = recipe_id
    record["intent"] = intent
    ni_flow._flow_write(store, item_id, record)
    result = ni_flow.continue_from_recipe_confirm(store, item_id,
                                                   recipe["url_template"])
    # keyed recipes settle awaiting_credential; keyless settle ready
    assert result.get("state") in ("ready", "awaiting_credential"), result
    return item_id


def test_flow_recipe_keyless_lands_commissioning_with_template_url() -> None:
    """A keyless recipe lands commissioning; the sealed source.url equals the
    recipe url_template; a 'recipe' journal entry names the recipe."""
    ctx, _c, _k = _tool_ctx()
    recipe = ni_catalog.get_recipe("crypto-price-btc-usd")
    iid = _confirm_recipe(ctx.ni, "crypto-price-btc-usd",
                          "track bitcoin in usd",
                          {"subject": "Bitcoin", "cadence_minutes": 15})
    item = ctx.ni.get_item(iid)
    assert item["state"] == "commissioning"
    assert item["spec"]["source"]["url"] == recipe["url_template"]
    journal = ctx.ni.read_journal(iid)
    assert any(e["kind"] == "recipe" for e in journal)


def test_flow_recipe_keyed_lands_draft_and_rewrites_self_refs() -> None:
    """A keyed recipe ALWAYS lands draft; ``ni:self:<name>`` refs rewrite to
    ``ni:<item_id>:<name>`` in the sealed spec (install-path parity)."""
    ctx, _c, _k = _tool_ctx()
    iid = _confirm_recipe(ctx.ni, "stock-quote-finnhub",
                          "show me AAPL stock",
                          {"subject": "AAPL", "cadence_minutes": 30})
    item = ctx.ni.get_item(iid)
    assert item["state"] == "draft"
    ref = item["spec"]["source"]["headers"]["X-Finnhub-Token"]["$secret"]
    assert ref == f"ni:{iid}:api_key"


def test_flow_recipe_fills_symbol_param_deterministically() -> None:
    """needs_params (2026-09-14): the field failure was a recipe card created
    with symbol='' fetching ``?symbol=`` — Finnhub returned sentinel zeros and
    the card commissioned a lying $0.00 quote. The flow handoff now fills the
    ``symbol`` slot from the SAME corroborated ticker token that matched the
    finance recipe. Pure code, no model."""
    ctx, _c, _k = _tool_ctx()
    iid = _confirm_recipe(ctx.ni, "stock-quote-finnhub",
                          "show me AAPL stock every 30 minutes",
                          {"subject": "AAPL", "cadence_minutes": 30})
    item = ctx.ni.get_item(iid)
    assert item["spec"]["params"]["symbol"]["value"] == "AAPL"
    # No unfilled referenced params remain — only the credential blocks it.
    assert nimod.unfilled_referenced_params(item["spec"]) == []


def test_flow_recipe_unfillable_param_lands_draft_needs_params() -> None:
    """A recipe slot code cannot derive stays EMPTY on purpose and forces a
    draft landing — never a commissioning card that dies param_empty."""
    from smartbrain_3000 import ni_flow
    ctx, _c, _k = _tool_ctx()
    recipe = ni_catalog.get_recipe("stock-quote-finnhub")
    assert recipe is not None
    item_id = ni_flow.create_shell_item(ctx.ni, "watch my stock please")
    record = ni_flow._make_record("watch my stock please", "confirm_source",
                                   source_url=recipe["url_template"],
                                   notes=["test"])
    record["_recipe_id"] = "stock-quote-finnhub"
    record["intent"] = {"subject": "my stock", "cadence_minutes": 30}
    ni_flow._flow_write(ctx.ni, item_id, record)
    result = ni_flow.continue_from_recipe_confirm(ctx.ni, item_id,
                                                    recipe["url_template"])
    assert result.get("state") in ("ready", "awaiting_credential")
    item = ctx.ni.get_item(item_id)
    # "watch my stock please" carries no ticker token — symbol stays empty.
    assert item["spec"]["params"]["symbol"]["value"] == ""
    assert item["state"] == "draft"
    assert "symbol" in nimod.unfilled_referenced_params(item["spec"])


# --- freeform create: always-draft-with-secrets + rewrite --------------------

def _basic_scene() -> dict:
    return {"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "text", "value": "{{text}}", "role": "title",
         "tone": "default", "size": "md"},
    ]}


def _basic_args(**over) -> dict:
    body = {
        "title": "Freeform",
        "goal": "test",
        "params": {},
        "source": {"type": "model", "instruction": "hi"},
        "pipeline": [],
        "scene": _basic_scene(),
        "display": {"size": "small"},
        "interval_minutes": 60,
        "preview_payload": {"text": "sunny"},
    }
    body.update(over)
    return body


def test_create_ni_item_rewrites_self_refs_at_create() -> None:
    """H2 install-path parity extended to the chat create path — a spec that ships
    with ``ni:self:<name>`` is rewritten to ``ni:<item_id>:<name>`` in one sealed
    write (pre-minted item id)."""
    ctx, _c, _k = _tool_ctx()
    args = _basic_args(
        title="Freeform (keyed)",
        params={"api_key": {"label": "Key", "kind": "secret", "value": "ni:self:api_key"}},
        source={"type": "http_page",
                "url": "https://api.example.com/q",
                "headers": {"X-Api-Key": {"$secret": "ni:self:api_key"}}},
    )
    out = _tool_call("create_ni_item", ctx, args)
    assert out["state"] == "draft"
    item = ctx.ni.get_item(out["id"])
    ref = item["spec"]["source"]["headers"]["X-Api-Key"]["$secret"]
    assert ref == f"ni:{out['id']}:api_key"
    assert item["spec"]["params"]["api_key"]["value"] == f"ni:{out['id']}:api_key"


def test_create_ni_item_journals_created_entry() -> None:
    ctx, _c, _k = _tool_ctx()
    out = _tool_call("create_ni_item", ctx, _basic_args())
    journal = ctx.ni.read_journal(out["id"])
    assert len(journal) == 1
    assert journal[0]["kind"] == "created"
    assert "the routed model" in journal[0]["summary"]


def test_create_ni_item_duplicate_guard_uses_case_insensitive_title() -> None:
    ctx, _c, _k = _tool_ctx()
    _tool_call("create_ni_item", ctx, _basic_args(title="ACME quote"))
    with pytest.raises(ValueError) as exc:
        _tool_call("create_ni_item", ctx, _basic_args(title="acme QUOTE"))
    assert "already exists" in str(exc.value)


def test_create_ni_item_allow_duplicate_lets_a_second_card_through() -> None:
    ctx, _c, _k = _tool_ctx()
    _tool_call("create_ni_item", ctx, _basic_args(title="Weather"))
    out2 = _tool_call("create_ni_item", ctx,
                      _basic_args(title="Weather", allow_duplicate=True))
    assert out2["id"]


# --- derive_ni_paths --------------------------------------------------------

def test_derive_ni_paths_nested_sample_yields_leaves_with_types_and_examples() -> None:
    ctx = tools.ToolContext()
    sample = {"bitcoin": {"usd": 65000, "eur": 60000}}
    out = _tool_call("derive_ni_paths", ctx, {"sample": sample})
    paths = {entry["path"] for entry in out["paths"]}
    assert "bitcoin.usd" in paths and "bitcoin.eur" in paths
    # Numeric leaves come first per the deterministic ordering rule.
    first = out["paths"][0]
    assert first["type"] == "number"


def test_derive_ni_paths_reports_unaddressable_keys() -> None:
    """§27 grammar: keys with spaces / dots / punctuation are NOT §4.1-addressable —
    the tool reports them so the model knows to pick a different source."""
    ctx = tools.ToolContext()
    sample = {"Global Quote": {"05. price": "210.5"}, "clean": 1}
    out = _tool_call("derive_ni_paths", ctx, {"sample": sample})
    assert any("Global Quote" in u for u in out["unaddressable"])
    # The addressable sibling still appears.
    assert any(entry["path"] == "clean" for entry in out["paths"])


def test_derive_ni_paths_accepts_json_string_via_programmatic_call() -> None:
    """The gated tool surface takes an object; programmatic callers (tests,
    future MCP tool) can pass a JSON-encoded string via the handler directly."""
    handler = tools.get_tool("derive_ni_paths").handler
    encoded = json.dumps({"a": {"b": 1}})
    # Bypass validate_args (its schema requires object at the surface). The
    # handler's own _coerce_sample accepts a JSON string.
    out = handler(tools.ToolContext(), {"sample": encoded})
    assert any(entry["path"] == "a.b" for entry in out["paths"])


def test_derive_ni_paths_caps_output_at_60_and_flags_truncated() -> None:
    ctx = tools.ToolContext()
    # A sample with many leaves that all survive the grammar filter.
    sample = {f"k{i}": i for i in range(120)}
    out = _tool_call("derive_ni_paths", ctx, {"sample": sample})
    assert len(out["paths"]) <= 60
    assert out["truncated"] is True


def test_derive_ni_paths_bare_list_root_reports_unaddressable() -> None:
    """A bare-list root cannot be extract-addressed (§4.1 requires a key first)."""
    # A list at the input root is legal input but §4.1 needs a key first.
    handler = tools.get_tool("derive_ni_paths").handler
    out = handler(tools.ToolContext(), {"sample": [{"a": 1}, {"a": 2}]})
    assert any("[0]" in u for u in out["unaddressable"])


def test_derive_ni_paths_rejects_oversize_sample() -> None:
    handler = tools.get_tool("derive_ni_paths").handler
    huge = "x" * (33 * 1024)
    with pytest.raises(ValueError):
        handler(tools.ToolContext(), {"sample": huge})


# --- journal: append / prune / cascade --------------------------------------

def test_append_journal_prunes_to_twenty_newest() -> None:
    ctx, _c, _k = _tool_ctx()
    out = _tool_call("create_ni_item", ctx, _basic_args(title="J"))
    for i in range(25):
        ctx.ni.append_journal(out["id"], "updated", f"tick {i}")
    entries = ctx.ni.read_journal(out["id"])
    # 20 is the ceiling — creating the item already wrote one; then 25 more —
    # only the newest 20 survive.
    assert len(entries) == 20
    assert entries[-1]["summary"] == "tick 24"


def test_journal_cascades_on_delete() -> None:
    ctx, _c, _k = _tool_ctx()
    out = _tool_call("create_ni_item", ctx, _basic_args(title="J2"))
    ctx.ni.append_journal(out["id"], "updated", "one")
    ctx.ni.delete(out["id"])
    # After delete the wildcard NIStore.delete cleaned the journal row too.
    assert ctx.ni.read_journal(out["id"]) == []


def test_append_journal_refuses_unknown_kind() -> None:
    ctx, _c, _k = _tool_ctx()
    out = _tool_call("create_ni_item", ctx, _basic_args(title="J3"))
    with pytest.raises(ValueError):
        ctx.ni.append_journal(out["id"], "no-such-kind", "hi")


def test_update_ni_item_journal_writes_changed_fields() -> None:
    ctx, _c, _k = _tool_ctx()
    iid = _tool_call("create_ni_item", ctx, _basic_args(title="Base"))["id"]
    _tool_call("update_ni_item", ctx, {"item_id": iid, "title": "Renamed"})
    entries = ctx.ni.read_journal(iid)
    # created + updated
    assert entries[-1]["kind"] == "updated"
    assert "title" in entries[-1]["summary"]


def test_update_ni_item_journal_writes_source_changed_kind_when_source_moves() -> None:
    ctx, _c, _k = _tool_ctx()
    iid = _tool_call("create_ni_item", ctx, _basic_args(title="Src"))["id"]
    _tool_call("update_ni_item", ctx,
               {"item_id": iid,
                "source": {"type": "model", "instruction": "new instruction"}})
    entries = ctx.ni.read_journal(iid)
    assert entries[-1]["kind"] == "source_changed"


# --- read_ni_item exposes journal + state_explanation + last_failure --------

def test_read_ni_item_returns_journal_and_state_explanation_for_draft_no_secret() -> None:
    ctx, _c, _k = _tool_ctx()
    out = _tool_call("create_ni_item", ctx, _basic_args(title="RS", draft=True))
    read = _tool_call("read_ni_item", ctx, {"item_id": out["id"]})
    assert read["state"] == "draft"
    assert "DRAFT" in read["state_explanation"]
    assert "Activate" in read["user_next_action"]
    assert read["journal"], "journal must ride the response"


def test_read_ni_item_explanation_for_draft_with_secret_names_the_missing_key() -> None:
    ctx, _c, _k = _tool_ctx()
    out = _tool_call("create_ni_item", ctx, _basic_args(
        title="Keyed quote",
        params={"api_key": {"label": "Finnhub API key", "kind": "secret",
                             "value": "ni:self:api_key"}},
        source={"type": "http_page",
                "url": "https://finnhub.io/api/v1/quote",
                "headers": {"X-Finnhub-Token": {"$secret": "ni:self:api_key"}}},
    ))
    read = _tool_call("read_ni_item", ctx, {"item_id": out["id"]})
    assert read["state"] == "draft"
    # The param label rides through to the user_next_action.
    assert "Finnhub API key" in read["user_next_action"]
    assert "do not describe this card as live" in read["state_explanation"]


def test_read_ni_item_explanation_for_live_and_broken_and_failing() -> None:
    ctx, _c, _k = _tool_ctx()
    out = _tool_call("create_ni_item", ctx, _basic_args(title="States"))
    ctx.ni.set_state(out["id"], "live")
    read = _tool_call("read_ni_item", ctx, {"item_id": out["id"]})
    assert "LIVE" in read["state_explanation"]
    ctx.ni.set_state(out["id"], "broken")
    read2 = _tool_call("read_ni_item", ctx, {"item_id": out["id"]})
    assert "BROKEN" in read2["state_explanation"]
    ctx.ni.set_state(out["id"], "failing")
    ctx.ni.bump_failure(out["id"], "extract_miss")
    ctx.ni.bump_failure(out["id"], "extract_miss")
    ctx.ni.bump_failure(out["id"], "extract_miss")
    read3 = _tool_call("read_ni_item", ctx, {"item_id": out["id"]})
    assert "failed" in read3["state_explanation"]
    assert "L1 self-repair" in read3["user_next_action"]


def test_read_ni_item_explanation_for_commissioning_with_failure() -> None:
    ctx, _c, _k = _tool_ctx()
    out = _tool_call("create_ni_item", ctx, _basic_args(title="Fx"))
    ctx.ni.set_state(out["id"], "commissioning")
    ctx.ni.bump_failure(out["id"], "extract_miss")
    read = _tool_call("read_ni_item", ctx, {"item_id": out["id"]})
    assert "first run failed" in read["state_explanation"]
    assert "NOT live" in read["state_explanation"]


def test_read_ni_item_exposes_last_failure_excerpt_for_http_source() -> None:
    """§27: an http-source item with a sealed last_failure snapshot gets the
    real excerpt back on read (feeds the fix conversation)."""
    ctx, _c, _k = _tool_ctx()
    # Author an http_json item and simulate a sealed last_failure snapshot.
    args = _basic_args(
        title="HTTP failure",
        source={"type": "http_page", "url": "https://api.example.com/x"},
        pipeline=[{"op": "extract", "paths": {"v": "a"}}],
        preview_payload={"v": 1},
        scene={"type": "stack", "dir": "v", "gap": "sm", "children": [
            {"type": "number", "value": {"$bind": "v"}, "format": "plain",
             "tone": "default", "size": "md"},
        ]},
    )
    out = _tool_call("create_ni_item", ctx, args)
    nimod._seal_last_failure_snapshot(
        ctx.ni, out["id"], nimod.NIError("extract_miss", "paths.v missing"),
        raw_excerpt='{"b": 2}',
    )
    read = _tool_call("read_ni_item", ctx, {"item_id": out["id"]})
    assert read["last_failure"] is not None
    assert read["last_failure"]["class"] == "extract_miss"
    assert read["last_failure"]["excerpt"] == '{"b": 2}'


def test_read_ni_item_last_failure_none_when_slot_absent() -> None:
    ctx, _c, _k = _tool_ctx()
    out = _tool_call("create_ni_item", ctx, _basic_args(title="No fail"))
    read = _tool_call("read_ni_item", ctx, {"item_id": out["id"]})
    assert read["last_failure"] is None


# --- board rows carry needs_credentials -------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "recipes.duckdb"))
    from smartbrain_3000.main import create_app
    with TestClient(create_app()) as test_client:
        yield test_client


def _unlock(client: TestClient) -> None:
    r = client.post("/api/account/setup", json={"passphrase": "correct-horse-battery-staple"})
    assert r.status_code == 200, r.text


def _install_recipe_via_flow(client: TestClient, recipe_id: str,
                              request_text: str, intent: dict) -> str:
    """One-door law (2026-09-14): recipes install ONLY through the flow's
    confirm_source pause — drive that path on the app's live store."""
    from smartbrain_3000 import ni_flow
    store = client.app.state.ni
    recipe = ni_catalog.get_recipe(recipe_id)
    assert recipe is not None, recipe_id
    item_id = ni_flow.create_shell_item(store, request_text)
    record = ni_flow._make_record(request_text, "confirm_source",
                                   source_url=recipe["url_template"],
                                   notes=["test"])
    record["_recipe_id"] = recipe_id
    record["intent"] = intent
    ni_flow._flow_write(store, item_id, record)
    result = ni_flow.continue_from_recipe_confirm(store, item_id,
                                                    recipe["url_template"])
    # keyed recipes settle awaiting_credential; keyless settle ready
    assert result.get("state") in ("ready", "awaiting_credential"), result
    return item_id


def test_board_row_needs_credentials_list_for_keyed_recipe(client: TestClient) -> None:
    _unlock(client)
    iid = _install_recipe_via_flow(client, "stock-quote-finnhub",
                                    "show me AAPL stock",
                                    {"subject": "AAPL", "cadence_minutes": 30})
    board = client.get("/api/ni/board").json()
    row = next(i for i in board["items"] if i["id"] == iid)
    assert row["state"] == "draft"
    assert row["needs_credentials"] == [
        {"name": "api_key", "label": "Finnhub API key"},
    ]


def test_board_row_needs_credentials_empty_after_credential_written(
        client: TestClient) -> None:
    _unlock(client)
    iid = _install_recipe_via_flow(client, "stock-quote-finnhub",
                                    "show me AAPL stock",
                                    {"subject": "AAPL", "cadence_minutes": 30})
    # Desktop-local PUT writes the credential; needs_credentials empties.
    r = client.put(f"/api/ni/items/{iid}/credential",
                   json={"name": "api_key", "value": "sk-xyz",
                         "host": "finnhub.io"},
                   headers={"X-SB-Local": "1"})
    assert r.status_code == 200, r.text
    board = client.get("/api/ni/board").json()
    row = next(i for i in board["items"] if i["id"] == iid)
    assert row["needs_credentials"] == []


def test_board_row_needs_credentials_empty_for_keyless_recipe(
        client: TestClient) -> None:
    _unlock(client)
    iid = _install_recipe_via_flow(client, "fx-usd-eur",
                                    "usd to eur rate",
                                    {"subject": "USD/EUR", "cadence_minutes": 60})
    board = client.get("/api/ni/board").json()
    row = next(i for i in board["items"] if i["id"] == iid)
    assert row["needs_credentials"] == []


# --- MED (recipe_url resolver): parked from_recipe tile carries the host -----

def test_resolve_pending_recipe_source_known_recipe_returns_url_template() -> None:
    """§26: a parked create_ni_item_from_recipe tile carries the recipe's
    ``url_template`` as ``recipe_url`` so the consent surface can show the host
    the card would call (the args themselves only carry the recipe id + params).
    """
    from smartbrain_3000 import agent_routes
    recipe = ni_catalog.get_recipe("fx-usd-eur")
    assert recipe is not None
    row = {"id": "p1", "tool": "create_ni_item_from_recipe",
           "args": {"recipe_id": "fx-usd-eur"}}
    assert (agent_routes._resolve_pending_recipe_source(row)
            == recipe["url_template"])


def test_resolve_pending_recipe_source_unknown_recipe_is_none() -> None:
    """§26: an unknown recipe id returns None — the tile still renders."""
    from smartbrain_3000 import agent_routes
    row = {"id": "p1", "tool": "create_ni_item_from_recipe",
           "args": {"recipe_id": "no-such-recipe"}}
    assert agent_routes._resolve_pending_recipe_source(row) is None


def test_resolve_pending_recipe_source_none_for_other_tools() -> None:
    """§26: a parked non-from_recipe tool never yields a recipe_url."""
    from smartbrain_3000 import agent_routes
    row = {"id": "p1", "tool": "web_fetch",
           "args": {"url": "https://example.com"}}
    assert agent_routes._resolve_pending_recipe_source(row) is None
    other = {"id": "p2", "tool": "create_ni_item",
             "args": {"source": {"type": "http_json", "url": "https://x"}}}
    assert agent_routes._resolve_pending_recipe_source(other) is None


def test_pending_tile_carries_recipe_url_when_passed() -> None:
    """The tile shape gets ``recipe_url`` as a first-class field."""
    from smartbrain_3000 import agent_routes
    row = {"id": "p1", "tool": "create_ni_item_from_recipe", "tier": "reviewed",
           "created_at": "t", "args": {"recipe_id": "fx-usd-eur"}}
    tile = agent_routes._pending_tile(row, recipe_url="https://api.example/x")
    assert tile["recipe_url"] == "https://api.example/x"


def test_pending_tile_recipe_url_defaults_none() -> None:
    """The tile shape always carries ``recipe_url`` (None for non-recipe rows)."""
    from smartbrain_3000 import agent_routes
    row = {"id": "p1", "tool": "web_fetch", "tier": "reviewed",
           "created_at": "t", "args": {"url": "https://example.com"}}
    tile = agent_routes._pending_tile(row)
    assert tile["recipe_url"] is None


# --- MED (derive_ni_paths): silent-starvation regressions --------------------

def test_derive_ni_paths_wide_pad_sample_returns_real_leaves_or_truncates() -> None:
    """Audit repro: a payload with ~200 pad keys + 2 real leaves must NOT return
    ``paths == [] and truncated is False`` — the walker's earlier ceiling of
    _DERIVE_MAX_CANDIDATES * 8 iterations starved silently on this shape."""
    ctx = tools.ToolContext()
    pad = {f"pad{i}": None for i in range(200)}
    sample = {"outer": {**pad, "real_price": 42, "real_vol": 7}}
    out = _tool_call("derive_ni_paths", ctx, {"sample": sample})
    # The starvation signature was BOTH conditions true simultaneously.
    assert not (len(out["paths"]) == 0 and out["truncated"] is False), out


def test_derive_ni_paths_under_budget_wide_sample_returns_all_leaves_untruncated() -> None:
    """A wide-but-under-budget shape still returns every leaf and truncated=False."""
    ctx = tools.ToolContext()
    sample = {f"k{i}": i for i in range(40)}  # well under the candidate cap + budget
    out = _tool_call("derive_ni_paths", ctx, {"sample": sample})
    paths = {entry["path"] for entry in out["paths"]}
    assert paths == {f"k{i}" for i in range(40)}
    assert out["truncated"] is False


# --- MED (derive_ni_paths): sample_json sibling ------------------------------

def test_derive_ni_paths_via_sample_json_gate_and_validate_args() -> None:
    """The gated ``sample_json`` sibling accepts a JSON string through
    validate_args (schema type ``string``) and returns the same leaves as the
    object-shaped ``sample`` arg."""
    ctx = tools.ToolContext()
    encoded = json.dumps({"a": {"b": 1}})
    out = _tool_call("derive_ni_paths", ctx, {"sample_json": encoded})
    assert any(entry["path"] == "a.b" for entry in out["paths"])


def test_derive_ni_paths_both_args_present_raises() -> None:
    """Exactly-one-of rule: passing both is refused at the handler."""
    ctx = tools.ToolContext()
    with pytest.raises(ValueError) as exc:
        _tool_call("derive_ni_paths", ctx,
                   {"sample": {"a": 1}, "sample_json": json.dumps({"a": 1})})
    assert "exactly one" in str(exc.value)


def test_derive_ni_paths_neither_arg_present_raises() -> None:
    """Exactly-one-of rule: passing neither is refused at the handler."""
    ctx = tools.ToolContext()
    with pytest.raises(ValueError) as exc:
        _tool_call("derive_ni_paths", ctx, {})
    assert "required" in str(exc.value)


# --- LOW (journal origin field) ---------------------------------------------

def test_journal_entry_carries_origin_field_system_for_code_composed() -> None:
    """§28: every code-composed entry lands with ``origin: 'system'``."""
    ctx, _c, _k = _tool_ctx()
    out = _tool_call("create_ni_item", ctx, _basic_args(title="Origin"))
    journal = ctx.ni.read_journal(out["id"])
    assert journal and journal[-1]["origin"] == "system"
    assert journal[-1]["kind"] == "created"


def test_journal_entry_c2_wrong_marked_as_user_origin() -> None:
    """§28: the user's C2 note lands with ``origin: 'user'`` so a later reader
    can distinguish the human authorship from code-composed entries."""
    ctx, _c, _k = _tool_ctx()
    out = _tool_call("create_ni_item", ctx, _basic_args(title="C2"))
    ctx.ni.append_journal(out["id"], "c2_wrong", "chart is off")
    journal = ctx.ni.read_journal(out["id"])
    assert journal[-1]["kind"] == "c2_wrong"
    assert journal[-1]["origin"] == "user"


def test_read_ni_item_returns_journal_entries_with_origin_field() -> None:
    """The tool surface passes ``origin`` through verbatim (read_journal is
    the source of truth)."""
    ctx, _c, _k = _tool_ctx()
    out = _tool_call("create_ni_item", ctx, _basic_args(title="Rd"))
    ctx.ni.append_journal(out["id"], "c2_wrong", "wrong")
    read = _tool_call("read_ni_item", ctx, {"item_id": out["id"]})
    origins = {entry["origin"] for entry in read["journal"]}
    assert origins == {"system", "user"}


# --- geocode-consent (2026-09-15, operator-approved) --------------------------

def _kc_geocode_fixture() -> dict:
    import pathlib
    fx = pathlib.Path(__file__).parent / "fixtures" / "ni_flow" / "geocode_kc.json"
    return json.loads(fx.read_text())


def test_weather_recipe_declares_geocode_fills() -> None:
    """The catalog validator accepted the weather recipe's geocode_fills map."""
    recipe = ni_catalog.get_recipe("weather-open-meteo")
    assert recipe is not None
    assert recipe["geocode_fills"] == {"latitude": "latitude",
                                        "longitude": "longitude"}


def test_geocode_place_resolves_recorded_fixture_and_degrades() -> None:
    from smartbrain_3000 import ni_flow
    fixture = _kc_geocode_fixture()
    out = ni_flow._geocode_place("Kansas City", lambda url: fixture)
    assert out is not None
    assert isinstance(out["latitude"], (int, float))
    assert isinstance(out["longitude"], (int, float))
    # Empty results / transport failure both degrade to None, never raise.
    assert ni_flow._geocode_place("x", lambda url: {"results": []}) is None
    def _boom(url):
        raise OSError("down")
    assert ni_flow._geocode_place("x", _boom) is None


def test_confirm_pause_stamps_geocode_disclosure_only_with_place() -> None:
    """The sealed record discloses the lookup ONLY when the recipe declares
    fills, a target slot is empty, and the intent names a place."""
    from smartbrain_3000 import ni_flow
    ctx, _c, _k = _tool_ctx()
    recipe = ni_catalog.get_recipe("weather-open-meteo")
    # With a place: stamped.
    iid = ni_flow.create_shell_item(ctx.ni, "weather in Kansas City")
    ni_flow._pause_for_recipe_confirm(
        ctx.ni, iid, {"subject": "KC weather", "place": "Kansas City",
                      "cadence_minutes": 15}, recipe)
    rec = ni_flow._flow_read(ctx.ni, iid)
    assert rec["_geocode"] == {"query": "Kansas City",
                                "host": "geocoding-api.open-meteo.com"}
    # Without a place: not stamped.
    iid2 = ni_flow.create_shell_item(ctx.ni, "weather please")
    ni_flow._pause_for_recipe_confirm(
        ctx.ni, iid2, {"subject": "weather", "place": None,
                       "cadence_minutes": 15}, recipe)
    rec2 = ni_flow._flow_read(ctx.ni, iid2)
    assert "_geocode" not in rec2


def test_confirm_executes_disclosed_lookup_and_lands_commissioning() -> None:
    """The user's single confirm covers both fetches: coordinates fill from the
    recorded geocode fixture and the keyless weather card lands commissioning
    (no unfilled slots left)."""
    from smartbrain_3000 import ni_flow
    ctx, _c, _k = _tool_ctx()
    recipe = ni_catalog.get_recipe("weather-open-meteo")
    iid = ni_flow.create_shell_item(ctx.ni, "weather in Kansas City")
    ni_flow._pause_for_recipe_confirm(
        ctx.ni, iid, {"subject": "KC weather", "place": "Kansas City",
                      "cadence_minutes": 15}, recipe)
    fixture = _kc_geocode_fixture()
    result = ni_flow.continue_from_recipe_confirm(
        ctx.ni, iid, recipe["url_template"], fetcher=lambda url: fixture)
    assert result.get("state") == "ready", result
    item = ctx.ni.get_item(iid)
    assert item["state"] == "commissioning"
    assert nimod.unfilled_referenced_params(item["spec"]) == []
    lat = item["spec"]["params"]["latitude"]["value"]
    assert isinstance(lat, (int, float))


def test_confirm_geocode_failure_degrades_to_awaiting_params() -> None:
    """A failed lookup leaves the slots empty ON PURPOSE — draft +
    awaiting_params, the card's Fill affordance asks; never a guessed value."""
    from smartbrain_3000 import ni_flow
    ctx, _c, _k = _tool_ctx()
    recipe = ni_catalog.get_recipe("weather-open-meteo")
    iid = ni_flow.create_shell_item(ctx.ni, "weather in Kansas City")
    ni_flow._pause_for_recipe_confirm(
        ctx.ni, iid, {"subject": "KC weather", "place": "Kansas City",
                      "cadence_minutes": 15}, recipe)
    def _boom(url):
        raise OSError("down")
    result = ni_flow.continue_from_recipe_confirm(
        ctx.ni, iid, recipe["url_template"], fetcher=_boom)
    assert result.get("state") == "awaiting_params", result
    item = ctx.ni.get_item(iid)
    assert item["state"] == "draft"
    assert "latitude" in nimod.unfilled_referenced_params(item["spec"])


def test_confirm_tool_enforces_geocode_query_display() -> None:
    """Consent completeness: the confirm tool REFUSES when the sealed record
    discloses a lookup but the args (= what the approval card displayed) do
    not echo it verbatim; a stray geocode_query with no pending lookup is
    refused too."""
    from smartbrain_3000 import ni_flow
    ctx, _c, _k = _tool_ctx()
    recipe = ni_catalog.get_recipe("weather-open-meteo")
    iid = ni_flow.create_shell_item(ctx.ni, "weather in Kansas City")
    ni_flow._pause_for_recipe_confirm(
        ctx.ni, iid, {"subject": "KC weather", "place": "Kansas City",
                      "cadence_minutes": 15}, recipe)
    tool = tools.get_tool("confirm_ni_flow_source")
    with pytest.raises(ValueError, match="Kansas City"):
        tool.handler(ctx, {"item_id": iid, "source_url": recipe["url_template"]})
    # Stray geocode_query on a flow with NO pending lookup is refused.
    iid2 = ni_flow.create_shell_item(ctx.ni, "bitcoin price")
    btc = ni_catalog.get_recipe("crypto-price-btc-usd")
    ni_flow._pause_for_recipe_confirm(
        ctx.ni, iid2, {"subject": "BTC", "place": None, "cadence_minutes": 15}, btc)
    with pytest.raises(ValueError, match="no pending place lookup"):
        tool.handler(ctx, {"item_id": iid2, "source_url": btc["url_template"],
                           "geocode_query": "Kansas City"})
    # The matching echo proceeds (lookup via injected... the tool uses netguard;
    # the degrade path still completes the flow honestly).
    fixture = _kc_geocode_fixture()
    import smartbrain_3000.ni_flow as flowmod
    orig = flowmod._netguard_mod.safe_fetch_json
    try:
        flowmod._netguard_mod.safe_fetch_json = lambda url: fixture
        out = tool.handler(ctx, {"item_id": iid, "source_url": recipe["url_template"],
                                 "geocode_query": "Kansas City"})
    finally:
        flowmod._netguard_mod.safe_fetch_json = orig
    assert out["state"] == "ready"
    item = ctx.ni.get_item(iid)
    assert item["state"] == "commissioning"
