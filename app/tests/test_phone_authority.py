"""Route authority classes for a paired phone (2026-10-26; T: the Mac is the source, the
phone is display and control).

Three classes, declared in ``account``:
  * Desktop-only: ``dependencies=[account.DESKTOP_ONLY]`` on the route. Keys, pasted links,
    typed answers, trust, backups, pairing, tokens, MCP config, feeds, updates.
  * Phone-ok: nothing declared. Reads, driving a card the phone already has, and T's five
    fixed-choice consent taps (allow a cloud model, Activate, Keep looking, Skip this source,
    repair policy).
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
    # Settings writers (2026-10-26 review of #558): provider keys, the passphrase, where model
    # requests go, routing to cloud, the web-search provider, self-improvement. Settings is a
    # Desktop-only page already; the chat page's "connect the detected local model" tap is the
    # one phone-visible caller, and it now reads "Finish this on your Mac".
    "DELETE /api/local-models/{name}",
    "DELETE /api/secrets/{key}",
    "POST /api/account/passphrase",
    "PUT /api/local-models/claudecode",
    "PUT /api/local-models/mlx",
    "PUT /api/local-models/mlxe",
    "PUT /api/local-models/ollama",
    "PUT /api/local-models/voice",
    "PUT /api/ni/step-models",
    "PUT /api/routes",
    "PUT /api/secrets/{key}",
    "PUT /api/selfimprove",
    "PUT /api/selfimprove/optimizer",
    "PUT /api/websearch",
})

# Routes that refuse a phone for part of their input (``from_paired_phone`` in the body).
PARTIAL = frozenset({
    "POST /api/ni/intake",                       # no pasted link from a phone
    "POST /api/ni/items/{item_id}/flow/answer",  # only the model-consent choice from a phone
})

# Every other /api route: reachable with phone authority (the open routes among them need no
# credential at all). A route lands in exactly one of the three sets or the census fails, so a
# new route is classed deliberately rather than phone-reachable by default.
PHONE_OK = frozenset({
    "DELETE /api/agent/remembered/{name}",
    "DELETE /api/conversations",
    "DELETE /api/conversations/trash",
    "DELETE /api/conversations/{cid}",
    "DELETE /api/email/disconnect",
    "DELETE /api/kb/{doc_id}",
    "DELETE /api/library/local/{source_id}",
    "DELETE /api/memories/{mid}",
    "DELETE /api/ni/items/{item_id}",
    "DELETE /api/schedules/{sid}",
    "DELETE /api/tasks/{tid}",
    "DELETE /api/vaults/{vault_id}",
    "DELETE /api/vaults/{vault_id}/documents/{doc_id}",
    "GET /api/account/status",
    "GET /api/agent/pending",
    "GET /api/agent/remembered",
    "GET /api/audit",
    "GET /api/conversations",
    "GET /api/conversations/trash",
    "GET /api/conversations/{cid}",
    "GET /api/devices",
    "GET /api/devices/pair-code",
    "GET /api/email/messages",
    "GET /api/email/messages/{mid}",
    "GET /api/email/oauth/callback",
    "GET /api/email/status",
    "GET /api/feeds",
    "GET /api/health",
    "GET /api/kb",
    "GET /api/kb/index-status",
    "GET /api/kb/search",
    "GET /api/kb/{doc_id}",
    "GET /api/library/local",
    "GET /api/library/sources",
    "GET /api/library/sources/{source_id}",
    "GET /api/library/status",
    "GET /api/library/taxonomy",
    "GET /api/local-models",
    "GET /api/mcp",
    "GET /api/memories",
    "GET /api/metrics/summary",
    "GET /api/model-context-lengths",
    "GET /api/models",
    "GET /api/ni/board",
    "GET /api/ni/findings",
    "GET /api/ni/items/{item_id}",
    "GET /api/ni/items/{item_id}/flow",
    "GET /api/ni/items/{item_id}/image",
    "GET /api/ni/library",
    "GET /api/ni/mcp-servers",
    "GET /api/ni/measure",
    "GET /api/ni/step-models",
    "GET /api/profile",
    "GET /api/routes",
    "GET /api/schedules",
    "GET /api/schedules/runs/recent",
    "GET /api/schedules/updates/unseen-count",
    "GET /api/schedules/{sid}/runs",
    "GET /api/secrets",
    "GET /api/selfimprove",
    "GET /api/selfimprove/improvements",
    "GET /api/selfimprove/optimizer",
    "GET /api/status",
    "GET /api/status/overview",
    "GET /api/tasks",
    "GET /api/tools",
    "GET /api/usage",
    "GET /api/vaults",
    "GET /api/vaults/{vault_id}",
    "GET /api/voice/status",
    "GET /api/websearch",
    "PATCH /api/conversations/{cid}",
    "PATCH /api/kb/{doc_id}",
    "PATCH /api/ni/items/{item_id}",
    "PATCH /api/schedules/{sid}",
    "PATCH /api/tasks/{tid}",
    "PATCH /api/vaults/{vault_id}",
    "PATCH /api/vaults/{vault_id}/subscription",
    "POST /api/account/lock",
    "POST /api/account/setup",
    "POST /api/account/unlock",
    "POST /api/agent/pending/{pid}/approve",
    "POST /api/agent/pending/{pid}/deny",
    "POST /api/agent/resume/{turn_id}",
    "POST /api/agent/turn",
    "POST /api/agent/turn/events",
    "POST /api/agent/turn/stream",
    "POST /api/chat",
    "POST /api/conversations",
    "POST /api/conversations/{cid}/messages",
    "POST /api/conversations/{cid}/restore",
    "POST /api/email/connect",
    "POST /api/email/reconnect",
    "POST /api/email/send",
    "POST /api/feedback",
    "POST /api/feeds/{feed_id}/refresh",
    "POST /api/kb",
    "POST /api/kb/ingest-url",
    "POST /api/kb/reindex",
    "POST /api/kb/upload",
    "POST /api/library/install",
    "POST /api/library/local",
    "POST /api/memories",
    "POST /api/ni/findings/{finding_id}/resolve",
    "POST /api/ni/items/{item_id}/activate",
    "POST /api/ni/items/{item_id}/apply-template-update",
    "POST /api/ni/items/{item_id}/brief-summary",
    "POST /api/ni/items/{item_id}/commission",
    "POST /api/ni/items/{item_id}/flow/cancel",
    "POST /api/ni/items/{item_id}/flow/fix",
    "POST /api/ni/items/{item_id}/flow/keep-looking",
    "POST /api/ni/items/{item_id}/flow/skip-source",
    "POST /api/ni/items/{item_id}/flow/reopen",
    "POST /api/ni/items/{item_id}/flow/retry",
    "POST /api/ni/items/{item_id}/flow/stop-looking",
    "POST /api/ni/items/{item_id}/l2-proposal/apply",
    "POST /api/ni/items/{item_id}/l2-proposal/dismiss",
    "POST /api/ni/items/{item_id}/refine",
    "POST /api/ni/items/{item_id}/repair-policy",
    "POST /api/ni/items/{item_id}/run",
    "POST /api/ni/items/{item_id}/validate",
    "POST /api/ni/library/check",
    "POST /api/ni/library/install",
    "POST /api/schedules",
    "POST /api/schedules/updates/seen",
    "POST /api/schedules/{sid}/run",
    "POST /api/tasks",
    "POST /api/tools/invoke",
    "POST /api/vaults",
    "POST /api/vaults/import",
    "POST /api/vaults/subscribe",
    "POST /api/vaults/{vault_id}/check-updates",
    "POST /api/vaults/{vault_id}/documents",
    "POST /api/vaults/{vault_id}/documents/{doc_id}/detach",
    "POST /api/vaults/{vault_id}/update",
    "POST /api/voice/speak",
    "POST /api/voice/transcribe",
    "PUT /api/model-context-lengths",
    "PUT /api/ni/items/{item_id}/param",
    "PUT /api/profile",
    "PUT /api/schedules/{sid}",
    "PUT /api/tasks/{tid}",
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
    # the five fixed-choice consent taps (T's "yes taps", 2026-10-26 10:46Z; Skip this source since #570)
    ("POST", "/api/ni/items/nope/activate", None),
    ("POST", "/api/ni/items/nope/flow/keep-looking", None),
    ("POST", "/api/ni/items/nope/flow/skip-source", None),
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


def test_every_route_is_in_exactly_one_class(client: TestClient) -> None:
    """The three sets partition the live /api route list: a new route must be classed."""
    live = {f"{m} {p}" for m, p, _route in _api_routes(client)}
    assert len(live) > 150, "route discovery broke"
    assert not (DESKTOP_ONLY & PARTIAL) and not (DESKTOP_ONLY & PHONE_OK) and not (PARTIAL & PHONE_OK)
    assert live == DESKTOP_ONLY | PARTIAL | PHONE_OK, {
        "unclassed": sorted(live - DESKTOP_ONLY - PARTIAL - PHONE_OK),
        "gone": sorted((DESKTOP_ONLY | PARTIAL | PHONE_OK) - live)}


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
