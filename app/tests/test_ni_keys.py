"""P3 (release plan, T 2026-10-24): your own sources, with your own key.

A link the user pastes and says needs their key, or one that answers with a key wall (a 401, an API's JSON 403),
pauses the card: the user says where the key goes (a query parameter or a header, by name, with an optional prefix)
and gives it. The key is stored host-bound in the secret store, rides only https requests to that host as a ref
(never a value in the spec, never shown to a model). A key the user gave another card for the same host is only
offered, for the user's tap, never copied by itself: tenants share hosts and the remote decides the 401 (#549 review
B1). A research run that meets a key wall ends with Use my key for that host.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from test_ni_access import _pick_pause, _stores, client  # noqa: F401  (client is a fixture)
from test_ni_research_tools import _listed

from smartbrain_3000 import netguard, ni_flow, ni_master
from smartbrain_3000 import ni as nimod
from smartbrain_3000.ni_research import tools

_URL = "https://api.rates.example.org/v1/latest?base=USD"
_HOST = "api.rates.example.org"
_RATES = {"base": "USD", "rates": {"EUR": 0.92, "GBP": 0.79}}


def _wall(status: int = 401, content_type: str = "application/json", kind: str | None = None):
    return netguard.FetchError("refused", status=status, content_type=content_type, kind=kind)


# --- the rule: what a key opens ---------------------------------------------------------------------------------

@pytest.mark.parametrize("exc, walled", [
    (_wall(401), True),
    (_wall(401, "text/html"), True),
    (_wall(403), True),                                  # an API's JSON refusal
    (_wall(403, "text/html"), False),                    # a page's plain 403 is the page browser's
    (_wall(403, kind="challenge"), False),               # a bot check is no key wall
    (_wall(429, kind="rate_limited"), False),
    (_wall(404), False),
    (netguard.FetchError("dns"), False),
])
def test_a_key_wall_is_a_401_or_an_apis_json_403(exc, walled) -> None:
    assert netguard.key_wall(exc) is walled


# --- paste a link that needs your key -----------------------------------------------------------------------------

def _flow_of(client: TestClient, iid: str) -> dict:  # noqa: F811
    return next(x for x in client.get("/api/ni/board").json()["items"] if x["id"] == iid)["flow"]


def test_a_pasted_link_that_needs_a_key_asks_where_it_goes_then_for_the_key(client: TestClient, monkeypatch) -> None:  # noqa: F811
    fired: list = []
    iid = _pick_pause(client, monkeypatch, [], fired, "dollar exchange rates")
    r = client.post(f"/api/ni/items/{iid}/flow/pick-source", json={"url": _URL, "pasted": True, "needs_key": True})
    assert r.status_code == 200 and r.json() == {"ok": True, "started": False, "needs": ["key"]}
    assert fired == [], "nothing is fetched before the key"
    flow = _flow_of(client, iid)
    assert flow["state"] == "awaiting_access"
    assert flow["access"] == {"host": _HOST, "provider": _HOST,
                              "key": {"docs_url": "", "place": True, "reuse": False}, "contact": False}
    access = f"/api/ni/items/{iid}/flow/access"
    for bad in ({"key": "k-1"},                                              # where it goes is the user's to say
                {"key": "k-1", "where": "body", "name": "key"},
                {"key": "k-1", "where": "query", "name": "api key"},
                {"key": "k-1", "where": "header", "name": "Cookie"},         # a header that carries the request
                {"key": "k-1", "where": "header", "name": "X-Key", "prefix": "Bearer\n"}):
        assert client.post(access, json=bad).status_code == 400, bad
    r = client.post(access, json={"key": " k-1 ", "where": "header", "name": "Authorization", "prefix": "Bearer "})
    assert r.status_code == 200 and r.json()["started"] is True and fired == [{"source_url": _URL}]
    stored = json.loads(client.app.state.secret_store.get(f"ni:{iid}:api_key"))
    assert stored == {"value": "Bearer k-1", "host": _HOST, "named": True}, "bound to the host the user named"
    sealed = ni_flow._flow_read(client.app.state.ni, iid)["_access"]["key"]
    assert sealed == {"in": "header", "name": "Authorization", "prefix": "Bearer ", "docs_url": ""}
    assert "k-1" not in json.dumps(client.app.state.audit.list(limit=20), default=str)


def test_a_key_only_goes_with_a_pasted_https_link(client: TestClient, monkeypatch) -> None:  # noqa: F811
    fired: list = []
    iid = _pick_pause(client, monkeypatch, [], fired, "dollar exchange rates")
    pick = f"/api/ni/items/{iid}/flow/pick-source"
    assert client.post(pick, json={"url": _URL, "needs_key": True}).status_code == 400
    plain = "http://api.rates.example.org/v1/latest"
    assert client.post(pick, json={"url": plain, "pasted": True, "needs_key": True}).status_code == 400
    assert fired == []


_OTHER = "22222222-2222-2222-2222-222222222222"


def _other_card(store, secrets, monkeypatch, url: str = "https://api.rates.example.org/v1/latest?base=EUR",
                *, named: bool = True) -> None:
    """A live card on the same host whose key goes in the query."""
    source = {"type": "http_json", "url": url, "secret_query": {"apikey": {"$secret": f"ni:{_OTHER}:api_key"}}}
    real = store.list_items
    monkeypatch.setattr(store, "list_items", lambda: [*real(), {"id": _OTHER, "spec": {"source": source}}])
    nimod.put_credential(secrets, _OTHER, "api_key", "k-old", _HOST, named=named)


def test_a_key_for_the_same_host_is_offered_and_used_only_on_the_users_tap(client: TestClient, monkeypatch) -> None:  # noqa: F811
    store, secrets = client.app.state.ni, client.app.state.secret_store
    _other_card(store, secrets, monkeypatch)
    fired: list = []
    iid = _pick_pause(client, monkeypatch, [], fired, "dollar exchange rates")
    r = client.post(f"/api/ni/items/{iid}/flow/pick-source", json={"url": _URL, "pasted": True, "needs_key": True})
    assert r.json() == {"ok": True, "started": False, "needs": ["key"]}, "never copied by itself"
    assert secrets.get(f"ni:{iid}:api_key") is None and fired == []
    assert ni_flow.board_flow_field(store, iid)["access"]["key"] == {"docs_url": "", "place": True, "reuse": True}
    r = client.post(f"/api/ni/items/{iid}/flow/access", json={"reuse": True})
    assert r.status_code == 200 and r.json() == {"ok": True, "started": True, "needs": []}
    assert json.loads(secrets.get(f"ni:{iid}:api_key")) == {"value": "k-old", "host": _HOST, "named": True}
    assert ni_flow._flow_read(store, iid)["_access"]["key"]["in"] == "query", "with that card's placement"
    rows = [r for r in client.app.state.audit.list(limit=10) if r["tool"] == "ni_flow_access"]
    assert rows and json.loads(rows[0]["args_summary"])["reuse"] is True, "the reuse is audited"
    assert "k-old" not in json.dumps(client.app.state.audit.list(limit=20), default=str)


def test_reuse_is_refused_when_the_card_offers_none(client: TestClient, monkeypatch) -> None:  # noqa: F811
    fired: list = []
    iid = _pick_pause(client, monkeypatch, [], fired, "dollar exchange rates")
    client.post(f"/api/ni/items/{iid}/flow/pick-source", json={"url": _URL, "pasted": True, "needs_key": True})
    r = client.post(f"/api/ni/items/{iid}/flow/access", json={"reuse": True})
    assert r.status_code == 400 and "add it here" in r.json()["detail"] and fired == []


def test_a_key_for_one_address_never_rides_to_another_on_the_same_host(monkeypatch) -> None:
    """#549 review B1: two tenants of one host. The stranger's address answers 401; the user's key for their own
    address is never sent to it, on a plain paste or after Use my key, without the user's tap."""
    store, secrets = _stores()
    mine, stranger = "https://api.rates.example.org/s/MY-OWN/exec", "https://api.rates.example.org/s/STRANGER/exec"
    _other_card(store, secrets, monkeypatch, mine)
    sent: list = []

    def serve(url, headers=None, allow_redirects=True):
        sent.append((url, headers))
        raise _wall(401)
    monkeypatch.setattr(netguard, "safe_fetch_json", serve)
    for start in ("paste", "use_key"):
        item_id = ni_flow.create_shell_item(store, f"dollar exchange rates ({start})")
        if start == "use_key":
            record = _declined_with_wall(store, item_id, stranger)
            ni_flow.use_key_for(store, item_id, record, stranger)
            assert ni_flow.missing_access(store, item_id, secrets) == ["key"]
        else:
            ni_flow.set_secrets_provider(lambda: secrets)
            try:
                out = ni_flow.run_flow(store, item_id, gateway_call=lambda _m, _p: _replies()[0],
                                       ni_route_model="ollama/test", source_url=stranger)
            finally:
                ni_flow.set_secrets_provider(None)
            assert out["state"] == "awaiting_access", out
        assert secrets.get(f"ni:{item_id}:api_key") is None, start
        assert ni_flow.board_flow_field(store, item_id)["access"]["key"]["reuse"] is True, "offered, not taken"
    assert sent and all(h is None and "k-old" not in u for u, h in sent), "the stranger never saw the key"


def test_a_library_row_never_takes_a_key_given_for_a_named_address_by_itself(monkeypatch) -> None:
    store, secrets = _stores()
    _other_card(store, secrets, monkeypatch)                 # a key the user gave for an address they named
    item_id = ni_flow.create_shell_item(store, "dollar exchange rates")
    row = {"provider": "Rates", "needs_key": {"in": "query", "name": "apikey", "docs_url": ""}}
    ni_flow.seal_access(store, item_id, _URL, row)
    assert ni_flow.missing_access(store, item_id, secrets) == ["key"]
    assert secrets.get(f"ni:{item_id}:api_key") is None
    assert ni_flow.give_access(store, item_id, secrets, key=None, email=None, reuse=True) == []
    assert json.loads(secrets.get(f"ni:{item_id}:api_key"))["value"] == "k-old"
    library = ni_flow.create_shell_item(store, "euro exchange rates")     # a key given on a Library row is
    ni_flow.seal_access(store, library, _URL, row)                         # still reused by itself on the next one
    assert ni_flow.missing_access(store, library, secrets) == []


def test_a_fault_looking_for_a_same_host_key_asks_for_the_key(monkeypatch) -> None:
    """#549 review S1: the lookup is a convenience; a fault pauses for the key instead of failing the card."""
    store, secrets = _stores()
    nimod.put_credential(secrets, _OTHER, "api_key", "k-old", _HOST, named=True)

    def broken():
        raise RuntimeError("store down")
    item_id = ni_flow.create_shell_item(store, "dollar exchange rates")
    ni_flow.ask_for_key(store, item_id, _URL)
    monkeypatch.setattr(store, "list_items", broken)
    assert ni_flow.missing_access(store, item_id, secrets) == ["key"]
    assert ni_flow.board_flow_field(store, item_id)["access"]["key"]["reuse"] is False


# --- a link that answers with a key wall ------------------------------------------------------------------------

def _replies() -> list[str]:
    return [json.dumps({"kind": "external_data", "subject": "exchange rates", "cadence_minutes": 60,
                        "wants": ["eur"], "threshold": None, "display_hint": "value"}),
            json.dumps({"eur": "rates.EUR"})]


def _run(store, item_id: str, secrets, monkeypatch, serve) -> dict:
    monkeypatch.setattr(netguard, "safe_fetch_json", serve)
    replies = _replies()
    ni_flow.set_secrets_provider(lambda: secrets)
    try:
        return ni_flow.run_flow(store, item_id, gateway_call=lambda _m, _p: replies.pop(0) if replies else "{}",
                                ni_route_model="ollama/test", source_url=_URL)
    finally:
        ni_flow.set_secrets_provider(None)


def test_a_pasted_link_behind_a_key_wall_pauses_for_the_key(monkeypatch) -> None:
    store, secrets = _stores()
    item_id = ni_flow.create_shell_item(store, "dollar exchange rates")

    def serve(url, headers=None, allow_redirects=True):
        raise _wall(401)
    out = _run(store, item_id, secrets, monkeypatch, serve)
    assert out["state"] == "awaiting_access" and out["_pasted"] == _URL
    assert out["_access"]["key"]["in"] is None and out["_access"]["host"] == _HOST
    assert ni_flow.board_flow_field(store, item_id)["access"]["key"]["place"] is True


def test_a_key_given_builds_with_a_ref_and_a_refused_key_is_asked_again(monkeypatch) -> None:
    store, secrets = _stores()
    item_id = ni_flow.create_shell_item(store, "dollar exchange rates")
    ni_flow.ask_for_key(store, item_id, _URL)
    assert ni_flow.give_access(store, item_id, secrets, key="k-1", email=None, where="query", name="apikey") == []
    seen: list[str] = []

    def serve(url, headers=None, allow_redirects=True):
        seen.append(url)
        assert allow_redirects is False, "a key never follows a redirect"
        if not url.endswith("&apikey=k-1"):
            raise _wall(401)
        return dict(_RATES)
    out = _run(store, item_id, secrets, monkeypatch, serve)
    assert out["state"] == "ready", out
    source = store.get_item(item_id)["spec"]["source"]
    assert source["secret_query"] == {"apikey": {"$secret": f"ni:{item_id}:api_key"}}
    assert "k-1" not in json.dumps(store.get_item(item_id)["spec"])
    # the provider refuses the key later on a fresh card: the card asks again, with the placement to say again
    again = ni_flow.create_shell_item(store, "dollar exchange rates")
    ni_flow.ask_for_key(store, again, _URL)
    ni_flow.give_access(store, again, secrets, key="k-bad", email=None, where="query", name="apikey")

    def refuse(url, headers=None, allow_redirects=True):
        raise _wall(401)
    out = _run(store, again, secrets, monkeypatch, refuse)
    assert out["state"] == "awaiting_access" and out["_access"]["key"]["in"] is None
    assert "refused the key" in out["notes"][-1]
    assert secrets.get(f"ni:{again}:api_key") is None, "#549 review S4: a refused key is not kept"
    assert ni_flow.board_flow_field(store, again)["access"]["key"]["reuse"] is False, "nor offered again"


def test_retry_and_use_my_key_keep_where_the_users_key_goes(monkeypatch) -> None:
    """#549 review S3: a fresh start for the user's own address keeps the placement they gave, so the key they
    already gave this card is used again and nothing is asked twice."""
    store, secrets = _stores()
    item_id = ni_flow.create_shell_item(store, "dollar exchange rates")
    ni_flow.ask_for_key(store, item_id, _URL)
    ni_flow.give_access(store, item_id, secrets, key="k-1", email=None, where="query", name="apikey")
    record = ni_flow._flow_read(store, item_id)
    fresh = ni_flow._fresh_record(record, "dollar exchange rates", "intent", "retry", keep_named=True)
    assert fresh["_access"]["key"]["in"] == "query" and fresh["_pasted"] == _URL
    ni_flow._flow_write(store, item_id, fresh)
    assert ni_flow.missing_access(store, item_id, secrets) == []
    ni_flow.use_key_for(store, item_id, {**record, "state": "declined"}, _URL)
    assert ni_flow._flow_read(store, item_id)["_access"]["key"]["in"] == "query"
    assert ni_flow.missing_access(store, item_id, secrets) == []
    secrets.delete(f"ni:{item_id}:api_key")                    # the key went: the run asks for it, never fails

    def serve(url, headers=None, allow_redirects=True):
        raise AssertionError("nothing is fetched without the key")
    ni_flow._flow_write(store, item_id, fresh)
    out = _run(store, item_id, secrets, monkeypatch, serve)
    assert out["state"] == "awaiting_access" and out["_access"]["key"]["in"] == "query", out


def test_a_keyed_address_that_serves_csv_is_read_with_the_key(monkeypatch) -> None:
    """#549 review S2: an address the user named has no sealed format, so it is sniffed as a paste is, with the
    key on both reads and redirects refused."""
    store, secrets = _stores()
    item_id = ni_flow.create_shell_item(store, "dollar exchange rates")
    ni_flow.ask_for_key(store, item_id, _URL)
    ni_flow.give_access(store, item_id, secrets, key="k-1", email=None, where="header", name="X-Key")
    reads: list = []

    def as_json(url, headers=None, allow_redirects=True):
        reads.append(("json", headers, allow_redirects))
        raise netguard.FetchError("not json", kind="not_json", content_type="text/csv")

    def as_text(url, fmt, headers=None, allow_redirects=True):
        reads.append((fmt, headers, allow_redirects))
        return {"text": "currency,rate\nEUR,0.92\nGBP,0.79\n", "content_type": "text/csv"}
    monkeypatch.setattr(netguard, "safe_fetch_text", as_text)
    _run(store, item_id, secrets, monkeypatch, as_json)
    assert reads[:2] == [("json", {"X-Key": "k-1"}, False), ("sniff", {"X-Key": "k-1"}, False)], reads
    assert all(h == {"X-Key": "k-1"} and r is False for _f, h, r in reads), "never a read without the key"
    assert ni_flow._flow_read(store, item_id).get("_format") == "csv"


# --- research: Use my key -----------------------------------------------------------------------------------------

def test_a_research_fetch_behind_a_key_wall_is_remembered_and_reads_no_key(monkeypatch) -> None:
    ctx, net = _listed(monkeypatch, _URL, _RATES)
    net.fail[_URL] = _wall(401)
    got = tools.fetch_sample(ctx, _URL)
    assert got["ok"] is False and got["error"] == "needs_key"
    assert ctx.key_walls == [_URL] and net.calls[-1]["headers"] is None


def _declined_with_wall(store, item_id: str, url: str | None = _URL) -> dict:
    record = ni_flow._flow_read(store, item_id)
    seal = ni_master.seal_decline("research_declined_budget", "", [], key_wall=url)
    ni_flow._flow_write(store, item_id, {**record, "state": "declined", "error": "research_gave_up: x",
                                         "_decline_card": seal})
    return ni_flow._flow_read(store, item_id)


def test_a_decline_that_met_a_key_wall_offers_use_my_key_for_its_host(monkeypatch) -> None:
    store, _secrets = _stores()
    item_id = ni_flow.create_shell_item(store, "dollar exchange rates")
    _declined_with_wall(store, item_id)
    field = ni_flow.board_flow_field(store, item_id)
    assert field["decline"]["key_wall"] == {"host": _HOST}, "the host, never the address"
    assert field["reopen"][0] == "use_key" and "use_key" in field["actions"]
    _declined_with_wall(store, item_id, "http://api.rates.example.org/v1")   # a key never rides http
    field = ni_flow.board_flow_field(store, item_id)
    assert field["decline"]["key_wall"] is None and "use_key" not in field["actions"]


def test_use_my_key_asks_for_the_key_for_that_address(client: TestClient, monkeypatch) -> None:  # noqa: F811
    fired: list = []
    store = client.app.state.ni
    iid = _pick_pause(client, monkeypatch, [], fired, "dollar exchange rates")
    _declined_with_wall(store, iid, None)
    assert client.post(f"/api/ni/items/{iid}/flow/use-key").status_code == 409, "not offered without a wall"
    _declined_with_wall(store, iid)
    r = client.post(f"/api/ni/items/{iid}/flow/use-key")
    assert r.status_code == 200 and r.json() == {"ok": True, "started": False, "needs": ["key"]}
    record = ni_flow._flow_read(store, iid)
    assert record["state"] == "awaiting_access" and record["_pasted"] == _URL
    assert record.get("_decline_card") is None, "a fresh start: nothing the decline sealed rides along"
    assert record["request"] == "dollar exchange rates" and fired == []
    rows = client.app.state.audit.list(limit=10)
    assert any(r["tool"] == "ni_use_key" for r in rows)


def test_a_key_in_a_header_rides_as_a_ref_with_its_prefix(monkeypatch) -> None:
    store, secrets = _stores()
    item_id = ni_flow.create_shell_item(store, "dollar exchange rates")
    ni_flow.ask_for_key(store, item_id, _URL)
    ni_flow.give_access(store, item_id, secrets, key="k-1", email=None, where="header", name="Authorization",
                        prefix="Bearer ")

    def serve(url, headers=None, allow_redirects=True):
        if (headers or {}).get("Authorization") != "Bearer k-1" or url != _URL:
            raise _wall(401)
        return dict(_RATES)
    out = _run(store, item_id, secrets, monkeypatch, serve)
    assert out["state"] == "ready", out
    spec = store.get_item(item_id)["spec"]
    assert spec["source"]["headers"] == {"Authorization": {"$secret": f"ni:{item_id}:api_key"}}
    assert "k-1" not in json.dumps(spec)
