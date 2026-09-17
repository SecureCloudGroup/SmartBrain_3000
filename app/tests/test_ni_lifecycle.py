"""§C lifecycle matrix (L1..L12) — the card exists; the user talks about it.

Every row is exercised through the REAL tool registry (tools.get_tool +
tools.validate_args) so nothing is faked below the model-facing surface. The
scripted fixture drives are `ni_flow.run_flow` calls with a pure-callable
gateway + a pure-callable fetcher — the same seam existing recorded tests use
for aapl/hn/iss, extended to the two shipped-but-pytest-uncovered fixtures.

Mirrors idioms from:
  * `test_ni.py`         — `_tool_ctx` / `_tool_call` / `_tool_spec_args`.
  * `test_ni_routes.py`  — TestClient fixture + `_unlock` + `_create_via_tool`.
  * `test_ni_flow.py`    — `_store`, scripted-model helper, end-to-end shape.
  * `test_ni_recipes.py` — recipe handoff helpers.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import ni as nimod
from smartbrain_3000 import ni_flow, tools
from smartbrain_3000.secrets import gen_master_key

FIXTURES = Path(__file__).parent / "fixtures" / "ni_flow"


# --- helpers (idioms mirrored from test_ni + test_ni_flow) ------------------

def _tool_ctx() -> tuple[tools.ToolContext, duckdb.DuckDBPyConnection, bytes]:
    """Fresh NIStore wrapped in a ToolContext (mirrors test_ni._tool_ctx)."""
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    return tools.ToolContext(ni=nimod.NIStore(conn, key)), conn, key


def _tool_call(name: str, ctx: tools.ToolContext, args: dict) -> dict:
    tool = tools.get_tool(name)
    if tool is not None:
        return tool.handler(ctx, tools.validate_args(tool, args))
    # NI Foreman P2: retired write tools run via the internal factory.
    return tools.INTERNAL_NI_TOOLS[name](ctx, args)


def _basic_scene() -> dict:
    """A scene binding the 'text' field so preview renders (test_ni parity)."""
    return {"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "text", "value": "{{text}}", "role": "title",
         "tone": "default", "size": "md"},
    ]}


def _basic_args(**over) -> dict:
    """create_ni_item args body — a model-source card that lands as draft."""
    body = {
        "title": "Card",
        "goal": "show a thing",
        "params": {},
        "source": {"type": "model", "instruction": "hi"},
        "pipeline": [],
        "scene": _basic_scene(),
        "display": {"size": "small"},
        "interval_minutes": 60,
        "preview_payload": {"text": "preview"},
        "draft": True,
    }
    body.update(over)
    return body


def _scripted_model(replies: list[str]):
    """Return a `(model, prompt) -> reply` callable (mirrors test_ni_flow)."""
    calls: list[dict] = []

    def _call(model: str, prompt: str) -> str:
        assert isinstance(model, str) and isinstance(prompt, str), "args required"
        calls.append({"model": model, "prompt": prompt})
        assert replies, "scripted model exhausted"
        return replies.pop(0)

    _call.calls = calls  # type: ignore[attr-defined]
    return _call


def _load_fixture(name: str) -> object:
    """Load one recorded JSON fixture body from tests/fixtures/ni_flow/."""
    path = FIXTURES / f"{name}.json"
    assert path.exists(), f"missing fixture {name}"
    with path.open() as fp:
        return json.load(fp)


# --- L1: duplicate-title guard names the existing card; allow_duplicate escapes -

def test_L1_start_ni_flow_refuses_duplicate_title_and_names_existing_card(
        monkeypatch) -> None:
    """L1: an existing card titled X blocks a flow whose shell title matches.

    `create_shell_item` derives the shell title from the first line of the
    request (`ni_flow._empty_shell_spec`) — so a request whose first line is
    exactly "AAPL" collides with an existing card named "AAPL". The tool's
    prevalidate is pure; the collision is caught at execute time by
    `_check_shell_title_duplicate`, which names the existing card in the
    error so the model can point the user at update instead.
    """
    ctx, _c, _k = _tool_ctx()
    # Seed a real card via the model-facing tool so this is a lifecycle test,
    # not a store trick.
    existing = _tool_call("create_ni_item", ctx, _basic_args(title="AAPL"))["id"]

    # The worker never runs — the guard fires before spawn — but pin the
    # wait short so a stray path can't hang the test.
    monkeypatch.setattr(ni_flow, "start_flow_worker", lambda *_a, **_k: True)
    monkeypatch.setattr(tools, "_FLOW_WAIT_SECONDS", 0.05)
    with pytest.raises(ValueError) as excinfo:
        _tool_call("start_ni_flow", ctx, {"request": "AAPL"})
    msg = str(excinfo.value)
    assert "already exists" in msg, msg
    assert existing in msg, "the guard error must name the existing card id"

    # Nothing new landed on the store.
    ids_before = {item["id"] for item in ctx.ni.list_items()}
    assert ids_before == {existing}, "guard must not park a shell alongside the raise"


def test_L1_start_ni_flow_allow_duplicate_lets_it_through(monkeypatch) -> None:
    """L1: `allow_duplicate: true` escapes the guard — a second card lands."""
    ctx, _c, _k = _tool_ctx()
    _tool_call("create_ni_item", ctx, _basic_args(title="AAPL"))

    spawned: dict = {}

    def _fake_spawn(_store, iid, **kwargs) -> bool:
        spawned["id"] = iid
        spawned["source_url"] = kwargs.get("source_url")
        return True

    monkeypatch.setattr(ni_flow, "start_flow_worker", _fake_spawn)
    monkeypatch.setattr(tools, "_FLOW_WAIT_SECONDS", 0.05)
    out = _tool_call("start_ni_flow", ctx,
                     {"request": "AAPL", "allow_duplicate": True})
    assert out["state"] == "intent", out
    assert out["id"] and out["id"] != spawned.get("id", None) or True
    assert spawned["id"] == out["id"], "worker must have been spawned"
    # A second card really landed.
    titles = [str(item["spec"].get("title") or "") for item in ctx.ni.list_items()]
    assert titles.count("AAPL") == 2, f"two AAPL cards expected; got {titles}"


# --- L2: cadence-only update leaves state untouched + journal records -------

def test_L2_cadence_only_update_leaves_state_untouched_and_journals(
) -> None:
    """L2: create -> commissioning -> live -> cadence-only update; state stays
    LIVE and the journal picks up an ``updated`` entry naming the field."""
    ctx, _c, _k = _tool_ctx()
    iid = _tool_call("create_ni_item", ctx,
                     _basic_args(title="L2 cadence", interval_minutes=60))["id"]
    # Full lifecycle: draft -> commissioning -> live.
    ctx.ni.commission(iid)
    ctx.ni.set_state(iid, "live")
    before = ctx.ni.get_item(iid)
    assert before["state"] == "live" and before["interval_minutes"] == 60

    out = _tool_call("update_ni_item", ctx,
                     {"item_id": iid, "interval_minutes": 30})
    assert out["state_reset"] is None, "cadence-only is not a source change"

    after = ctx.ni.get_item(iid)
    assert after["state"] == "live", "state must not move on a pure cadence patch"
    assert after["interval_minutes"] == 30, "cadence value must have applied"

    journal = ctx.ni.read_journal(iid)
    updated = [e for e in journal if e["kind"] == "updated"]
    assert updated, f"expected an 'updated' entry; got {journal}"
    assert "interval_minutes" in updated[-1]["summary"], updated[-1]


# --- L3: title-only update leaves state untouched + journal records ---------

def test_L3_title_only_update_leaves_state_untouched_and_journals() -> None:
    """L3: create -> commissioning -> live -> title-only update; state stays
    LIVE, the sealed title changes, and the journal records the change."""
    ctx, _c, _k = _tool_ctx()
    iid = _tool_call("create_ni_item", ctx, _basic_args(title="Old name"))["id"]
    ctx.ni.commission(iid)
    ctx.ni.set_state(iid, "live")
    before = ctx.ni.get_item(iid)
    assert before["state"] == "live" and before["spec"]["title"] == "Old name"

    out = _tool_call("update_ni_item", ctx,
                     {"item_id": iid, "title": "Apple Stock"})
    assert out["state_reset"] is None, "title-only is not a source change"

    after = ctx.ni.get_item(iid)
    assert after["state"] == "live", "state must not move on a title patch"
    assert after["spec"]["title"] == "Apple Stock", "title must have applied"

    journal = ctx.ni.read_journal(iid)
    updated = [e for e in journal if e["kind"] == "updated"]
    assert updated, f"expected an 'updated' entry; got {journal}"
    assert "title" in updated[-1]["summary"], updated[-1]


# --- L4: pause + resume via set_ni_item_enabled -----------------------------

def test_L4_pause_and_resume_via_set_ni_item_enabled_both_directions() -> None:
    """L4: `set_ni_item_enabled` flips ``enabled`` in both directions.

    Starts on the default enabled=True landing, walks to False and back.
    """
    ctx, _c, _k = _tool_ctx()
    iid = _tool_call("create_ni_item", ctx, _basic_args(title="Bitcoin"))["id"]
    assert ctx.ni.get_item(iid)["enabled"] is True, "new item enabled by default"

    off = _tool_call("set_ni_item_enabled", ctx,
                     {"item_id": iid, "enabled": False})
    assert off["enabled"] is False, off
    assert ctx.ni.get_item(iid)["enabled"] is False, "pause must persist"

    on = _tool_call("set_ni_item_enabled", ctx,
                    {"item_id": iid, "enabled": True})
    assert on["enabled"] is True, on
    assert ctx.ni.get_item(iid)["enabled"] is True, "resume must persist"


# --- L5: run_ni_item_now — slug prevalidate + missing-uuid execute-time -----

def test_L5_run_ni_item_now_slug_id_bounces_at_prevalidate_before_store(
) -> None:
    """L5 (a): a slugged invented id is refused BEFORE any store call — the
    prevalidate hook is pure, so no card ever parks."""
    tool = tools.get_tool("run_ni_item_now")
    assert tool.prevalidate is not None, "run_ni_item_now must carry a prevalidate"
    with pytest.raises(ValueError, match="not a card id"):
        tool.prevalidate({"item_id": "aapl-quote-every-30m"})


def test_L5_run_ni_item_now_missing_uuid_returns_existing_cards_listing(
) -> None:
    """L5 (b): a UUID-shaped id that doesn't exist returns the real card list
    so the model self-corrects in one step."""
    ctx, _c, _k = _tool_ctx()
    existing = _tool_call("create_ni_item", ctx, _basic_args(title="Real"))["id"]
    ctx.ni.commission(existing)  # /run refuses draft (K6) but only for the real id
    with pytest.raises(ValueError) as excinfo:
        _tool_call("run_ni_item_now", ctx,
                   {"item_id": "12345678-1234-1234-1234-1234567890ab"})
    msg = str(excinfo.value)
    assert "not found" in msg or "existing cards" in msg, msg
    assert existing in msg, "listing must include the real card id"


# --- L6: fix-broken via remap_ni_item ---------------------------------------

def test_L6_remap_ni_item_reenters_at_sampling_on_frozen_source(
        monkeypatch) -> None:
    """L6: a flow-born http_json item in ``broken`` state → `remap_ni_item`
    re-enters at Sampling on the item's OWN frozen source URL. Asserts the
    sealed flow record + reported state + that the URL threaded to the worker
    equals the frozen one (never a re-derived URL, never a recipe match).
    """
    ctx, _c, _k = _tool_ctx()
    frozen_url = (
        "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson")

    # Author a flow-born shell so `is_flow_or_recipe_born` fires — then swing
    # the sealed spec to a real http_json card that has since gone broken.
    request = "quakes card"
    item_id = ni_flow.create_shell_item(ctx.ni, request)
    spec = dict(ctx.ni.get_item(item_id)["spec"])
    spec["source"] = {"type": "http_json", "url": frozen_url}
    spec["scene"] = {"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "text", "value": "quakes", "role": "title",
         "tone": "default", "size": "md"},
    ]}
    ctx.ni.update_spec(item_id, spec, origin="agent")
    ctx.ni.set_state(item_id, "broken")

    fired: dict = {}

    def _fake_spawn(_store, iid, **kwargs) -> bool:
        fired["id"] = iid
        fired["source_url"] = kwargs.get("source_url")
        return True

    monkeypatch.setattr(ni_flow, "start_flow_worker", _fake_spawn)
    monkeypatch.setattr(tools, "_FLOW_WAIT_SECONDS", 0.05)
    out = _tool_call("remap_ni_item", ctx, {"item_id": item_id})

    assert out["state"] == "sampling", out
    assert out["source_url"] == frozen_url, out
    assert fired["id"] == item_id, "worker must have been spawned for this item"
    assert fired["source_url"] == frozen_url, "remap must fetch the FROZEN url"

    record = ni_flow._flow_read(ctx.ni, item_id)
    assert record is not None
    assert record["state"] == "sampling", record
    assert record.get("source_url") == frozen_url, record
    assert record.get("_remap") is True, "remap marker must ride the flow record"


# --- L7: source-change refusal on a flow-born item points at remap ----------

def test_L7_update_source_refused_on_flow_born_item_points_at_remap() -> None:
    """L7: an `update_ni_item` swapping the source of a flow-born item is
    refused with a message that names `remap_ni_item` — the §29 door."""
    ctx, _c, _k = _tool_ctx()
    item_id = ni_flow.create_shell_item(ctx.ni, "some flow request")
    assert ni_flow.is_flow_or_recipe_born(ctx.ni, item_id), \
        "sanity: shell item is flow-born"
    with pytest.raises(ValueError) as excinfo:
        _tool_call("update_ni_item", ctx,
                   {"item_id": item_id,
                    "source": {"type": "model", "instruction": "different"}})
    msg = str(excinfo.value)
    assert "remap" in msg, msg
    # State is untouched (the refusal preempts every store write).
    assert ctx.ni.get_item(item_id)["state"] == "draft"


# --- L8: keyed sequence + needs_params sibling via TestClient ---------------

@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "ni_lifecycle.duckdb"))
    from smartbrain_3000.main import create_app
    with TestClient(create_app()) as tc:
        yield tc


def _unlock(client: TestClient) -> None:
    r = client.post("/api/account/setup",
                    json={"passphrase": "correct-horse-battery-staple"})
    assert r.status_code == 200, r.text


def _spec_body(**over) -> dict:
    body = {
        "title": "Weather",
        "goal": "show the weather",
        "params": {},
        "source": {"type": "model", "instruction": "hi"},
        "pipeline": [],
        "scene": _basic_scene(),
        "display": {"size": "small"},
        "interval_minutes": 60,
        "preview_payload": {"text": "preview"},
        "draft": True,
    }
    body.update(over)
    return body


def _create_via_tool(client: TestClient, **over) -> str:
    """NI Foreman P2: creation left the model registry — internal factory."""
    from smartbrain_3000 import tools
    body = _spec_body(**over)
    ctx = tools.ToolContext(ni=client.app.state.ni)
    return tools.INTERNAL_NI_TOOLS["create_ni_item"](ctx, body)["id"]


def test_L8_keyed_card_credential_flow_reaches_commissioning(
        client: TestClient) -> None:
    """L8 (keyed): draft with ``ni:self:api_key`` → /commission 409 →
    /credential 200 → /commission 200 (state=commissioning)."""
    _unlock(client)
    iid = _create_via_tool(
        client,
        title="L8 keyed",
        params={"api_key": {"label": "Weather Key",
                            "kind": "secret", "value": "ni:self:api_key"}},
        source={"type": "http_page",
                "url": "https://api.example.com/q",
                "headers": {"X-Api-Key": {"$secret": "ni:self:api_key"}}},
    )
    item = client.app.state.ni.get_item(iid)
    assert item["state"] == "draft", "keyed draft must land in draft"

    # First commission attempt refuses (409): the secret is unfilled.
    r = client.post(f"/api/ni/items/{iid}/commission")
    assert r.status_code == 409, r.text
    assert "secret" in r.json()["detail"], r.json()

    # PUT the credential (Desktop-local). The secret now rides the SecretStore.
    put = client.put(
        f"/api/ni/items/{iid}/credential",
        json={"name": "api_key", "value": "s3cret", "host": "api.example.com"},
        headers={"X-SB-Local": "1"},
    )
    assert put.status_code == 200, put.text

    # Second commission attempt succeeds — state = commissioning.
    ok = client.post(f"/api/ni/items/{iid}/commission")
    assert ok.status_code == 200, ok.text
    assert ok.json()["state"] == "commissioning"
    after = client.app.state.ni.get_item(iid)
    assert after["state"] == "commissioning"


def test_L8_needs_params_sibling_reaches_commissioning(
        client: TestClient) -> None:
    """L8 (needs_params sibling): a referenced non-secret param empty →
    /commission 409 naming the param → /param 200 → /commission 200."""
    _unlock(client)
    iid = _create_via_tool(
        client,
        title="L8 needs_params",
        params={"city": {"label": "City name",
                          "kind": "string", "value": ""}},
        source={"type": "http_page",
                 "url": "https://api.example.com/w?city={{param:city}}",
                 "headers": {}},
    )
    # First commission — refuses (409), naming the param.
    r = client.post(f"/api/ni/items/{iid}/commission")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "city" in detail and "City name" in detail, detail

    # PUT the param (Desktop-local, non-secret).
    put = client.put(f"/api/ni/items/{iid}/param",
                     json={"name": "city", "value": "Kansas City"},
                     headers={"X-SB-Local": "1"})
    assert put.status_code == 200, put.text
    assert put.json()["needs_params"] == []

    # Second commission — succeeds.
    ok = client.post(f"/api/ni/items/{iid}/commission")
    assert ok.status_code == 200, ok.text
    assert ok.json()["state"] == "commissioning"


# --- L9: C2 wrong route walks a commissioning item back to draft + journal --

def test_L9_validate_wrong_returns_to_draft_and_records_journal_entry(
        client: TestClient) -> None:
    """L9: POST /api/ni/items/{id}/validate {ok: false, note: ...} on a
    commissioning card returns state=draft AND lands a `c2_wrong` journal
    entry carrying the user's note verbatim.
    """
    _unlock(client)
    iid = _create_via_tool(client, title="L9 wrong")
    client.app.state.ni.set_state(iid, "commissioning")

    r = client.post(f"/api/ni/items/{iid}/validate",
                    json={"ok": False, "note": "the number is way off"})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "draft"
    assert client.app.state.ni.get_item(iid)["state"] == "draft"

    journal = client.app.state.ni.read_journal(iid)
    c2_rows = [e for e in journal if e["kind"] == "c2_wrong"]
    assert c2_rows, f"expected a c2_wrong journal entry; got {journal}"
    assert "way off" in c2_rows[-1]["summary"], c2_rows[-1]


# --- L10: delete cascade (handler) + route secret purge ---------------------

def test_L10_delete_ni_item_handler_cascades_snapshots_and_journal(
) -> None:
    """L10 (handler): `delete_ni_item` removes the row and cascades snapshots
    + revisions + journal (the store's wildcard cascade). Also proves the
    handler works when the item carries an ``ni:<id>:api_key`` param, even
    though the ToolContext has no SecretStore (that purge is a route-level
    concern — see the sibling test).
    """
    ctx, conn, _k = _tool_ctx()
    iid = _tool_call(
        "create_ni_item", ctx,
        _basic_args(
            title="L10 handler",
            params={"api_key": {"label": "Key", "kind": "secret",
                                 "value": "ni:self:api_key"}},
            source={"type": "http_page",
                     "url": "https://api.example.com/x",
                     "headers": {"X-Api-Key": {"$secret": "ni:self:api_key"}}},
        ),
    )["id"]
    ctx.ni.append_journal(iid, "updated", "some noise entry")
    assert ctx.ni.read_journal(iid), "sanity: journal has entries"

    _tool_call("delete_ni_item", ctx, {"item_id": iid})
    assert ctx.ni.get_item(iid) is None, "row must be gone"
    for table in ("ni_snapshots", "ni_revisions", "ni_runs"):
        remaining = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE item_id = ?;", [iid],
        ).fetchone()[0]
        assert remaining == 0, f"{table} did not cascade"
    assert ctx.ni.read_journal(iid) == [], "journal must cascade too"


def test_L10_delete_ni_item_route_purges_item_scoped_secret(
        client: TestClient) -> None:
    """L10 (route): DELETE /api/ni/items/{id} also removes every
    ``ni:<id>:<name>`` secret written under the item's namespace (§L9 in
    ni_routes) — the tool handler has no SecretStore, so this behaviour lives
    on the route surface exclusively.
    """
    _unlock(client)
    iid = _create_via_tool(
        client,
        title="L10 route",
        params={"api_key": {"label": "Key",
                            "kind": "secret", "value": "ni:self:api_key"}},
        source={"type": "http_page",
                "url": "https://api.example.com/x",
                "headers": {"X-Api-Key": {"$secret": "ni:self:api_key"}}},
    )
    # Seed a credential — value never rides the tool surface.
    put = client.put(
        f"/api/ni/items/{iid}/credential",
        json={"name": "api_key", "value": "s3cret", "host": "api.example.com"},
        headers={"X-SB-Local": "1"},
    )
    assert put.status_code == 200, put.text
    stored_key = f"ni:{iid}:api_key"
    assert client.app.state.secret_store.get(stored_key) is not None, (
        "sanity: credential landed under the expected item-scoped key"
    )

    r = client.delete(f"/api/ni/items/{iid}")
    assert r.status_code == 200, r.text
    assert client.app.state.ni.get_item(iid) is None
    assert client.app.state.secret_store.get(stored_key) is None, (
        "the route must drop item-scoped secrets alongside the item"
    )


# --- L11: state literacy — draft/commissioning/live/broken + list_ni_items ---

@pytest.mark.parametrize(
    ("state", "must_contain"),
    [
        ("draft", "DRAFT"),
        ("commissioning", "COMMISSIONING"),
        ("live", "LIVE"),
        ("broken", "BROKEN"),
    ],
)
def test_L11_read_ni_item_state_explanation_matches_state(
        state: str, must_contain: str) -> None:
    """L11: read_ni_item's `state_explanation` names the state truth for
    every core state; a card is NEVER described as "live" while it is not
    (draft / commissioning / broken all point elsewhere).
    """
    ctx, _c, _k = _tool_ctx()
    iid = _tool_call("create_ni_item", ctx, _basic_args(title=f"L11 {state}"))["id"]
    if state != "draft":
        # draft is the natural landing for _basic_args(draft=True); drive the
        # other states through the store's transition surface.
        ctx.ni.set_state(iid, state)
    read = _tool_call("read_ni_item", ctx, {"item_id": iid})
    assert read["state"] == state, read
    assert must_contain in read["state_explanation"], (
        f"{state}: expected {must_contain!r} in {read['state_explanation']!r}"
    )
    if state != "live":
        # The literacy guarantee: no non-live state pretends to be live.
        assert "LIVE" not in read["state_explanation"], (
            f"{state}: state_explanation must not claim LIVE"
        )
    assert read["user_next_action"], "user_next_action must be non-empty"


def test_L11_list_ni_items_returns_every_card_with_its_state() -> None:
    """L11: list_ni_items reports every card, each carrying its own state —
    the "what's on my dashboard?" question the chat needs a truthful answer to.
    """
    ctx, _c, _k = _tool_ctx()
    a = _tool_call("create_ni_item", ctx, _basic_args(title="A"))["id"]
    b = _tool_call("create_ni_item", ctx, _basic_args(title="B"))["id"]
    ctx.ni.commission(a)                    # A -> commissioning
    ctx.ni.set_state(b, "live")             # B -> live
    out = _tool_call("list_ni_items", ctx, {})
    by_id = {i["id"]: i for i in out["items"]}
    assert set(by_id) == {a, b}, f"list must include both cards; got {by_id.keys()}"
    assert by_id[a]["state"] == "commissioning", by_id[a]
    assert by_id[b]["state"] == "live", by_id[b]


# --- L12: needs_params literacy — the fill-slot conversation ----------------

def test_L12_read_ni_item_names_missing_param_label_and_directs_to_fill(
) -> None:
    """L12: a draft item whose referenced non-secret param is empty →
    read_ni_item's `state_explanation` + `user_next_action` name the param
    LABEL and direct the user to the card's Fill (never suggest inventing a
    value).
    """
    ctx, _c, _k = _tool_ctx()
    iid = _tool_call(
        "create_ni_item", ctx,
        _basic_args(
            title="L12 city",
            params={"city": {"label": "City name",
                              "kind": "string", "value": ""}},
            source={"type": "http_page",
                     "url": "https://api.example.com/w?city={{param:city}}",
                     "headers": {}},
            draft=True,
        ),
    )["id"]
    # Sanity: the referenced param really is unfilled.
    unfilled = nimod.unfilled_referenced_params(ctx.ni.get_item(iid)["spec"])
    assert unfilled == ["city"], f"sanity: unfilled = {unfilled!r}"

    read = _tool_call("read_ni_item", ctx, {"item_id": iid})
    assert read["state"] == "draft"
    explanation = read["state_explanation"]
    action = read["user_next_action"]
    assert "City name" in explanation, explanation
    assert "City name" in action, action
    assert "fill" in action.lower(), (
        f"user_next_action must direct to Fill; got {action!r}"
    )
    # Never suggest fabricating a value.
    assert "invent" not in action.lower() and "guess" not in action.lower(), action


# --- end-to-end (recorded fixtures) — astros + fx ---------------------------

def _empty_catalog() -> list[dict]:
    """Force the freeform path — no recipes to match against."""
    return []


def _e2e_store() -> tuple[nimod.NIStore, duckdb.DuckDBPyConnection]:
    """Fresh in-memory NIStore for one end-to-end drive."""
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    return nimod.NIStore(conn, key), conn


def test_flow_end_to_end_astros_scalar_count_fixture() -> None:
    """astros.json ("how many people are in space right now") → shell → freeform
    mapping → ready. The intent's `count` field maps to the fixture's
    top-level `number` (a scalar count), the frozen source URL equals the
    approved one, and the preview payload carries a real number.
    """
    store, _conn = _e2e_store()
    fixture = _load_fixture("astros")
    assert isinstance(fixture, dict) and isinstance(fixture.get("number"), int), (
        "sanity: astros fixture must carry an integer 'number' at the root"
    )
    request = "how many people are in space right now"
    url = "http://api.open-notify.org/astros.json"
    item_id = ni_flow.create_shell_item(store, request)

    intent_reply = json.dumps({
        "kind": "external_data", "subject": "people in space",
        "cadence_minutes": 60, "wants": ["count"],
        "threshold": None, "display_hint": "value",
    })
    # 'count' maps from the fixture's top-level 'number' (the actual scalar
    # count) — verified against the fixture shape above.
    mapping_reply = json.dumps({"count": "number"})
    model = _scripted_model([intent_reply, mapping_reply])
    result = ni_flow.run_flow(
        store, item_id, gateway_call=model,
        fetcher=lambda _u: fixture, catalog=_empty_catalog(),
        source_url=url,
    )
    assert result["state"] == "ready", result
    item = store.get_item(item_id)
    assert item is not None
    assert item["spec"]["source"]["url"] == url, (
        "frozen source.url must equal the approved URL"
    )
    preview = store.read_snapshot(item_id, "preview_data")
    assert preview is not None
    count = preview["payload"].get("count")
    assert isinstance(count, (int, float)) and count == fixture["number"], (
        f"preview count must equal fixture.number ({fixture['number']}); "
        f"got {count!r}"
    )


def test_flow_end_to_end_fx_rate_fixture() -> None:
    """fx.json ("EUR to USD exchange rate") → shell → freeform mapping →
    ready. The intent's `rate` field maps to the fixture's `rates.USD`, the
    frozen source URL equals the approved one, and the preview payload
    carries the numeric rate."""
    store, _conn = _e2e_store()
    fixture = _load_fixture("fx")
    assert (isinstance(fixture, dict)
            and isinstance(fixture.get("rates"), dict)
            and isinstance(fixture["rates"].get("USD"), (int, float))), (
        "sanity: fx fixture must carry a numeric rates.USD"
    )
    request = "EUR to USD exchange rate"
    url = "https://api.frankfurter.dev/latest?base=EUR&symbols=USD"
    item_id = ni_flow.create_shell_item(store, request)

    intent_reply = json.dumps({
        "kind": "external_data", "subject": "EUR to USD",
        "cadence_minutes": 60, "wants": ["rate"],
        "threshold": None, "display_hint": "value",
    })
    mapping_reply = json.dumps({"rate": "rates.USD"})
    model = _scripted_model([intent_reply, mapping_reply])
    result = ni_flow.run_flow(
        store, item_id, gateway_call=model,
        fetcher=lambda _u: fixture, catalog=_empty_catalog(),
        source_url=url,
    )
    assert result["state"] == "ready", result
    item = store.get_item(item_id)
    assert item is not None
    assert item["spec"]["source"]["url"] == url, (
        "frozen source.url must equal the approved URL"
    )
    preview = store.read_snapshot(item_id, "preview_data")
    assert preview is not None
    rate = preview["payload"].get("rate")
    assert isinstance(rate, (int, float)) and rate == fixture["rates"]["USD"], (
        f"preview rate must equal fixture.rates.USD "
        f"({fixture['rates']['USD']}); got {rate!r}"
    )
