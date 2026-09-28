"""Deterministic-authoring backend (§26/§27/§28) tests.

Covers:
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
from smartbrain_3000 import tools
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
    if tool is not None:
        return tool.handler(ctx, tools.validate_args(tool, args))
    # NI Foreman P2: retired write tools run via the internal factory.
    return tools.INTERNAL_NI_TOOLS[name](ctx, args)


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
    handler = tools.INTERNAL_NI_TOOLS["derive_ni_paths"]
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
    handler = tools.INTERNAL_NI_TOOLS["derive_ni_paths"]
    out = handler(tools.ToolContext(), {"sample": [{"a": 1}, {"a": 2}]})
    assert any("[0]" in u for u in out["unaddressable"])


def test_derive_ni_paths_rejects_oversize_sample() -> None:
    handler = tools.INTERNAL_NI_TOOLS["derive_ni_paths"]
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
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "authoring.duckdb"))
    from smartbrain_3000.main import create_app
    with TestClient(create_app()) as test_client:
        yield test_client


def _unlock(client: TestClient) -> None:
    r = client.post("/api/account/setup", json={"passphrase": "correct-horse-battery-staple"})
    assert r.status_code == 200, r.text


def _install_card(client: TestClient, *, keyed: bool) -> str:
    """A card on the app's live store — keyed ones declare a secret header."""
    ctx = tools.ToolContext(ni=client.app.state.ni)
    over: dict = {"source": {"type": "http_page", "url": "https://finnhub.io/api/v1/quote"}}
    if keyed:
        over = {"params": {"api_key": {"label": "Finnhub API key", "kind": "secret",
                                        "value": "ni:self:api_key"}},
                "source": {"type": "http_page", "url": "https://finnhub.io/api/v1/quote",
                           "headers": {"X-Finnhub-Token": {"$secret": "ni:self:api_key"}}}}
    return _tool_call("create_ni_item", ctx, _basic_args(**over))["id"]


def test_board_row_needs_credentials_list_for_keyed_card(client: TestClient) -> None:
    _unlock(client)
    iid = _install_card(client, keyed=True)
    board = client.get("/api/ni/board").json()
    row = next(i for i in board["items"] if i["id"] == iid)
    assert row["state"] == "draft"
    assert row["needs_credentials"] == [
        {"name": "api_key", "label": "Finnhub API key"},
    ]


def test_board_row_needs_credentials_empty_after_credential_written(
        client: TestClient) -> None:
    _unlock(client)
    iid = _install_card(client, keyed=True)
    # Desktop-local PUT writes the credential; needs_credentials empties.
    r = client.put(f"/api/ni/items/{iid}/credential",
                   json={"name": "api_key", "value": "sk-xyz",
                         "host": "finnhub.io"})
    assert r.status_code == 200, r.text
    board = client.get("/api/ni/board").json()
    row = next(i for i in board["items"] if i["id"] == iid)
    assert row["needs_credentials"] == []


def test_board_row_needs_credentials_empty_for_keyless_card(
        client: TestClient) -> None:
    _unlock(client)
    iid = _install_card(client, keyed=False)
    board = client.get("/api/ni/board").json()
    row = next(i for i in board["items"] if i["id"] == iid)
    assert row["needs_credentials"] == []


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
