"""Route authority classes for a paired phone (2026-10-26; T: the Mac is the source, the
phone is display and control).

Three classes, declared in ``account``:
  * Desktop-only: ``dependencies=[account.DESKTOP_ONLY]`` on the route. Keys, pasted links,
    typed answers, trust, backups, pairing, tokens, MCP config, feeds, updates.
  * Phone-ok: nothing declared. Reads, driving a card the phone already has, and T's four
    fixed-choice consent taps (allow a cloud model, Activate, Keep looking, repair policy).
  * Partial: ``from_paired_phone`` inside exactly two routes (intake without a link; answer
    for the model-consent choice only).

The census below reads the declarations off the live app, so a guard hidden in a helper
cannot escape it, and the frozen sets make a new Desktop-only or partial route a deliberate,
reviewed edit. The audit chokepoint test shows a phone's user actions carry ``via``.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import account, auth
from smartbrain_3000.auth import relay_headers

_PHONE = relay_headers("phone-under-test")
_PACKAGE = Path(account.__file__).parent

# Every route that refuses a phone outright. Adding to or shrinking this set needs T's decision.
DESKTOP_ONLY = frozenset({
    "DELETE /api/devices/pair-code",
    "DELETE /api/devices/{device_id}",
    "DELETE /api/feeds/{feed_id}",
    "DELETE /api/mcp/token",
    "DELETE /api/ni/library",
    "DELETE /api/ni/mcp-servers/{server_id}",
    "GET /api/mcp/token",
    "GET /api/ni/items/{item_id}/export-template",
    "GET /api/ni/notices",
    "POST /api/account/passphrase/reset",
    "POST /api/backup",
    "POST /api/devices",
    "POST /api/devices/pair-code",
    "POST /api/export",
    "POST /api/feeds",
    "POST /api/local-models/claudecode/update",
    "POST /api/mcp/token",
    "POST /api/ni/items/{item_id}/flow/access",
    "POST /api/ni/items/{item_id}/flow/pick-source",
    "POST /api/ni/items/{item_id}/flow/use-key",
    "POST /api/ni/items/{item_id}/switch-feed",
    "POST /api/ni/library/connect",
    "POST /api/ni/library/trust-key",
    "POST /api/ni/mcp-servers",
    "POST /api/restore",
    "POST /api/update/install",
    "POST /api/vaults/{vault_id}/export",
    "POST /api/vaults/{vault_id}/key",
    "POST /api/vaults/{vault_id}/retire",
    "POST /api/vaults/{vault_id}/trust-publisher",
    "POST /api/vaults/{vault_id}/verify-hosted",
    "PUT /api/ni/items/{item_id}/credential",
    "PUT /api/ni/mcp-servers/{server_id}",
})

# Routes that refuse a phone for part of their input (``from_paired_phone`` in the body).
PARTIAL = frozenset({
    "POST /api/ni/intake",                       # no pasted link from a phone
    "POST /api/ni/items/{item_id}/flow/answer",  # only the model-consent choice from a phone
})

PHONE_OK_NI = [
    ("POST", "/api/ni/items/nope/flow/retry", None),
    ("POST", "/api/ni/items/nope/flow/cancel", None),
    ("POST", "/api/ni/items/nope/flow/fix", None),
    ("POST", "/api/ni/items/nope/flow/reopen", None),
    ("POST", "/api/ni/items/nope/flow/stop-looking", None),
    ("POST", "/api/ni/items/nope/refine", {"note": "every hour"}),
    ("PUT", "/api/ni/items/nope/param", {"name": "x", "value": "y"}),
    ("POST", "/api/ni/items/nope/brief-summary", {"on": True}),
    ("POST", "/api/ni/findings/nope/resolve", None),
    # T's four fixed-choice consent taps ("yes taps", 2026-10-26 10:46Z)
    ("POST", "/api/ni/items/nope/activate", None),
    ("POST", "/api/ni/items/nope/flow/keep-looking", None),
    ("POST", "/api/ni/items/nope/repair-policy", {"l1": True}),
    ("POST", "/api/ni/items/nope/flow/answer", {"kind": "model_consent", "value": "allow"}),
]
PHONE_OK_READS = ["/api/ni/findings", "/api/ni/measure"]


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "phone.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        assert test_client.post("/api/account/setup", json={"passphrase": "correct-horse"}).status_code == 200
        yield test_client


def _api_routes(client: TestClient):
    """Every (method, path, route) under /api, through FastAPI's include_router wrappers."""
    def walk(routes):
        for route in routes:
            inner = getattr(route, "original_router", None)
            if inner is not None:
                yield from walk(inner.routes)
            else:
                yield route
    for route in walk(client.app.routes):
        if getattr(route, "endpoint", None) is None or not getattr(route, "methods", None):
            continue
        if not str(route.path).startswith("/api"):
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            yield method, route.path, route


def _declared_desktop_only(route) -> bool:
    return any(dep.call is account._require_desktop_local for dep in route.dependant.dependencies)


def test_the_desktop_only_set_is_frozen(client: TestClient) -> None:
    """Read off the live routes' declarations, not their source text."""
    declared = {f"{m} {p}" for m, p, route in _api_routes(client) if _declared_desktop_only(route)}
    assert declared == DESKTOP_ONLY


def test_the_declaration_is_the_only_way_to_be_desktop_only() -> None:
    """No route body or helper calls the guard: a guard the census cannot see must not exist."""
    callers = []
    for path in sorted(_PACKAGE.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"_require_desktop_local\(", text):
            if path.name == "account.py" and "def _require_desktop_local(" in text[match.start() - 4:match.end()]:
                continue  # its definition
            if text[match.start() - 8:match.start()] == "Depends(":
                continue  # the one declaration, account.DESKTOP_ONLY
            callers.append(f"{path.name}:{text.count(chr(10), 0, match.start()) + 1}")
    assert callers == []


def test_the_partial_set_is_frozen(client: TestClient) -> None:
    partial = {f"{m} {p}" for m, p, route in _api_routes(client)
               if "from_paired_phone(" in inspect.getsource(route.endpoint)}
    assert partial == PARTIAL
    # ... and no helper hides a third one: every textual use outside account.py sits in those two.
    uses = sum(path.read_text(encoding="utf-8").count("from_paired_phone(")
               for path in _PACKAGE.glob("*.py") if path.name != "account.py")
    assert uses == len(PARTIAL)


@pytest.mark.parametrize("line", sorted(DESKTOP_ONLY))
def test_a_phone_is_told_to_finish_on_the_mac(client: TestClient, line: str) -> None:
    method, path = line.split(" ", 1)
    path = path.replace("{item_id}", "nope").replace("{vault_id}", "nope") \
        .replace("{device_id}", "nope").replace("{feed_id}", "nope").replace("{server_id}", "nope")
    r = client.request(method, path, headers=_PHONE, json={})
    assert r.status_code == 403, r.text  # before validation, before any lookup
    assert r.json()["detail"] == account.DESKTOP_ONLY_DETAIL
    assert "on your Mac" in account.DESKTOP_ONLY_DETAIL


@pytest.mark.parametrize(("method", "path", "body"), PHONE_OK_NI)
def test_a_phone_can_drive_its_cards(client: TestClient, method: str, path: str, body: dict | None) -> None:
    r = client.request(method, path, headers=_PHONE, json=body)
    assert r.status_code == 404, r.text  # the authority passed; the card just does not exist


@pytest.mark.parametrize("path", PHONE_OK_READS)
def test_a_phone_can_read_findings_and_measure(client: TestClient, path: str) -> None:
    assert client.get(path, headers=_PHONE).status_code == 200


def test_a_phone_can_ask_for_a_card_but_not_with_a_link(client: TestClient, monkeypatch) -> None:
    from smartbrain_3000 import ni_flow
    fired: dict = {}
    monkeypatch.setattr(ni_flow, "start_flow_worker", lambda store, iid, **kw: fired.update(id=iid, **kw) or True)
    r = client.post("/api/ni/intake", headers=_PHONE, json={"request": "Oregon wildfires today"})
    assert r.status_code == 200, r.text
    assert fired["id"] == r.json()["id"] and fired.get("source_url") is None
    link = client.post("/api/ni/intake", headers=_PHONE,
                       json={"request": "Oregon wildfires", "source_url": "https://example.com/a.json"})
    assert link.status_code == 403 and link.json()["detail"] == account.DESKTOP_ONLY_DETAIL
    blank = client.post("/api/ni/intake", headers=_PHONE, json={"request": "Oregon wildfires", "source_url": "  "})
    assert blank.status_code == 200 and fired.get("source_url") is None  # a blank link is no link


def test_a_phone_may_only_answer_the_model_consent_question(client: TestClient) -> None:
    typed = client.post("/api/ni/items/nope/flow/answer", headers=_PHONE,
                        json={"kind": "supply_date", "value": "2026-01-01"})
    assert typed.status_code == 403 and typed.json()["detail"] == account.DESKTOP_ONLY_DETAIL
    spaced = client.post("/api/ni/items/nope/flow/answer", headers=_PHONE,
                         json={"kind": "model_consent ", "value": "allow"})
    assert spaced.status_code == 403  # the kind is matched exactly


def test_user_audit_rows_say_where_they_came_from(client: TestClient, monkeypatch) -> None:
    """The chokepoint (AuditLog.append) stamps ``via`` from the request being served, so a
    route that forgets nothing is the only kind of route. args_summary stays JSON."""
    import json

    from smartbrain_3000 import ni_flow
    monkeypatch.setattr(ni_flow, "start_flow_worker", lambda store, iid, **kw: True)
    assert client.post("/api/ni/intake", headers=_PHONE, json={"request": "pollen in Portland"}).status_code == 200
    assert client.post("/api/ni/intake", json={"request": "pollen in Seattle"}).status_code == 200
    rows = [e for e in client.get("/api/audit").json()["entries"] if e["tool"] == "ni_intake"]
    assert len(rows) == 2
    by_text = {json.loads(e["args_summary"])["request"]: e for e in rows}
    assert by_text["pollen in Portland"]["via"] == auth.PHONE_VIA
    assert by_text["pollen in Seattle"]["via"] == ""
    assert all("paired phone" not in e["args_summary"] for e in rows)


def test_via_tells_a_lan_browser_from_a_phone() -> None:
    relayed = {"headers": [(b"x-sb-relay", b"anything")]}
    assert auth.via_of(relayed, auth.REMOTE) == auth.PHONE_VIA
    assert auth.via_of({"headers": [(b"host", b"192.168.1.9:33000")]}, auth.REMOTE) == auth.LAN_VIA
    assert auth.via_of(relayed, auth.DESKTOP) is None
    assert auth.via_of(relayed, None) is None
    assert auth.request_via() is None  # outside a request


def test_assistant_rows_never_carry_via(client: TestClient) -> None:
    token = auth._REQUEST_VIA.set(auth.PHONE_VIA)
    try:
        client.app.state.audit.append("assistant", "kb_search", "observe", "auto", True, args_summary="{}")
        client.app.state.audit.append("user", "ni_flow_cancel", "reviewed", "executed", True, args_summary="{}")
    finally:
        auth._REQUEST_VIA.reset(token)
    rows = {e["tool"]: e for e in client.get("/api/audit").json()["entries"]}
    assert rows["kb_search"]["via"] == "" and rows["ni_flow_cancel"]["via"] == auth.PHONE_VIA
