"""A picked Library source that needs the user's own key or contact email.

The card asks before the first fetch; the key is stored host-bound under the card and rides
only HTTPS requests to that host (query or header), redirects refused; the contact email rides
only ``contact_ua`` sources' User-Agent. Keyless sources come first (test_library_index).
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import duckdb
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import netguard, ni_flow
from smartbrain_3000 import ni as nimod
from smartbrain_3000.secrets import SecretStore, gen_master_key

_KEYED_URL = "https://www.airnowapi.org/aq/observation/zipCode/current/?format=application/json&zipCode=29401"
_KEYED_ROW = {"source_id": "airnow-current-zip", "title": "Air quality now", "host": "www.airnowapi.org",
              "url": _KEYED_URL, "provider": "AirNow", "authority": "official", "label": "",
              "choice": False, "format": "json",
              "needs_key": {"in": "query", "name": "API_KEY", "prefix": "",
                            "docs_url": "https://docs.airnowapi.org/faq"},
              "needs_contact": False}
_SEC_URL = "https://data.sec.gov/submissions/CIK0000320193.json"
_SEC_ROW = {"source_id": "sec-submissions", "title": "Company filings", "host": "data.sec.gov",
            "url": _SEC_URL, "provider": "SEC", "authority": "official", "label": "", "choice": False,
            "format": "json", "needs_key": None, "needs_contact": True}


def _stores() -> tuple[nimod.NIStore, SecretStore]:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    return nimod.NIStore(conn, key), SecretStore(conn, key)


# --- the spec shape ---------------------------------------------------------------------------

def _spec(source: dict) -> dict:
    return {"version": 1, "title": "t", "goal": "g",
            "params": {"api_key": {"label": "k", "kind": "secret", "value": "ni:x:api_key"}},
            "source": source, "pipeline": [{"op": "extract", "paths": {"v": "v"}}],
            "scene": ni_flow.value_scene(["v"]), "display": {"size": "small"}, "interval_minutes": 15}


def test_secret_query_and_contact_ua_validate_closed() -> None:
    ok = {"type": "http_json", "url": "https://a.example.org/x",
          "secret_query": {"API_KEY": {"$secret": "ni:x:api_key"}}, "contact_ua": True}
    nimod.validate_spec(_spec(ok))
    for bad, why in [
        ({**ok, "url": "http://a.example.org/x"}, "https"),
        ({**ok, "secret_query": {"API_KEY": "literal-key"}}, "secret_query"),
        ({**ok, "secret_query": {"API_KEY": {"$secret": "other:x"}}}, "ni:"),
        ({**ok, "secret_query": {"bad name": {"$secret": "ni:x:api_key"}}}, "malformed"),
        ({**ok, "secret_query": {}}, "1..2"),
        ({**ok, "contact_ua": "yes"}, "contact_ua"),
    ]:
        with pytest.raises(ValueError, match=why):
            nimod.validate_spec(_spec(bad))


# --- the request the engine and the first sample send -------------------------------------------

def test_request_parts_add_the_host_bound_key_and_the_contact_email() -> None:
    _store, secrets = _stores()
    nimod.put_credential(secrets, "item1", "api_key", "k-123", "www.airnowapi.org")
    nimod.set_contact_email(secrets, "me@example.com")
    source = {"type": "http_json", "url": _KEYED_URL,
              "secret_query": {"API_KEY": {"$secret": "ni:item1:api_key"}}, "contact_ua": True}
    url, headers = nimod.http_request_parts(source, "item1", secrets)
    assert url == _KEYED_URL + "&API_KEY=k-123"
    assert headers["User-Agent"] == f"{netguard.USER_AGENT} me@example.com"
    # the key is bound to its host: the same ref on another host is refused
    with pytest.raises(nimod.NIError) as exc:
        nimod.http_request_parts({**source, "url": "https://evil.example.org/x"}, "item1", secrets)
    assert exc.value.kind == "secret_host_mismatch"


def test_a_contact_source_without_an_email_fails_honestly() -> None:
    _store, secrets = _stores()
    with pytest.raises(nimod.NIError) as exc:
        nimod.http_request_parts({"type": "http_json", "url": _SEC_URL, "contact_ua": True}, "i", secrets)
    assert exc.value.kind == "contact_missing"


@pytest.mark.parametrize("email", ["", "not an email", "a@b", "a@b.com, c@d.com", "<a@b.com>"])
def test_only_one_plain_email_is_accepted(email) -> None:
    _store, secrets = _stores()
    with pytest.raises(ValueError, match="one email address"):
        nimod.set_contact_email(secrets, email)


def test_the_engine_refuses_redirects_while_a_key_rides(monkeypatch) -> None:
    _store, secrets = _stores()
    nimod.put_credential(secrets, "item1", "api_key", "k-123", "www.airnowapi.org")
    seen: dict = {}

    def fake(url, headers=None, allow_redirects=True):
        seen.update(url=url, headers=headers, allow_redirects=allow_redirects)
        return {"v": 1}

    monkeypatch.setattr(netguard, "safe_fetch_json", fake)
    source = {"type": "http_json", "url": _KEYED_URL,
              "secret_query": {"API_KEY": {"$secret": "ni:item1:api_key"}}}
    assert nimod._fetch_http_json(source, "item1", secrets) == {"v": 1}
    assert seen["url"].endswith("&API_KEY=k-123") and seen["allow_redirects"] is False


# --- the card: pick → ask → build ------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "access.duckdb"))
    from smartbrain_3000.main import create_app
    with TestClient(create_app()) as test_client:
        r = test_client.post("/api/account/setup", json={"passphrase": "correct-horse-battery-staple"})
        assert r.status_code == 200, r.text
        yield test_client


def _pick_pause(client: TestClient, monkeypatch, rows: list[dict], fired: list,
                ask: str = "air quality in Charleston") -> str:
    monkeypatch.setattr(ni_flow, "start_flow_worker",
                        lambda s, i, **kw: fired.append(kw) or True)
    iid = client.post("/api/ni/intake", json={"request": ask}).json()["id"]
    store = client.app.state.ni
    record = ni_flow._flow_read(store, iid)
    record.update(state="source", error=ni_flow.AWAITING_SOURCE_PICK, _ranked_library=rows)
    ni_flow._flow_write(store, iid, record)
    fired.clear()
    return iid


def test_a_keyed_pick_asks_for_the_key_then_builds(client: TestClient, monkeypatch) -> None:
    fired: list = []
    iid = _pick_pause(client, monkeypatch, [_KEYED_ROW], fired)
    board = client.get("/api/ni/board").json()["items"]
    sug = next(x for x in board if x["id"] == iid)["flow"]["suggestions"][0]
    assert sug["needs"] == ["key"]
    r = client.post(f"/api/ni/items/{iid}/flow/pick-source", json={"url": _KEYED_URL})
    assert r.status_code == 200 and r.json() == {"ok": True, "started": False, "needs": ["key"]}
    assert fired == []  # nothing is fetched before the key
    from smartbrain_3000 import library_client
    assert library_client.pending(client.app.state.ni) == 1  # the tap is a Yes, queued for the Library
    flow = next(x for x in client.get("/api/ni/board").json()["items"] if x["id"] == iid)["flow"]
    assert flow["state"] == "awaiting_access"
    assert flow["access"] == {"host": "www.airnowapi.org", "provider": "AirNow",
                              "key": {"docs_url": "https://docs.airnowapi.org/faq"}, "contact": False}
    assert client.post(f"/api/ni/items/{iid}/flow/access", json={"key": "has space"}).status_code == 400
    r = client.post(f"/api/ni/items/{iid}/flow/access", json={"key": " k-123 "})
    assert r.status_code == 200 and r.json()["started"] is True
    assert fired == [{"source_url": _KEYED_URL}]
    stored = json.loads(client.app.state.secret_store.get(f"ni:{iid}:api_key"))
    assert stored == {"value": "k-123", "host": "www.airnowapi.org"}
    rows = client.app.state.audit.list(limit=20)
    assert any(r["tool"] == "ni_flow_access" for r in rows)
    assert "k-123" not in json.dumps(rows, default=str)  # the audit names the host, never the key


def test_a_key_given_to_another_card_for_the_same_host_is_reused(client: TestClient, monkeypatch) -> None:
    nimod.put_credential(client.app.state.secret_store, "11111111-1111-1111-1111-111111111111",
                         "api_key", "k-old", "www.airnowapi.org")
    fired: list = []
    iid = _pick_pause(client, monkeypatch, [_KEYED_ROW], fired)
    r = client.post(f"/api/ni/items/{iid}/flow/pick-source", json={"url": _KEYED_URL})
    assert r.json() == {"ok": True, "started": True, "needs": []}
    assert json.loads(client.app.state.secret_store.get(f"ni:{iid}:api_key"))["value"] == "k-old"


def test_sec_asks_for_the_contact_email_once(client: TestClient, monkeypatch) -> None:
    fired: list = []
    iid = _pick_pause(client, monkeypatch, [_SEC_ROW], fired, "Apple's latest filings")
    r = client.post(f"/api/ni/items/{iid}/flow/pick-source", json={"url": _SEC_URL})
    assert r.json()["needs"] == ["contact"]
    r = client.post(f"/api/ni/items/{iid}/flow/access", json={"email": "me@example.com"})
    assert r.status_code == 200 and r.json()["started"] is True
    assert nimod.contact_email(client.app.state.secret_store) == "me@example.com"
    # a second SEC card never asks again
    iid2 = _pick_pause(client, monkeypatch, [_SEC_ROW], fired, "Microsoft's latest filings")
    assert client.post(f"/api/ni/items/{iid2}/flow/pick-source", json={"url": _SEC_URL}).json()["needs"] == []


def test_the_access_route_is_only_for_a_waiting_card(client: TestClient, monkeypatch) -> None:
    fired: list = []
    iid = _pick_pause(client, monkeypatch, [_KEYED_ROW], fired)
    r = client.post(f"/api/ni/items/{iid}/flow/access", json={"key": "k"})
    assert r.status_code == 409


# --- the built card carries refs, never values -------------------------------------------------------

def test_the_built_card_fetches_with_the_key_as_a_ref(monkeypatch) -> None:
    store, secrets = _stores()
    item_id = ni_flow.create_shell_item(store, "air quality in Charleston")
    nimod.put_credential(secrets, item_id, "api_key", "k-123", "www.airnowapi.org")
    record = ni_flow._flow_read(store, item_id)
    record["_access"] = {"url": _KEYED_URL, "host": "www.airnowapi.org", "provider": "AirNow",
                         "key": _KEYED_ROW["needs_key"], "contact": False}
    ni_flow._flow_write(store, item_id, record)
    ni_flow.set_secrets_provider(lambda: secrets)
    seen: dict = {}

    def fake(url, headers=None, allow_redirects=True):
        seen.update(url=url, allow_redirects=allow_redirects)
        return {"AQI": 42, "ParameterName": "PM2.5", "ReportingArea": "Charleston"}

    monkeypatch.setattr(netguard, "safe_fetch_json", fake)
    replies = [json.dumps({"kind": "external_data", "subject": "air quality", "cadence_minutes": 60,
                           "wants": ["aqi"], "threshold": None, "display_hint": "value"}),
               json.dumps({"aqi": "AQI"})]
    try:
        result = ni_flow.run_flow(store, item_id, gateway_call=lambda _m, _p: replies.pop(0) if replies else "{}",
                                  ni_route_model="ollama/test", source_url=_KEYED_URL)
    finally:
        ni_flow.set_secrets_provider(None)
    assert seen["url"].endswith("&API_KEY=k-123") and seen["allow_redirects"] is False
    assert result["state"] == "ready", result
    item = store.get_item(item_id)
    source = item["spec"]["source"]
    assert source["url"] == _KEYED_URL and "k-123" not in json.dumps(item["spec"])
    assert source["secret_query"] == {"API_KEY": {"$secret": f"ni:{item_id}:api_key"}}
    assert item["spec"]["params"]["api_key"]["kind"] == "secret"
    assert item["state"] == "commissioning"
