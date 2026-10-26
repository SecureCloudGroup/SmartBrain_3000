"""Migration: cards the retired v1 build flow stored still load, refresh, render, export and import.

v1 differed on disk in two ways: a pick made by the user sealed ``_built_from.path = "declared"`` (and the card
was validated, not held for Keep), and a flow slot could sit at the ``source`` pause with a sealed list of
suggestions. v2 reads both without a migration."""

from __future__ import annotations

import copy

from test_ni_answers import WEATHER_URL, lib  # noqa: F401  (lib is a fixture)
from test_ni_auto_pick import (  # noqa: F401  (client is a fixture)
    OPEN_METEO,
    _auto_card,
    client,
)

from smartbrain_3000 import ni as nimod
from smartbrain_3000 import ni_flow, ni_master, ni_routes


def _as_v1_card(client, lib, monkeypatch) -> str:  # noqa: F811
    """A finished card shaped as v1 stored it: declared by a user pick, validated, not held, flow slot cleared."""
    item_id = _auto_card(client, lib, monkeypatch)
    store = client.app.state.ni
    spec = copy.deepcopy(store.get_item(item_id)["spec"])
    spec["_built_from"]["path"] = "declared"
    store.update_spec(item_id, spec, origin="agent")
    store.record_validation(item_id, True)
    ni_flow.clear_flow_slot(store, item_id)
    return item_id


def _row(cl, item_id: str) -> dict:
    return next(r for r in cl.get("/api/ni/board").json()["items"] if r["id"] == item_id)


def test_a_v1_declared_card_loads_and_renders(client, lib, monkeypatch) -> None:  # noqa: F811
    item_id = _as_v1_card(client, lib, monkeypatch)
    item = client.app.state.ni.get_item(item_id)
    assert item["spec"]["_built_from"]["path"] == "declared" and not nimod.awaits_yes(item)
    nimod.validate_spec(copy.deepcopy(item["spec"]))
    row = _row(client, item_id)
    assert row["awaiting_yes"] is None and row["flow"] is None and row["state"] == "commissioning"
    assert item_id in [i["id"] for i in client.app.state.ni.due_items()], "validated: due for its first run"
    assert set(row["actions"]) <= ni_master.ACTION_KINDS and "delete" in row["actions"]


def test_a_card_from_before_the_built_from_marker_loads_runs_and_exports(client, lib, monkeypatch) -> None:  # noqa: F811
    """Cards built before the 2026-10-04 ruling carry no ``_built_from`` at all: every reader treats it as absent."""
    from smartbrain_3000 import netguard

    item_id = _as_v1_card(client, lib, monkeypatch)
    store = client.app.state.ni
    spec = copy.deepcopy(store.get_item(item_id)["spec"])
    spec.pop("_built_from")
    store.update_spec(item_id, spec, origin="agent", preserve_attestations=True)
    item = store.get_item(item_id)
    assert "_built_from" not in item["spec"] and not nimod.awaits_yes(item)
    row = _row(client, item_id)
    assert row["awaiting_yes"] is None and set(row["actions"]) <= ni_master.ACTION_KINDS
    monkeypatch.setattr(netguard, "safe_fetch_json",
                        lambda url, headers=None, allow_redirects=True: copy.deepcopy(OPEN_METEO))
    assert client.post(f"/api/ni/items/{item_id}/run").json()["status"] == "ok"
    template = ni_routes._build_export_template(store, store.get_item(item_id), client.app.state.secret_store)
    nimod.validate_spec(copy.deepcopy(template["spec_template"]))


def test_a_v1_declared_card_refreshes(client, lib, monkeypatch) -> None:  # noqa: F811
    from smartbrain_3000 import netguard

    item_id = _as_v1_card(client, lib, monkeypatch)
    monkeypatch.setattr(netguard, "safe_fetch_json",
                        lambda url, headers=None, allow_redirects=True: copy.deepcopy(OPEN_METEO))
    body = client.post(f"/api/ni/items/{item_id}/run").json()
    assert body["status"] == "ok", body
    row = _row(client, item_id)
    assert row["payload_slot"] == "latest" and row["payload"] is not None and row["payload_ok"] is True


def test_a_v1_declared_card_exports_and_imports(client, lib, monkeypatch) -> None:  # noqa: F811
    item_id = _as_v1_card(client, lib, monkeypatch)
    store = client.app.state.ni
    template = ni_routes._build_export_template(store, store.get_item(item_id), client.app.state.secret_store)
    assert "_built_from" not in template["spec_template"], "the marker never leaves the card"
    nimod.validate_spec(copy.deepcopy(template["spec_template"]))


def test_a_v1_flow_parked_on_the_source_pick_serves_actions_and_polls(client, monkeypatch) -> None:  # noqa: F811
    """A flow slot v1 left at ``source`` (the pick pause) reads as a stopped shell: it has a reason and a way on."""
    store = client.app.state.ni
    monkeypatch.setattr(ni_flow, "start_flow_worker", lambda *a, **k: True)
    item_id = ni_flow.create_shell_item(store, "old ask from v1")
    ni_flow._flow_write(store, item_id, {
        **ni_flow._make_record("old ask from v1", "source"), "error": ni_flow.AWAITING_SOURCE_PICK,
        "_ranked_library": [{"source_id": "open-meteo-forecast", "url": WEATHER_URL, "title": "t"}]})
    row = _row(client, item_id)
    assert row["actions"] and "delete" in row["actions"]
    poll = client.get(f"/api/ni/items/{item_id}/flow")
    assert poll.status_code == 200
    assert client.post(f"/api/ni/items/{item_id}/flow/cancel").status_code in (200, 409)


def test_a_paste_matching_a_v1_parked_row_that_refuses_declines_and_never_searches(lib, monkeypatch) -> None:  # noqa: F811
    """S10 review M3: a stored v1 pause still holds its tapped rows. A paste of one that then refuses ends on the
    declined card; the retired web stage (search plus page pre-reads) never runs."""
    from test_ni_auto_pick import _store

    store = _store()
    searched: list[str] = []

    class _Search:
        def search(self, query: str, limit: int = 10) -> dict:
            searched.append(query)
            return {"results": []}

    monkeypatch.setattr(ni_flow, "_SEARCH_PROVIDER", lambda: _Search())
    item_id = ni_flow.create_shell_item(store, "old ask from v1")
    ni_flow._flow_write(store, item_id, {
        **ni_flow._make_record("old ask from v1", "source"), "error": ni_flow.AWAITING_SOURCE_PICK,
        "_ranked_library": [{"source_id": "open-meteo-forecast", "url": WEATHER_URL, "title": "t"}]})
    out = ni_flow._move_on(store, item_id, WEATHER_URL, "refused SmartBrain's request")
    assert out["state"] == "declined" and out["_decline_card"]["reason"] == "source_cant_answer"
    assert searched == []
