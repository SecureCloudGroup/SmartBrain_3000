"""Neural Interface: validators (spec + scene + path grammar), pipeline, binder, contract,
param substitution, store CRUD/pruning, due/backoff, tick isolation, host-bound secrets,
and the commissioning state machine."""

from __future__ import annotations

from types import SimpleNamespace

import duckdb
import pytest

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import ni as nimod
from smartbrain_3000.scheduler import ScheduleStore
from smartbrain_3000.secrets import SecretStore, gen_master_key

# --- helpers ---------------------------------------------------------------

def _minimal_scene() -> dict:
    return {"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "text", "value": "hello", "role": "title", "tone": "default", "size": "md"},
    ]}


def _basic_spec(**overrides) -> dict:
    base: dict = {
        "version": 1,
        "title": "Weather",
        "goal": "show the temperature",
        "params": {},
        "source": {"type": "model", "instruction": "hi"},
        "pipeline": [],
        "scene": _minimal_scene(),
        "display": {"size": "small"},
        "contract": None,
        "repair_policy": {"l1": True, "l2_frontier": False},
        "model": None,
    }
    base.update(overrides)
    return base


def _store():
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    return nimod.NIStore(conn, key), conn, key


# --- path grammar ---------------------------------------------------------

def test_parse_path_basic() -> None:
    assert nimod.parse_path("a") == [("key", "a")]
    assert nimod.parse_path("a.b") == [("key", "a"), ("key", "b")]
    assert nimod.parse_path("a[0]") == [("key", "a"), ("index", 0)]
    assert nimod.parse_path("a[-1]") == [("key", "a"), ("index", -1)]
    assert nimod.parse_path("items[0:5]") == [("key", "items"), ("slice", 0, 5)]
    assert nimod.parse_path("q.rows[0].amount") == [
        ("key", "q"), ("key", "rows"), ("index", 0), ("key", "amount")
    ]


def test_parse_path_rejects_bad_shapes() -> None:
    for bad in ("", ".", "a..b", "a b", "a[]", "a[b]", "a[0", "a[0]b", "a[1:2:3]"):
        with pytest.raises(ValueError):
            nimod.parse_path(bad)


def test_parse_path_rejects_denied_keys() -> None:
    for bad in ("__proto__", "constructor", "prototype", "foo.__proto__"):
        with pytest.raises(ValueError):
            nimod.parse_path(bad)


# --- spec validation ------------------------------------------------------

def test_validate_spec_good_roundtrip() -> None:
    assert nimod.validate_spec(_basic_spec())["version"] == 1


def test_validate_spec_rejects_unknown_key() -> None:
    with pytest.raises(ValueError):
        nimod.validate_spec(_basic_spec(surprise=1))


def test_validate_spec_rejects_bad_source_type() -> None:
    with pytest.raises(ValueError):
        nimod.validate_spec(_basic_spec(source={"type": "carrier-pigeon"}))


def test_validate_spec_rejects_reserved_scene_node() -> None:
    scene = {"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "spark"}  # reserved for a later phase
    ]}
    with pytest.raises(ValueError):
        nimod.validate_spec(_basic_spec(scene=scene))


def test_validate_spec_rejects_bad_display_size() -> None:
    with pytest.raises(ValueError):
        nimod.validate_spec(_basic_spec(display={"size": "gigantic"}))


def test_validate_http_json_source_and_secret_headers() -> None:
    spec = _basic_spec(source={
        "type": "http_json",
        "url": "https://api.example.com/q?sym=ACME",
        "headers": {"X-Api-Key": {"$secret": "ni:item:api_key"}},
    })
    nimod.validate_spec(spec)


# --- scene caps -----------------------------------------------------------

def test_scene_depth_cap() -> None:
    # Nested stacks past _MAX_SCENE_DEPTH must be rejected at spec validation time.
    inner: dict = {"type": "text", "value": "x", "role": "label",
                   "tone": "default", "size": "sm"}
    for _ in range(nimod._MAX_SCENE_DEPTH + 2):
        inner = {"type": "stack", "dir": "v", "gap": "sm", "children": [inner]}
    with pytest.raises(ValueError):
        nimod.validate_scene(inner)


def test_scene_node_count_cap_preexpansion() -> None:
    children = [{"type": "text", "value": "x", "role": "label",
                 "tone": "default", "size": "sm"}
                for _ in range(nimod._MAX_SCENE_NODES_PRE_EXPAND + 5)]
    scene = {"type": "stack", "dir": "v", "gap": "sm", "children": children}
    with pytest.raises(ValueError):
        nimod.validate_scene(scene)


def test_scene_text_value_length_cap() -> None:
    scene = {"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "text", "value": "x" * (nimod._MAX_TEXT_CHARS + 1),
         "role": "label", "tone": "default", "size": "sm"},
    ]}
    with pytest.raises(ValueError):
        nimod.validate_scene(scene)


def test_scene_repeat_max_over_cap() -> None:
    scene = {"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "repeat", "items": {"$bind": "rows"},
         "max": nimod._MAX_REPEAT_MAX + 1,
         "template": {"type": "text", "value": "x", "role": "label",
                      "tone": "default", "size": "sm"}},
    ]}
    with pytest.raises(ValueError):
        nimod.validate_scene(scene)


def test_scene_grid_cols_bounds() -> None:
    for cols in (1, 5):
        with pytest.raises(ValueError):
            nimod.validate_scene({"type": "grid", "cols": cols, "children": []})


# --- pipeline (pure) -------------------------------------------------------

def test_pipeline_extract_hit_and_miss() -> None:
    payload = {"quote": {"latest": 12.5}, "items": [{"a": 1}, {"a": 2}]}
    out = nimod.run_pipeline(
        [{"op": "extract", "paths": {"price": "quote.latest", "rows": "items[0:2]"}}],
        payload,
    )
    assert out == {"price": 12.5, "rows": [{"a": 1}, {"a": 2}]}
    with pytest.raises(nimod.NIError):
        nimod.run_pipeline(
            [{"op": "extract", "paths": {"nope": "quote.missing"}}], payload
        )


def test_transform_round_scale_type_mismatch() -> None:
    dictset = {"price": 3.14159, "s": "not a number"}
    out = nimod.run_pipeline(
        [{"op": "transform", "apply": [{"fn": "round", "field": "price", "digits": 2}]}],
        dictset,
    )
    assert out["price"] == 3.14
    with pytest.raises(nimod.NIError):
        nimod.run_pipeline(
            [{"op": "transform", "apply": [{"fn": "scale", "field": "s", "factor": 2}]}],
            dictset,
        )


def test_transform_rename_pick_sort_top() -> None:
    payload = {"rows": [
        {"name": "b", "amount": 2, "extra": "x"},
        {"name": "a", "amount": 3, "extra": "y"},
        {"name": "c", "amount": 1, "extra": "z"},
    ]}
    out = nimod.run_pipeline([
        {"op": "transform", "apply": [
            {"fn": "pick", "field": "rows", "keys": ["name", "amount"]},
            {"fn": "sort_by", "field": "rows", "key": "amount", "dir": "desc"},
            {"fn": "top_n", "field": "rows", "n": 2},
            {"fn": "rename", "field": "rows", "to": "top"},
        ]},
    ], payload)
    assert out == {"top": [{"name": "a", "amount": 3}, {"name": "b", "amount": 2}]}


def test_transform_top_n_needs_list() -> None:
    with pytest.raises(nimod.NIError):
        nimod.run_pipeline(
            [{"op": "transform", "apply": [{"fn": "top_n", "field": "x", "n": 3}]}],
            {"x": 42},
        )


# --- binder ---------------------------------------------------------------

def test_bind_scene_replaces_bind_and_interpolates() -> None:
    scene = {"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "text", "value": "hi {{name}}", "role": "title",
         "tone": "default", "size": "md"},
        {"type": "number", "value": {"$bind": "price"},
         "format": "plain", "unit": "USD", "tone": "default", "size": "md"},
    ]}
    bound = nimod.bind_scene(scene, {"name": "world", "price": 12.5})
    kids = bound["children"]
    assert kids[0]["value"] == "hi world"
    assert kids[1]["value"] == 12.5


def test_bind_scene_repeat_expands_item_paths() -> None:
    scene = {"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "repeat", "items": {"$bind": "rows"}, "max": 5,
         "template": {"type": "text", "value": "row-{{item.name}}",
                      "role": "value", "tone": "default", "size": "sm"}},
    ]}
    bound = nimod.bind_scene(scene, {"rows": [{"name": "a"}, {"name": "b"}]})
    expansion = bound["children"][0]
    assert expansion["type"] == "stack"
    assert [c["value"] for c in expansion["children"]] == ["row-a", "row-b"]


def test_bind_scene_unresolved_binding_fails() -> None:
    scene = {"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "text", "value": {"$bind": "missing.path"}, "role": "title",
         "tone": "default", "size": "md"},
    ]}
    with pytest.raises(nimod.NIError):
        nimod.bind_scene(scene, {})


def test_bind_scene_repeat_needs_list() -> None:
    scene = {"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "repeat", "items": {"$bind": "rows"}, "max": 5,
         "template": {"type": "text", "value": "x", "role": "label",
                      "tone": "default", "size": "sm"}},
    ]}
    with pytest.raises(nimod.NIError):
        nimod.bind_scene(scene, {"rows": {"not": "a list"}})


# --- contract -------------------------------------------------------------

def test_capture_and_check_contract_ok() -> None:
    outputs = {"price": 12.5, "rows": [{"amount": 3}, {"amount": 2}]}
    contract = nimod.capture_contract(outputs)
    assert contract["shape"] == {"price": "number", "rows": "list",
                                  "rows[].amount": "number"}
    ok, why = nimod.check_contract(contract, outputs)
    assert ok and why == ""


def test_check_contract_detects_type_change() -> None:
    contract = {"shape": {"price": "number"}}
    ok, why = nimod.check_contract(contract, {"price": "hi"})
    assert not ok and "price" in why


def test_check_contract_honors_bounds() -> None:
    contract = {"shape": {"price": "number"}, "bounds": {"price": {"min": 0}}}
    ok, _ = nimod.check_contract(contract, {"price": 1.0})
    assert ok
    ok, why = nimod.check_contract(contract, {"price": -0.5})
    assert not ok and "min" in why


# --- param substitution ---------------------------------------------------

def test_substitute_params_fills_string_in_url() -> None:
    spec = _basic_spec(
        params={"sym": {"label": "S", "kind": "string", "value": "ACME"}},
        source={"type": "http_json",
                "url": "https://api.example.com/q?sym={{param:sym}}",
                "headers": {}},
    )
    filled = nimod.substitute_params(spec)
    assert filled["source"]["url"] == "https://api.example.com/q?sym=ACME"


def test_substitute_params_rejects_secret_inline() -> None:
    spec = _basic_spec(
        params={"apikey": {"label": "K", "kind": "secret", "value": "ni:x:api_key"}},
        source={"type": "http_json",
                "url": "https://api.example.com/q?k={{param:apikey}}",
                "headers": {}},
    )
    with pytest.raises(ValueError):
        nimod.substitute_params(spec)


def test_substitute_params_leaves_secret_headers_alone() -> None:
    spec = _basic_spec(
        params={"apikey": {"label": "K", "kind": "secret", "value": "ni:x:api_key"}},
        source={"type": "http_json",
                "url": "https://api.example.com/q",
                "headers": {"X-Api-Key": {"$secret": "ni:x:api_key"}}},
    )
    filled = nimod.substitute_params(spec)
    assert filled["source"]["headers"]["X-Api-Key"] == {"$secret": "ni:x:api_key"}


# --- store CRUD + pruning -------------------------------------------------

def _preview() -> dict:
    return {"name": "hello"}


def test_add_get_list_delete_roundtrip() -> None:
    store, conn, _ = _store()
    spec = _basic_spec(scene={"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "text", "value": "{{name}}", "role": "title",
         "tone": "default", "size": "md"},
    ]})
    item_id = store.add_item(spec, _preview())
    assert store.get_item(item_id)["state"] == "draft"
    assert [i["id"] for i in store.list_items()] == [item_id]
    snap = store.read_snapshot(item_id, "preview")
    assert snap is not None and snap["ok"] is True
    store.delete(item_id)
    assert store.get_item(item_id) is None
    # snapshot/revisions/runs cascade in code (no FK)
    assert conn.execute(
        "SELECT COUNT(*) FROM ni_snapshots WHERE item_id = ?;", [item_id]
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM ni_revisions WHERE item_id = ?;", [item_id]
    ).fetchone()[0] == 0


def test_update_spec_bumps_rev_and_writes_revision() -> None:
    store, conn, _ = _store()
    spec = _basic_spec(scene={"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "text", "value": "{{name}}", "role": "title",
         "tone": "default", "size": "md"},
    ]})
    item_id = store.add_item(spec, _preview())
    new_rev = store.update_spec(item_id, dict(spec, title="Renamed"), origin="agent")
    assert new_rev == 2
    assert store.get_item(item_id)["spec"]["title"] == "Renamed"
    revs = conn.execute("SELECT COUNT(*) FROM ni_revisions WHERE item_id = ?;",
                        [item_id]).fetchone()[0]
    assert revs == 2


def test_update_spec_prunes_revisions_to_ten() -> None:
    store, conn, _ = _store()
    spec = _basic_spec(scene={"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "text", "value": "{{name}}", "role": "title",
         "tone": "default", "size": "md"},
    ]})
    item_id = store.add_item(spec, _preview())
    for i in range(14):  # bounded loop; each update writes one revision
        store.update_spec(item_id, dict(spec, title=f"v{i}"))
    revs = conn.execute("SELECT COUNT(*) FROM ni_revisions WHERE item_id = ?;",
                        [item_id]).fetchone()[0]
    assert revs == nimod._MAX_REVISIONS


def test_record_run_prunes_to_fifty() -> None:
    store, conn, _ = _store()
    item_id = store.add_item(_basic_spec(), _preview())
    for _ in range(nimod._MAX_RUNS + 7):  # bounded
        store.record_run(item_id, "ok", duration_ms=1, error=None, contract_ok=True)
    n = conn.execute("SELECT COUNT(*) FROM ni_runs WHERE item_id = ?;",
                     [item_id]).fetchone()[0]
    assert n == nimod._MAX_RUNS


# --- due query + backoff ---------------------------------------------------

def test_due_query_excludes_draft_paused_broken() -> None:
    store, conn, _ = _store()
    for state in ("draft", "paused", "broken"):
        iid = store.add_item(_basic_spec(), _preview())
        store.set_state(iid, state)
    active = store.add_item(_basic_spec(), _preview())
    store.set_state(active, "live")
    # force it to be due
    conn.execute("UPDATE ni_items SET last_checked = NULL WHERE id = ?;", [active])
    due = store.due_items()
    assert [i["id"] for i in due] == [active]


def test_effective_interval_doubles_on_failing_and_caps() -> None:
    # < threshold: base
    assert nimod.effective_interval_minutes(60, 0) == 60
    assert nimod.effective_interval_minutes(60, 2) == 60
    # at threshold: base; each additional failure doubles it, capped at 24h
    assert nimod.effective_interval_minutes(60, 3) == 60
    assert nimod.effective_interval_minutes(60, 4) == 120
    assert nimod.effective_interval_minutes(60, 5) == 240
    assert nimod.effective_interval_minutes(60, 20) == 1440


def test_bump_and_clear_failures_track_counter() -> None:
    store, _, _ = _store()
    iid = store.add_item(_basic_spec(), _preview())
    assert store.bump_failure(iid, "fetch_failed") == 1
    assert store.bump_failure(iid, "fetch_failed") == 2
    store.clear_failures(iid, "ok")
    assert store.get_item(iid)["consecutive_failures"] == 0


# --- host-bound credential ------------------------------------------------

def test_credential_refuses_wrong_host() -> None:
    _store_ignored, conn, key = _store()
    secrets = SecretStore(conn, key)
    key_name = nimod.put_credential(secrets, "item-1", "api_key", "s3cret",
                                     "api.example.com")
    # Right host resolves (K2/K3 require item_id + https scheme).
    assert nimod._load_credential(secrets, key_name, "api.example.com",
                                  item_id="item-1", request_scheme="https") == "s3cret"
    # Wrong host refuses — a permanent, engine-visible failure class.
    with pytest.raises(nimod.NIError) as excinfo:
        nimod._load_credential(secrets, key_name, "attacker.example.com",
                               item_id="item-1", request_scheme="https")
    assert excinfo.value.kind == "secret_host_mismatch"


def test_put_credential_normalizes_and_loader_scopes_and_requires_https() -> None:
    """K2 (loader-scoped prefix), K3 (host IDNA-lowercase + https-only) — all in one shot."""
    _store_ignored, conn, key = _store()
    secrets = SecretStore(conn, key)
    # Mixed-case + trailing whitespace host normalizes to plain lowercase (matches
    # urlparse().hostname later).
    key_name = nimod.put_credential(secrets, "item-A", "api_key", "s3cret",
                                     "API.Example.COM  ")
    assert nimod._load_credential(secrets, key_name, "api.example.com",
                                  item_id="item-A", request_scheme="https") == "s3cret"
    # K2 loader-scope: this credential belongs to item-A; item-B may not read it.
    with pytest.raises(nimod.NIError) as scoped:
        nimod._load_credential(secrets, key_name, "api.example.com",
                               item_id="item-B", request_scheme="https")
    assert scoped.value.kind == "secret_not_scoped"
    # K3 https-only: an http request refuses secret attachment ("secret_requires_https").
    with pytest.raises(nimod.NIError) as http_err:
        nimod._load_credential(secrets, key_name, "api.example.com",
                               item_id="item-A", request_scheme="http")
    assert http_err.value.kind == "secret_requires_https"


# --- engine: run_item + state transitions ---------------------------------

def _fetching_scene_spec() -> dict:
    # Scene binds a text field so a real payload {"text": "..."} renders end-to-end.
    scene = {"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "text", "value": "{{text}}", "role": "title",
         "tone": "default", "size": "md"},
    ]}
    return _basic_spec(scene=scene)


def _fetching_preview() -> dict:
    # Preview must satisfy the fetching-spec scene: a "text" string field.
    return {"text": "preview"}


class _FakeGateway:
    """A tiny stand-in for the gateway module (never dials the real Bifrost)."""

    class GatewayError(Exception):
        def __init__(self, status_code: int, message: str) -> None:
            super().__init__(message)
            self.status_code = status_code

    def __init__(self, text: str = "hello world", *, raises: Exception | None = None,
                 local_ok: bool = True, model: str = "ollama/x") -> None:
        self._text = text
        self._raises = raises
        self._local_ok = local_ok
        self._model = model
        self.calls = 0

    def load_routes(self, _conn) -> dict:
        return {"ni": self._model}

    def resolve_model(self, capability: str, routes: dict) -> str | None:
        return routes.get(capability)

    def is_local(self, model: str) -> bool:
        return model.startswith(("ollama/", "mlx/"))

    def local_available(self) -> bool:
        return self._local_ok

    def chat(self, _messages, _model, **_kwargs) -> dict:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return {"choices": [{"message": {"content": self._text}}]}

    def completion_text(self, data: dict) -> str:
        return data["choices"][0]["message"]["content"]


def test_run_item_c1_captures_contract_and_writes_snapshots() -> None:
    store, conn, key = _store()
    secrets = SecretStore(conn, key)
    schedules = ScheduleStore(conn, key)
    iid = store.add_item(_fetching_scene_spec(), _fetching_preview())
    store.set_state(iid, "commissioning")
    gw = _FakeGateway(text="today is sunny")

    nimod.run_item(store, iid, gateway_mod=gw, secrets_store=secrets,
                   schedules_store=schedules)

    item = store.get_item(iid)
    # C1: still commissioning until C2 records the verdict; contract now stored.
    assert item["state"] == "commissioning"
    assert item["spec"]["contract"]["shape"] == {"text": "string"}
    snap = store.read_snapshot(iid, "latest")
    assert snap is not None and snap["ok"] is True
    assert store.read_snapshot(iid, "last_good") is not None


def test_full_commissioning_to_live() -> None:
    store, conn, key = _store()
    secrets, schedules = SecretStore(conn, key), ScheduleStore(conn, key)
    iid = store.add_item(_fetching_scene_spec(), _fetching_preview())
    store.set_state(iid, "commissioning")
    gw = _FakeGateway(text="today is sunny")

    nimod.run_item(store, iid, gateway_mod=gw, secrets_store=secrets,
                   schedules_store=schedules)     # C1: contract captured
    store.record_validation(iid, True)             # C2: user says "Looks right"
    nimod.run_item(store, iid, gateway_mod=gw, secrets_store=secrets,
                   schedules_store=schedules)     # C3: next run, contract satisfied
    assert store.get_item(iid)["state"] == "live"


def test_run_item_contract_violation_degrades_and_preserves_last_good() -> None:
    store, conn, key = _store()
    secrets, schedules = SecretStore(conn, key), ScheduleStore(conn, key)
    iid = store.add_item(_fetching_scene_spec(), _fetching_preview())
    # Push straight to live with a captured contract that mandates string 'text'.
    good_gw = _FakeGateway(text="all good")
    store.set_state(iid, "commissioning")
    nimod.run_item(store, iid, gateway_mod=good_gw, secrets_store=secrets,
                   schedules_store=schedules)
    store.record_validation(iid, True)
    nimod.run_item(store, iid, gateway_mod=good_gw, secrets_store=secrets,
                   schedules_store=schedules)
    assert store.get_item(iid)["state"] == "live"
    good_snap = store.read_snapshot(iid, "last_good")
    assert good_snap is not None

    # Rig the gateway to return an empty string — the pipeline runs, the contract fires
    # against a "string" shape and passes (still a string), but let's simulate a real
    # contract violation by manually patching the stored contract to expect a number.
    updated = store.get_item(iid)["spec"]
    updated["contract"]["shape"] = {"text": "number"}
    conn.execute("UPDATE ni_items SET nonce = ?, ciphertext = ? WHERE id = ?;",
                 [*store._seal_item(iid, updated), iid])

    with pytest.raises(nimod.NIError) as excinfo:
        nimod.run_item(store, iid, gateway_mod=good_gw, secrets_store=secrets,
                       schedules_store=schedules)
    assert excinfo.value.kind == "contract_violation"
    item_after = store.get_item(iid)
    assert item_after["state"] == "degraded"
    assert item_after["consecutive_failures"] == 1
    # last_good survives — the degraded UI dims the prior data instead of blanking it.
    assert store.read_snapshot(iid, "last_good") is not None


def test_run_item_secret_host_mismatch_marks_broken() -> None:
    store, conn, key = _store()
    secrets, schedules = SecretStore(conn, key), ScheduleStore(conn, key)
    scene = {"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "text", "value": "{{text}}", "role": "title",
         "tone": "default", "size": "md"},
    ]}
    # Placeholder $secret ref satisfies validation ("ni:" prefix); we update it to the
    # real item id after add_item returns (K2: loader checks the full ``ni:{id}:`` prefix).
    spec = _basic_spec(scene=scene,
                       source={"type": "http_json",
                               "url": "https://api.example.com/q",
                               "headers": {"X-Api-Key": {"$secret": "ni:pending:api_key"}}})
    iid = store.add_item(spec, _fetching_preview())
    scoped = dict(spec)
    scoped["source"] = {"type": "http_json",
                        "url": "https://api.example.com/q",
                        "headers": {"X-Api-Key": {"$secret": f"ni:{iid}:api_key"}}}
    store.update_spec(iid, scoped)
    store.set_state(iid, "live")
    # Store a credential bound to a DIFFERENT host — the fetch must refuse it.
    nimod.put_credential(secrets, iid, "api_key", "s3cret", "attacker.example.com")

    with pytest.raises(nimod.NIError) as excinfo:
        nimod.run_item(store, iid, gateway_mod=_FakeGateway(), secrets_store=secrets,
                       schedules_store=schedules)
    assert excinfo.value.kind == "secret_host_mismatch"
    assert store.get_item(iid)["state"] == "broken"


# --- tick: isolation + budget ---------------------------------------------

def _fake_app(conn, key: bytes):
    """Minimal app.state shim for tick(): master_key + a db exposing .cursor().

    ``cursor`` is bound to ``conn.cursor`` so each call yields a fresh child cursor —
    the tick closes its own cursor in its finally block, and ``conn`` stays alive for
    the test's post-tick assertions."""
    return SimpleNamespace(state=SimpleNamespace(
        master_key=key,
        db=SimpleNamespace(cursor=conn.cursor),
    ))


def test_tick_isolates_per_item_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    store, conn, key = _store()
    good_iid = store.add_item(_fetching_scene_spec(), _fetching_preview())
    bad_iid = store.add_item(_fetching_scene_spec(), _fetching_preview())
    store.set_state(good_iid, "live")
    store.set_state(bad_iid, "live")
    conn.execute("UPDATE ni_items SET last_checked = NULL;")

    ok_gw = _FakeGateway(text="ok")

    def fake_run_item(store_arg, item_id, *, gateway_mod, secrets_store,
                      schedules_store=None):
        if item_id == bad_iid:
            raise nimod.NIError("fetch_failed", "oops")
        # Reuse the real run_item for the good one; use the ok gateway.
        return nimod._real_run_item_for_test(store_arg, item_id, ok_gw,
                                              secrets_store, schedules_store)

    # Bind a helper alias so the fake can delegate to the real function without cycling
    # through the monkeypatched name.
    monkeypatch.setattr(nimod, "_real_run_item_for_test", nimod.run_item, raising=False)
    monkeypatch.setattr(nimod, "run_item", fake_run_item)

    checked = nimod.tick(_fake_app(conn, key))
    assert checked == 2, "the bad item must not stop the pass"
    # The bad item's last_status carries a host-free class string.
    assert store.get_item(bad_iid)["last_status"] == "fetch_failed"


def test_tick_no_op_when_locked() -> None:
    _s, conn, key = _store()
    app = _fake_app(conn, key)
    app.state.master_key = None
    assert nimod.tick(app) == 0


# --- NI tool registry (Phase 1 wiring) -------------------------------------

def test_ni_tools_registered_with_correct_tiers_and_egress() -> None:
    """Every NI tool is present, tiered as §9 says, and OBSERVE tools stay non-egress."""
    from smartbrain_3000 import tools

    expected = {
        "list_ni_items": (tools.Tier.OBSERVE, False),
        "read_ni_item": (tools.Tier.OBSERVE, False),
        "create_ni_item": (tools.Tier.REVIEWED, True),
        "update_ni_item": (tools.Tier.REVIEWED, True),
        "set_ni_item_enabled": (tools.Tier.REVIEWED, True),
        "run_ni_item_now": (tools.Tier.REVIEWED, True),
        "delete_ni_item": (tools.Tier.IRREVERSIBLE, False),
    }
    for name, (tier, egress) in expected.items():
        tool = tools.get_tool(name)
        assert tool is not None, name
        assert tool.tier is tier, f"{name} tier {tool.tier} != {tier}"
        assert tool.egress is egress, f"{name} egress {tool.egress} != {egress}"
        # closed schema — the whole-registry gate at import already asserts this too
        assert tool.params_schema["additionalProperties"] is False, name


def test_ni_observe_tools_are_in_the_readonly_allowlist() -> None:
    """OBSERVE registration would fail import without this membership — check it explicitly."""
    from smartbrain_3000 import tools

    assert "list_ni_items" in tools._OBSERVE_READONLY
    assert "read_ni_item" in tools._OBSERVE_READONLY


def test_ni_write_tools_are_never_auto_in_unattended_turns() -> None:
    """NI_WRITE_TOOLS ⊆ UNATTENDED_NEVER_AUTO — the scheduler strips them from auto_approve."""
    from smartbrain_3000 import tools

    assert tools.NI_WRITE_TOOLS <= tools.UNATTENDED_NEVER_AUTO
    assert tools.NI_WRITE_TOOLS == {
        "create_ni_item", "update_ni_item", "set_ni_item_enabled", "run_ni_item_now",
    }


def test_ni_tools_are_never_rememberable() -> None:
    """No NI tool is in consent.py's lists — consent.remember_mode returns None for each.

    The write tools are egress=True and unlisted, so the default rule in ``remember_mode``
    refuses them; the OBSERVE reads never remembered (only REVIEWED tools qualify at all);
    delete is IRREVERSIBLE (always re-asks). This is the structural non-rememberability §9
    depends on — a new NI tool must stay unlisted on purpose.
    """
    from smartbrain_3000 import consent

    for name in ("list_ni_items", "read_ni_item",
                 "create_ni_item", "update_ni_item",
                 "set_ni_item_enabled", "run_ni_item_now",
                 "delete_ni_item"):
        assert consent.remember_mode(name) is None, name


def _tool_ctx():
    """Build a ToolContext wired to a fresh NIStore (plus its cursor + key for assertions)."""
    from smartbrain_3000 import tools

    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    return tools.ToolContext(ni=nimod.NIStore(conn, key)), conn, key


def _tool_call(name: str, ctx, args: dict) -> dict:
    from smartbrain_3000 import tools

    tool = tools.get_tool(name)
    return tool.handler(ctx, tools.validate_args(tool, args))


def _tool_spec_args() -> dict:
    """Args body for create_ni_item — a scene that binds a 'text' field so preview renders.

    ``draft: True`` (A1) so this helper's default matches the pre-fix behavior — tests
    that specifically exercise the commissioning-default path pass ``draft: False`` or
    omit the key. Keeps existing state assertions ("draft") non-load-bearing on the
    new landing rule.
    """
    scene = {"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "text", "value": "{{text}}", "role": "title",
         "tone": "default", "size": "md"},
    ]}
    return {
        "title": "Weather",
        "goal": "show the weather",
        "params": {},
        "source": {"type": "model", "instruction": "hi"},
        "pipeline": [],
        "scene": scene,
        "display": {"size": "small"},
        "interval_minutes": 60,
        "preview_payload": {"text": "sunny"},
        "draft": True,
    }


def test_create_ni_item_tool_validates_and_creates_draft() -> None:
    """Tool builds a valid spec, lands the item in draft, and writes the preview snapshot."""
    ctx, _conn, _key = _tool_ctx()
    out = _tool_call("create_ni_item", ctx, _tool_spec_args())
    assert out["state"] == "draft" and out["id"]
    item = ctx.ni.get_item(out["id"])
    assert item is not None and item["state"] == "draft"
    snap = ctx.ni.read_snapshot(out["id"], "preview")
    assert snap is not None and snap["ok"] is True


def test_create_ni_item_rejects_bad_spec() -> None:
    """A validator-refused spec (reserved scene node) surfaces as a ValueError, no row lands."""
    ctx, conn, _ = _tool_ctx()
    args = _tool_spec_args()
    args["scene"] = {"type": "spark"}  # reserved for a later phase; refused in v1
    with pytest.raises(ValueError):
        _tool_call("create_ni_item", ctx, args)
    assert conn.execute("SELECT COUNT(*) FROM ni_items;").fetchone()[0] == 0


def test_update_ni_item_source_change_resets_to_commissioning() -> None:
    """A3: any source change re-consents via the approved update card — commissioning, not draft.

    The card the user approves to run the tool IS the re-consent, so the item goes
    straight to ``commissioning`` (the C1 tick happens on the next scheduler pass).
    Also proves the sealed spec no longer carries a stale ``_c2_ok``/``contract``.
    """
    ctx, _c, _k = _tool_ctx()
    iid = _tool_call("create_ni_item", ctx, _tool_spec_args())["id"]
    # Force a post-draft state with attested fields — the update must strip both.
    ctx.ni.set_state(iid, "live")
    stale = dict(ctx.ni.get_item(iid)["spec"])
    stale["_c2_ok"] = True
    stale["contract"] = {"shape": {"text": "string"}}
    ctx.ni.update_spec(iid, stale)  # bumps rev; but update_spec ALSO strips these
    # Confirm baseline: our own update_spec already strips (A3) even without a source change.
    baseline = ctx.ni.get_item(iid)["spec"]
    assert "_c2_ok" not in baseline and baseline["contract"] is None
    ctx.ni.set_state(iid, "live")  # simulate the item having reached live
    out = _tool_call("update_ni_item", ctx,
                     {"item_id": iid, "source": {"type": "model", "instruction": "changed"}})
    assert out["state_reset"] == "commissioning"
    assert ctx.ni.get_item(iid)["state"] == "commissioning"


def test_update_ni_item_referenced_param_change_re_consents() -> None:
    """D4: a change to a param the source.url interpolates counts as a source change.

    The URL template stays the same but the effective URL moves — the item must
    re-consent via commissioning.
    """
    ctx, _c, _k = _tool_ctx()
    args = _tool_spec_args()
    args["params"] = {"sym": {"label": "Ticker", "kind": "string", "value": "ACME"}}
    args["source"] = {"type": "http_json",
                      "url": "https://api.example.com/q?sym={{param:sym}}",
                      "headers": {}}
    args["preview_payload"] = {"text": "preview"}
    iid = _tool_call("create_ni_item", ctx, args)["id"]
    ctx.ni.set_state(iid, "live")
    out = _tool_call(
        "update_ni_item", ctx,
        {"item_id": iid,
         "params": {"sym": {"label": "Ticker", "kind": "string", "value": "WIDGET"}}},
    )
    assert out["state_reset"] == "commissioning"
    assert ctx.ni.get_item(iid)["state"] == "commissioning"


def test_update_ni_item_title_only_leaves_state_untouched() -> None:
    """A pure metadata change is not a source change — state stays as it was."""
    ctx, _c, _k = _tool_ctx()
    iid = _tool_call("create_ni_item", ctx, _tool_spec_args())["id"]
    ctx.ni.set_state(iid, "live")
    out = _tool_call("update_ni_item", ctx, {"item_id": iid, "title": "Renamed"})
    assert out["state_reset"] is None
    item = ctx.ni.get_item(iid)
    assert item["state"] == "live" and item["spec"]["title"] == "Renamed"


def test_read_ni_item_provenance_first_and_no_secret_value() -> None:
    """Provenance line is the FIRST key; header $secret refs return as NAMES, never values."""
    ctx, _c, _k = _tool_ctx()
    args = _tool_spec_args()
    args["source"] = {"type": "http_json",
                      "url": "https://api.example.com/q",
                      "headers": {"X-Api-Key": {"$secret": "ni:x:api_key"}}}
    args["preview_payload"] = {"text": "preview"}
    iid = _tool_call("create_ni_item", ctx, args)["id"]
    out = _tool_call("read_ni_item", ctx, {"item_id": iid})
    keys = list(out.keys())
    assert keys[0] == "provenance", "the warning must precede the payload"
    assert "External content from api.example.com" in out["provenance"]
    # The stored spec carries the $secret NAME (never the value); double-check.
    hdr = out["spec"]["source"]["headers"]["X-Api-Key"]
    assert hdr == {"$secret": "ni:x:api_key"}
    # Belt-and-suspenders: no candidate secret string leaked into the returned dict.
    import json as _json
    assert "s3cret" not in _json.dumps(out)


def test_run_ni_item_now_clears_last_checked() -> None:
    """The tool marks the item due (last_checked → NULL) — no synchronous fetch from chat."""
    ctx, conn, _k = _tool_ctx()
    iid = _tool_call("create_ni_item", ctx, _tool_spec_args())["id"]
    # run_ni_item_now refuses draft (K6) — push to commissioning so the mark-due path fires.
    ctx.ni.commission(iid)
    ctx.ni.mark_checked(iid, "seeded")
    assert conn.execute("SELECT last_checked FROM ni_items WHERE id = ?;", [iid]).fetchone()[0] is not None
    _tool_call("run_ni_item_now", ctx, {"item_id": iid})
    assert conn.execute("SELECT last_checked FROM ni_items WHERE id = ?;", [iid]).fetchone()[0] is None


def test_run_ni_item_now_refuses_draft_paused_broken() -> None:
    """K6: refuses the run-now shortcut when the item cannot legitimately fire."""
    from smartbrain_3000 import tools

    ctx, _c, _k = _tool_ctx()
    iid = _tool_call("create_ni_item", ctx, _tool_spec_args())["id"]  # draft
    tool = tools.get_tool("run_ni_item_now")
    with pytest.raises(ValueError, match="draft"):
        tool.handler(ctx, {"item_id": iid})
    ctx.ni.commission(iid)
    ctx.ni.set_state(iid, "broken")
    with pytest.raises(ValueError, match="broken"):
        tool.handler(ctx, {"item_id": iid})
    ctx.ni.set_state(iid, "live")
    ctx.ni.set_enabled(iid, False)
    with pytest.raises(ValueError, match="paused"):
        tool.handler(ctx, {"item_id": iid})


def test_set_ni_item_enabled_toggles() -> None:
    ctx, _c, _k = _tool_ctx()
    iid = _tool_call("create_ni_item", ctx, _tool_spec_args())["id"]
    _tool_call("set_ni_item_enabled", ctx, {"item_id": iid, "enabled": False})
    assert ctx.ni.get_item(iid)["enabled"] is False
    _tool_call("set_ni_item_enabled", ctx, {"item_id": iid, "enabled": True})
    assert ctx.ni.get_item(iid)["enabled"] is True


def test_delete_ni_item_cascades() -> None:
    ctx, conn, _k = _tool_ctx()
    iid = _tool_call("create_ni_item", ctx, _tool_spec_args())["id"]
    _tool_call("delete_ni_item", ctx, {"item_id": iid})
    assert ctx.ni.get_item(iid) is None
    for table in ("ni_snapshots", "ni_revisions", "ni_runs"):
        assert conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE item_id = ?;", [iid]
        ).fetchone()[0] == 0


def test_ni_tools_refuse_when_store_unavailable() -> None:
    """A locked context (ctx.ni is None) refuses every NI tool cleanly (assert)."""
    from smartbrain_3000 import tools

    ctx = tools.ToolContext(ni=None)
    for name in ("list_ni_items", "read_ni_item", "create_ni_item", "update_ni_item",
                 "set_ni_item_enabled", "run_ni_item_now", "delete_ni_item"):
        with pytest.raises(AssertionError):
            tools.get_tool(name).handler(ctx, {"item_id": "x"})


# --- audit-finding regression tests ---------------------------------------

def test_create_ni_item_defaults_to_commissioning_and_secret_forces_draft() -> None:
    """A1: handler execution is consent, so a create lands in commissioning by default;
    an unfilled secret param still forces draft (credential must be entered first)."""
    ctx, _c, _k = _tool_ctx()
    # Default (no draft flag, no secret param) → commissioning.
    args = _tool_spec_args()
    args.pop("draft")
    out = _tool_call("create_ni_item", ctx, args)
    assert out["state"] == "commissioning"
    assert ctx.ni.get_item(out["id"])["state"] == "commissioning"
    # An empty secret param forces draft even without draft=True.
    args2 = _tool_spec_args()
    args2.pop("draft")
    args2["params"] = {"api_key": {"label": "Key", "kind": "secret", "value": ""}}
    args2["source"] = {"type": "http_json",
                       "url": "https://api.example.com/q",
                       "headers": {"X-Api-Key": {"$secret": "ni:self:api_key"}}}
    out2 = _tool_call("create_ni_item", ctx, args2)
    assert out2["state"] == "draft", "unfilled secret must force draft even without draft=True"


def test_update_ni_item_strips_c2_ok_and_contract_and_resets_streak() -> None:
    """A3/F: ANY update strips _c2_ok + contract from the sealed spec and resets the streak."""
    ctx, conn, _k = _tool_ctx()
    iid = _tool_call("create_ni_item", ctx, _tool_spec_args())["id"]
    spec = dict(ctx.ni.get_item(iid)["spec"])
    spec["_c2_ok"] = True
    spec["contract"] = {"shape": {"text": "string"}}
    ctx.ni.update_spec(iid, spec)
    # Prime a streak: two failures.
    ctx.ni.bump_failure(iid, "fetch_failed")
    ctx.ni.bump_failure(iid, "fetch_failed")
    assert ctx.ni.get_item(iid)["consecutive_failures"] == 2
    # A pure title change (no source change) still strips + resets (A3/F contract).
    _tool_call("update_ni_item", ctx, {"item_id": iid, "title": "Renamed"})
    after = ctx.ni.get_item(iid)
    assert "_c2_ok" not in after["spec"] and after["spec"]["contract"] is None
    assert after["consecutive_failures"] == 0
    assert ctx.ni.get_first_failure_at(iid) is None


def test_fetch_model_passes_conn_to_load_routes() -> None:
    """B: `_fetch_model` must call load_routes(store.conn); passing None trips its assertion."""

    class _StrictGateway:
        class GatewayError(Exception):
            def __init__(self, status_code: int, message: str) -> None:
                super().__init__(message)
                self.status_code = status_code

        def load_routes(self, conn) -> dict:
            assert conn is not None, "load_routes REQUIRES a real cursor (never None)"
            return {"ni": "ollama/x"}

        def resolve_model(self, capability: str, routes: dict) -> str | None:
            return routes.get(capability)

        def is_local(self, model: str) -> bool:
            return True

        def local_available(self) -> bool:
            return True

        def chat(self, _messages, _model, **_kwargs) -> dict:
            return {"choices": [{"message": {"content": "hi"}}]}

        def completion_text(self, data: dict) -> str:
            return data["choices"][0]["message"]["content"]

    store, conn, key = _store()
    secrets, schedules = SecretStore(conn, key), ScheduleStore(conn, key)
    iid = store.add_item(_fetching_scene_spec(), _fetching_preview())
    store.set_state(iid, "commissioning")
    nimod.run_item(store, iid, gateway_mod=_StrictGateway(),
                   secrets_store=secrets, schedules_store=schedules)
    # No assertion tripped inside StrictGateway.load_routes = the fix landed.
    assert store.get_item(iid)["state"] == "commissioning"


def test_record_validation_refuses_outside_commissioning() -> None:
    """C: record_validation refuses (ValueError) unless the item is currently commissioning."""
    store, _, _ = _store()
    iid = store.add_item(_basic_spec(), _preview())  # lands draft
    with pytest.raises(ValueError, match="commissioning"):
        store.record_validation(iid, True)
    store.set_state(iid, "live")
    with pytest.raises(ValueError, match="commissioning"):
        store.record_validation(iid, True)


def test_c3_captures_then_requires_one_more_clean_run_when_c2_early() -> None:
    """C: if _c2_ok is set BEFORE C1 captured the contract, the first clean run captures
    and stays commissioning; only the NEXT clean run promotes to live."""
    store, conn, key = _store()
    secrets, schedules = SecretStore(conn, key), ScheduleStore(conn, key)
    iid = store.add_item(_fetching_scene_spec(), _fetching_preview())
    store.set_state(iid, "commissioning")
    store.record_validation(iid, True)  # _c2_ok=True with contract still None
    gw = _FakeGateway(text="today is sunny")
    nimod.run_item(store, iid, gateway_mod=gw, secrets_store=secrets,
                   schedules_store=schedules)
    # First clean run: contract captured, but item MUST stay commissioning.
    item = store.get_item(iid)
    assert item["state"] == "commissioning"
    assert item["spec"]["contract"]["shape"] == {"text": "string"}
    # Second clean run: check active + passes → live.
    nimod.run_item(store, iid, gateway_mod=gw, secrets_store=secrets,
                   schedules_store=schedules)
    assert store.get_item(iid)["state"] == "live"


def test_validate_http_json_url_refuses_placeholder_in_authority() -> None:
    """D1: {{param:}} anywhere in scheme/host/port is refused at validation time."""
    for bad in (
        "https://{{param:host}}.example.com/q",
        "https://{{param:host}}/q",
        "{{param:scheme}}://api.example.com/q",
        "https://api.example.com:{{param:port}}/q",
    ):
        with pytest.raises(ValueError):
            nimod.validate_spec(_basic_spec(source={
                "type": "http_json", "url": bad, "headers": {},
            }))
    # But a placeholder in path/query is fine.
    nimod.validate_spec(_basic_spec(source={
        "type": "http_json",
        "url": "https://api.example.com/q?sym={{param:sym}}",
        "headers": {},
    }, params={"sym": {"label": "S", "kind": "string", "value": "ACME"}}))


def test_substitute_params_url_encodes_values_in_source_url() -> None:
    """D2: param values landing in source.url are percent-encoded so they can't rewrite structure."""
    spec = _basic_spec(
        params={"q": {"label": "Q", "kind": "string", "value": "one two&admin=1"}},
        source={"type": "http_json",
                "url": "https://api.example.com/s?q={{param:q}}",
                "headers": {}},
    )
    filled = nimod.substitute_params(spec)
    # ``one two&admin=1`` must become ``one%20two%26admin%3D1`` — the & no longer starts a new key.
    assert filled["source"]["url"] == \
        "https://api.example.com/s?q=one%20two%26admin%3D1"


def test_validate_http_json_forbids_param_placeholder_in_plain_header() -> None:
    """D3: {{param:}} is forbidden anywhere in plain header values."""
    with pytest.raises(ValueError, match="param"):
        nimod.validate_spec(_basic_spec(source={
            "type": "http_json",
            "url": "https://api.example.com/q",
            "headers": {"X-Trace": "id-{{param:sym}}"},
        }))


def test_auth_shaped_header_requires_secret_ref() -> None:
    """K4: auth-shaped literal header names are refused unless the value is a $secret ref."""
    for name in ("Authorization", "Cookie", "X-Api-Key", "X-Some-Token"):
        bad = _basic_spec(source={
            "type": "http_json",
            "url": "https://api.example.com/q",
            "headers": {name: "literal-value"},
        })
        with pytest.raises(ValueError, match="\\$secret"):
            nimod.validate_spec(bad)
    # A $secret ref satisfies it (and the ref must start with "ni:" per K2 validation).
    ok = _basic_spec(source={
        "type": "http_json",
        "url": "https://api.example.com/q",
        "headers": {"Authorization": {"$secret": "ni:self:token"}},
    })
    nimod.validate_spec(ok)


def test_secret_ref_must_start_with_ni_prefix() -> None:
    """K2 validation: $secret ref must start with 'ni:' at spec time."""
    with pytest.raises(ValueError, match="ni:"):
        nimod.validate_spec(_basic_spec(source={
            "type": "http_json",
            "url": "https://api.example.com/q",
            "headers": {"X-Api-Key": {"$secret": "some-other-namespace:key"}},
        }))


def test_icon_name_literal_only_charset() -> None:
    """K1: icon.name must match ^[a-z0-9-]{1,60}$ (no {{}} or $bind)."""
    for bad in ("Sun", "sun_shine", "sun.shine", "{{icon}}", ""):
        scene = {"type": "stack", "dir": "v", "gap": "sm", "children": [
            {"type": "icon", "name": bad, "tone": "default"},
        ]}
        with pytest.raises(ValueError):
            nimod.validate_scene(scene)
    # Valid: kebab lowercase.
    nimod.validate_scene({"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "icon", "name": "sun-medium", "tone": "default"},
    ]})


def test_extract_output_named_item_refused() -> None:
    """K5: 'item' is reserved (bind root under repeat) and refused at extract validation."""
    with pytest.raises(ValueError, match="reserved"):
        nimod.validate_spec(_basic_spec(pipeline=[
            {"op": "extract", "paths": {"item": "quote.latest"}},
        ]))


def test_nested_repeat_refused() -> None:
    """K5: a repeat inside another repeat's template is refused at scene validation."""
    inner = {"type": "repeat", "items": {"$bind": "inner"}, "max": 3,
             "template": {"type": "text", "value": "x", "role": "label",
                          "tone": "default", "size": "sm"}}
    outer = {"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "repeat", "items": {"$bind": "rows"}, "max": 3,
         "template": {"type": "stack", "dir": "v", "gap": "sm", "children": [inner]}},
    ]}
    with pytest.raises(ValueError, match="nested"):
        nimod.validate_scene(outer)


def test_number_unit_interpolation_grammar_checked_at_validation() -> None:
    """G1: a malformed {{path}} in number.unit fails at spec validation, not at bind."""
    scene = {"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "number", "value": 1.0, "format": "plain",
         "unit": "{{bad path}}", "tone": "default", "size": "md"},
    ]}
    with pytest.raises(ValueError, match="interpolation"):
        nimod.validate_scene(scene)


def test_bind_type_enforcement_refuses_wrong_leaf_type() -> None:
    """H: a $bind that resolves a dict into a text.value slot fails as bind_type."""
    scene = {"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "text", "value": {"$bind": "obj"}, "role": "title",
         "tone": "default", "size": "md"},
    ]}
    # bind_scene interpolates; _enforce_bind_types then catches the dict-in-text-value.
    bound = nimod.bind_scene(scene, {"obj": {"nested": 1}})
    with pytest.raises(nimod.NIError) as excinfo:
        nimod._enforce_bind_types(scene, bound)
    assert excinfo.value.kind == "bind_type"


def test_payload_size_cap_refuses_oversize_bound() -> None:
    """H: a bound payload whose JSON exceeds _MAX_PAYLOAD_BYTES raises payload_too_large."""
    scene = {"type": "stack", "dir": "v", "gap": "sm", "children": [
        {"type": "text", "value": "x" * 1900, "role": "title",
         "tone": "default", "size": "md"},
    ] * 3}
    bound = nimod.bind_scene(scene, {})
    # Craft a huge post-bind snapshot by hand (bypass validation caps to prove the byte cap fires).
    bloated = {"type": "wrap", "big": "y" * (nimod._MAX_PAYLOAD_BYTES + 10)}
    with pytest.raises(nimod.NIError) as excinfo:
        nimod._enforce_payload_size(bloated)
    assert excinfo.value.kind == "payload_too_large"
    nimod._enforce_payload_size(bound)  # smaller payload OK


def test_broken_escalation_uses_first_failure_at_not_created_at() -> None:
    """F: a long-lived healthy item that only STARTS failing today can't be classed broken today.

    Prior code compared ``now - created_at``, so an item that had been alive > 7 days went
    straight to broken at the 8th failure of a same-day streak. The fix compares
    ``now - first_failure_at`` (streak marker), so 8 fresh failures within a day stay
    on the failing ladder until the streak passes 7 days.
    """
    from datetime import timedelta as _td

    store, conn, key = _store()
    iid = store.add_item(_basic_spec(), _preview())
    # Force ``created_at`` back a month; nothing about escalation should care.
    conn.execute("UPDATE ni_items SET created_at = now() - INTERVAL '30 DAYS' WHERE id = ?;",
                 [iid])
    store.set_state(iid, "live")
    # 8 quick failures — streak marker was set on the first bump, so first_failure_at
    # is only seconds old. The escalation must NOT fire.
    for _ in range(nimod._BROKEN_FAILURE_COUNT):  # bounded
        exc = nimod.NIError("fetch_failed", "oops")
        nimod._handle_failure(store, store.get_item(iid), exc, started=0.0)
    assert store.get_item(iid)["state"] != "broken"
    # But when the streak marker is aged past the 7-day threshold, escalation fires next time.
    conn.execute(
        "UPDATE ni_items SET first_failure_at = now() - INTERVAL '8 DAYS' WHERE id = ?;",
        [iid],
    )
    nimod._handle_failure(store, store.get_item(iid),
                          nimod.NIError("fetch_failed"), started=0.0)
    assert store.get_item(iid)["state"] == "broken"
    # A clean run clears the marker + counter.
    assert isinstance(_td(days=1), _td)  # keeps the timedelta import wired (POW10 #2)


def test_first_failure_at_clears_on_success_and_update() -> None:
    """F: the streak marker is cleared on success and on any update_spec."""
    store, _, _ = _store()
    iid = store.add_item(_basic_spec(), _preview())
    store.bump_failure(iid, "fetch_failed")
    assert store.get_first_failure_at(iid) is not None
    store.clear_failures(iid, "ok")
    assert store.get_first_failure_at(iid) is None
    store.bump_failure(iid, "fetch_failed")
    assert store.get_first_failure_at(iid) is not None
    store.update_spec(iid, _basic_spec(title="Renamed"))
    assert store.get_first_failure_at(iid) is None
    assert store.get_item(iid)["consecutive_failures"] == 0


def test_handle_failure_writes_ok_false_latest_snapshot() -> None:
    """G3: after a failure, ``latest`` becomes an ok=False marker so the board's
    latest-if-ok fallback picks up ``last_good`` correctly."""
    store, _, _ = _store()
    iid = store.add_item(_basic_spec(), _preview())
    store.set_state(iid, "live")
    exc = nimod.NIError("fetch_failed", "oops")
    nimod._handle_failure(store, store.get_item(iid), exc, started=0.0)
    latest = store.read_snapshot(iid, "latest")
    assert latest is not None and latest["ok"] is False and latest["payload"] == {}


def test_ni_tick_skips_model_source_while_breaker_open() -> None:
    """I: while the breaker is open, model-source items stay due (skipped, no mark_checked)."""
    store, conn, key = _store()
    iid = store.add_item(_fetching_scene_spec(), _fetching_preview())
    store.set_state(iid, "live")
    conn.execute("UPDATE ni_items SET last_checked = NULL;")
    original_last = store.get_item(iid)["last_checked"]
    assert original_last is None

    # If the tick reached the model source, gateway_mod.load_routes would fire — but the
    # tick's own import is smartbrain_3000.gateway; monkeypatching that would risk bleed
    # across other tests, so instead we assert the OBSERVABLE contract: last_checked
    # stays NULL (no mark_checked ran) so the item remains due for the next tick.
    nimod.tick(_fake_app(conn, key), breaker_open=lambda: True)
    # No mark_checked ran, so last_checked is still NULL and the item stays due.
    assert store.get_item(iid)["last_checked"] is None


def test_create_ni_item_url_validation_rejects_lan_host() -> None:
    """J: create_ni_item runs netguard.validate_public_url — a LAN host is refused."""
    ctx, _c, _k = _tool_ctx()
    args = _tool_spec_args()
    args.pop("draft")
    args["source"] = {"type": "http_json",
                      "url": "http://127.0.0.1:9000/q",
                      "headers": {}}
    args["preview_payload"] = {"text": "preview"}
    with pytest.raises(ValueError, match="url"):
        _tool_call("create_ni_item", ctx, args)


def test_update_ni_item_preview_payload_rewrites_snapshot() -> None:
    """K8: an update with preview_payload rewrites the preview slot."""
    ctx, _c, _k = _tool_ctx()
    iid = _tool_call("create_ni_item", ctx, _tool_spec_args())["id"]
    _tool_call("update_ni_item", ctx,
               {"item_id": iid, "preview_payload": {"text": "brand-new"}})
    snap = ctx.ni.read_snapshot(iid, "preview")
    assert snap is not None and snap["payload"]["children"][0]["value"] == "brand-new"
