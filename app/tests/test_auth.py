"""R14 local API credential (auth.py) — the security properties, end to end.

Operator rulings 2026-09-23: every /api request carries a credential; browser
sessions last one app run and survive Lock; LAN-direct browsers get phone
authority; "Desktop-only" is decided by credential authority, never a header.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

from smartbrain_3000 import (
    auth,
    devices,
    pairing_code,
    pairing_host,
    webrtc_bridge,
    webrtc_peer,
)

_PASS = "correct-horse-battery"


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "auth.duckdb"))
    from smartbrain_3000.main import create_app

    application = create_app()
    with TestClient(application):  # run the lifespan once (DB, token, sessions)
        yield application


def _browser(app, host: str = "localhost") -> TestClient:
    """A browser: no bearer, cookies only, addressed to ``host``."""
    return TestClient(app, base_url=f"http://{host}", sb_auth=False)


def _all_routes(app) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []

    def walk(routes) -> None:
        for r in routes:
            if type(r).__name__ == "_IncludedRouter":
                walk(r.original_router.routes)
            elif hasattr(r, "methods"):
                out.extend((m, r.path) for m in sorted(r.methods))

    walk(app.routes)
    return out


# ---- the inventory: nothing outside the allowlist answers without a credential ---


def test_every_api_route_refuses_without_a_credential(app) -> None:
    """Walks EVERY route (FastAPI keeps included routers as _IncludedRouter —
    original_router holds the real routes), so a route added later is covered with
    no test change: only auth.OPEN_ROUTES may answer an anonymous request. Unlocked,
    so every refusal is the guard's own 401 (a locked vault answers 423 — see below)."""
    TestClient(app).post("/api/account/setup", json={"passphrase": _PASS})
    anon = TestClient(app, sb_auth=False)
    routes = [(m, p) for m, p in _all_routes(app) if p.startswith("/api")]
    assert len(routes) > 150, "route discovery broke — the inventory would be vacuous"
    leaked = []
    for method, path in routes:
        if (method, path) in auth.OPEN_ROUTES:
            continue
        concrete = path
        while "{" in concrete:  # fill every path parameter
            start = concrete.index("{")
            concrete = concrete[:start] + "x" + concrete[concrete.index("}", start) + 1:]
        r = anon.request(method, concrete)
        if r.status_code != 401 or r.json().get("code") != "no_session":
            leaked.append((method, path, r.status_code))
    assert leaked == [], f"reachable without a credential: {leaked}"


def test_a_locked_vault_refuses_anonymous_calls_with_423(app) -> None:
    """Locked, a caller with no credential gets the ordinary 423 — the page's existing
    locked path (and the previous release's page, open across an update) — never data;
    unlocked, the same call is the guard's 401 ("open SmartBrain in this browser")."""
    desk = TestClient(app)
    desk.post("/api/account/setup", json={"passphrase": _PASS})
    desk.post("/api/account/lock")
    anon = TestClient(app, sb_auth=False)
    locked = anon.get("/api/devices")
    assert locked.status_code == 423 and locked.json() == {"detail": "locked: unlock first"}
    desk.post("/api/account/unlock", json={"passphrase": _PASS})
    assert anon.get("/api/devices").json()["code"] == "no_session"


def test_open_routes_are_real_routes(app) -> None:
    """The allowlist names only routes that exist (no stale entries to hide behind)."""
    real = set(_all_routes(app))
    for method, path in auth.OPEN_ROUTES:
        assert (method, path) in real or (method == "HEAD" and ("GET", path) in real), (method, path)


def test_mcp_keeps_its_own_bearer(app) -> None:
    """The desktop token is not an MCP token (separate surfaces, separate secrets)."""
    assert TestClient(app).get("/mcp/").status_code == 401


# ---- credentials and authority ----------------------------------------------


def test_bearer_is_desktop_and_a_wrong_one_is_nothing(app) -> None:
    good = TestClient(app)
    good.post("/api/account/setup", json={"passphrase": _PASS})
    assert good.get("/api/mcp/token").status_code == 200  # desktop-only route
    bad = TestClient(app, sb_auth=False, headers={"Authorization": "Bearer " + "z" * 40})
    assert bad.get("/api/devices").status_code == 401


def test_browser_session_minted_at_setup_is_desktop_on_loopback(app) -> None:
    browser = _browser(app)
    r = browser.post("/api/account/setup", json={"passphrase": _PASS})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert "sb_session=" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert browser.get("/api/account/status").json()["session"] is True
    assert browser.get("/api/mcp/token").status_code == 200  # desktop authority


def test_lan_direct_browser_gets_phone_authority(app) -> None:
    """Operator ruling: the web app loaded from https://<LAN-IP> is a phone."""
    TestClient(app).post("/api/account/setup", json={"passphrase": _PASS})
    lan = _browser(app, host="testserver")  # a non-loopback Host (allow-listed in tests)
    assert lan.post("/api/account/unlock", json={"passphrase": _PASS}).status_code == 200
    assert lan.get("/api/devices").status_code == 200           # ordinary route: fine
    assert lan.get("/api/mcp/token").status_code == 403         # desktop-only: refused
    assert lan.post("/api/devices", json={"label": "x"}).status_code == 403


def test_a_lan_session_replayed_with_a_loopback_host_stays_a_phone(app) -> None:
    """The session's authority is fixed when it is minted: re-sending a LAN session's
    cookie with ``Host: localhost`` (any non-browser client can) gains nothing."""
    TestClient(app).post("/api/account/setup", json={"passphrase": _PASS})
    lan = _browser(app, host="testserver")
    assert lan.post("/api/account/unlock", json={"passphrase": _PASS}).status_code == 200
    replay = _browser(app)  # Host: localhost
    replay.cookies.set(auth.COOKIE_NAME, lan.cookies.get(auth.COOKIE_NAME))
    assert replay.get("/api/devices").status_code == 200    # still a session...
    assert replay.get("/api/mcp/token").status_code == 403  # ...with phone authority


def test_second_browser_opens_here_without_resetting_the_unlock(app) -> None:
    """Operator ruling: another browser enters the passphrase once; that must NOT
    re-run the unlock (a new unlock session would hide every pending approval)."""
    first = _browser(app)
    first.post("/api/account/setup", json={"passphrase": _PASS})
    session_before = app.state.session_id
    second = _browser(app)
    st = second.get("/api/account/status").json()
    assert st["unlocked"] is True and st["session"] is False  # "open SmartBrain in this browser"
    assert second.get("/api/devices").status_code == 401
    assert second.post("/api/account/unlock", json={"passphrase": "wrong-guess-here"}).status_code == 401
    assert second.get("/api/devices").status_code == 401
    assert second.post("/api/account/unlock", json={"passphrase": _PASS}).status_code == 200
    assert second.get("/api/devices").status_code == 200
    assert app.state.session_id == session_before


def test_sessions_survive_lock_so_a_phone_unlock_walks_the_desk_in(app) -> None:
    """Operator ruling: once per browser per app RUN — Lock doesn't drop sessions."""
    desk = _browser(app)
    desk.post("/api/account/setup", json={"passphrase": _PASS})
    desk.post("/api/account/lock")
    st = desk.get("/api/account/status").json()
    assert st["unlocked"] is False and st["session"] is True
    phone = TestClient(app, sb_auth=False, headers=auth.relay_headers("phone-1"))
    r = phone.post("/api/account/unlock", json={"passphrase": _PASS})
    assert r.status_code == 200
    assert "set-cookie" not in r.headers  # the relay is the phone's credential
    assert desk.get("/api/devices").status_code == 200  # walked back in, no passphrase


def test_a_restart_clears_sessions(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "restart.duckdb"))
    from smartbrain_3000.main import create_app

    first = create_app()
    with TestClient(first):
        browser = _browser(first)
        browser.post("/api/account/setup", json={"passphrase": _PASS})
        cookie = browser.cookies.get(auth.COOKIE_NAME)
    second = create_app()
    with TestClient(second):
        stale = _browser(second)
        stale.cookies.set(auth.COOKIE_NAME, cookie)
        assert stale.get("/api/account/status").json()["session"] is False
        TestClient(second).post("/api/account/unlock", json={"passphrase": _PASS})
        assert stale.get("/api/devices").status_code == 401


def test_a_forged_relay_header_is_nothing(app) -> None:
    TestClient(app).post("/api/account/setup", json={"passphrase": _PASS})
    forged = TestClient(app, sb_auth=False, headers={auth.RELAY_HEADER: "f" * 64})
    assert forged.get("/api/devices").status_code == 401


def test_garbage_credentials_are_refused_not_crashed(app) -> None:
    """Header values can carry any byte: a non-ASCII credential is a 401, never a
    500 — and a websocket without a credential is denied the same way."""
    TestClient(app).post("/api/account/setup", json={"passphrase": _PASS})
    anon = TestClient(app, sb_auth=False, raise_server_exceptions=False)
    for hdr in ({"Authorization": b"Bearer \xe9" + b"a" * 40}, {auth.RELAY_HEADER: b"\xe9" * 64}):
        assert anon.get("/api/devices", headers=hdr).status_code == 401
    with pytest.raises(WebSocketDenialResponse) as denied, anon.websocket_connect("/api/devices"):
        pass
    assert denied.value.status_code == 401


def test_the_bridge_cannot_be_used_to_smuggle_a_desktop_credential(app) -> None:
    """A phone that sends a bearer or its own relay header gets neither through:
    parse_request drops them, the bridge attaches the REAL relay credential, and
    the request lands with phone authority — never desktop."""
    TestClient(app).post("/api/account/setup", json={"passphrase": _PASS})
    downstream = TestClient(app, sb_auth=False)
    frame = {"id": "1", "method": "GET", "path": "/api/mcp/token", "body": b"",
             "headers": {"authorization": f"Bearer {os.environ['SMARTBRAIN_LOCAL_TOKEN']}",
                         auth.RELAY_HEADER: "forged", "cookie": "sb_session=x"}}
    assert webrtc_bridge.handle_frame(frame, downstream, "phone-1")["status"] == 403
    ok = dict(frame, id="2", path="/api/devices")
    assert webrtc_bridge.handle_frame(ok, downstream, "phone-1")["status"] == 200


# ---- the local token ---------------------------------------------------------


def test_local_token_file_is_created_0600_and_reused(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(auth.TOKEN_ENV, raising=False)
    db_path = tmp_path / "data" / "x.duckdb"
    db_path.parent.mkdir()
    first = auth.load_local_token(db_path)
    token_file = db_path.parent / auth.TOKEN_FILE
    assert token_file.read_text().strip() == first
    if os.name == "posix":
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert auth.load_local_token(db_path) == first  # stable across restarts


def test_launcher_env_token_wins_and_a_weak_one_is_refused(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(auth.TOKEN_ENV, "L" * 48)
    assert auth.load_local_token(tmp_path / "x.duckdb") == "L" * 48
    assert not (tmp_path / auth.TOKEN_FILE).exists()  # the launcher owns it; no file
    monkeypatch.setenv(auth.TOKEN_ENV, "short")
    with pytest.raises(ValueError):
        auth.load_local_token(tmp_path / "x.duckdb")


# ---- ride-alongs ---------------------------------------------------------------


def test_health_writes_need_a_credential(app) -> None:
    from smartbrain_3000 import db as dbmod

    anon = TestClient(app, sb_auth=False)
    anon.get("/api/health", headers={"x-smartbrain-timezone": "Asia/Tokyo",
                                      "x-smartbrain-launcher": "6.6.6"})
    assert dbmod.meta_get(app.state.dbx, "user:timezone") != "Asia/Tokyo"
    assert dbmod.meta_get(app.state.dbx, "launcher:version") != "6.6.6"
    TestClient(app).get("/api/health", headers={"x-smartbrain-timezone": "Asia/Tokyo"})
    assert dbmod.meta_get(app.state.dbx, "user:timezone") == "Asia/Tokyo"


def test_passphrase_reset_reproves_the_recovery_key(app) -> None:
    c = TestClient(app)
    kit = c.post("/api/account/setup", json={"passphrase": _PASS}).json()
    c.post("/api/account/lock")
    c.post("/api/account/unlock", json={"recovery_key": kit["recovery_key"]})
    wrong = c.post("/api/account/passphrase/reset",
                   json={"new_passphrase": "hijacked-pass", "recovery_key": "SB-WRONG-KEY"})
    assert wrong.status_code == 401
    c.post("/api/account/lock")
    assert c.post("/api/account/unlock", json={"passphrase": _PASS}).status_code == 200  # unchanged


def test_api_docs_pages_are_gone(app) -> None:
    c = TestClient(app)
    for path in ("/docs", "/redoc", "/openapi.json"):
        body = c.get(path).text
        assert "swagger" not in body.lower() and '"openapi"' not in body, path


def test_pairing_mints_the_device_only_when_the_code_is_proven() -> None:
    """An expired or wrong-code session must leave no credential behind."""
    minted: list[int] = []

    def factory() -> dict:
        minted.append(1)
        return {"deviceId": "d1"}

    class _Chan:
        def __init__(self) -> None:
            self.sent: list[dict] = []

        def send(self, text: str) -> None:
            self.sent.append(json.loads(text))

    _room, key = pairing_code.derive(pairing_code.generate_code())
    session = {"nonce2": b"n" * 16, "binding": b"b" * 32}

    def state() -> dict:
        return {"done": asyncio.Event(), "ok": False, "guesses": 0}

    wrong = {"mac": base64.b64encode(b"x" * 32).decode()}
    st, chan = state(), _Chan()
    pairing_host._handle_pconfirm(chan, key, factory, session, st, wrong)
    assert minted == [] and chan.sent[-1]["type"] == "perror"
    good = {"mac": base64.b64encode(pairing_code.mac(key, "guest", session["nonce2"],
                                                      session["binding"])).decode()}
    st, chan = state(), _Chan()
    pairing_host._handle_pconfirm(chan, key, factory, session, st, good)
    assert minted == [1] and st["ok"] and chan.sent[-1]["type"] == "ppayload"

    def locked() -> dict:
        raise RuntimeError("locked")

    st, chan = state(), _Chan()
    pairing_host._handle_pconfirm(chan, key, locked, session, st, good)
    assert not st["ok"] and chan.sent[-1]["type"] == "perror" and st["done"].is_set()


# ---- the phone path across a Lock ---------------------------------------------


class _Channel:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, text: str) -> None:
        self.sent.append(json.loads(text))


def _relay(app, store, device_id: str, path: str) -> int:
    chan = _Channel()
    msg = {"id": "r1", "method": "GET", "path": path, "headers": {}, "body_b64": ""}
    session = {"authed": True, "device_id": device_id, "inflight": 0}
    asyncio.run(webrtc_peer._handle_request(chan, msg, TestClient(app, sb_auth=False),
                                            store, session))
    return chan.sent[-1]["status"]


def test_an_authed_phone_keeps_working_across_lock_without_the_key(app) -> None:
    """The peer no longer holds the key-bearing store: while LOCKED it checks
    revocation via the plaintext digests and still relays — so "tap to unlock
    from your phone" works — while a revoked device is refused."""
    c = TestClient(app)
    c.post("/api/account/setup", json={"passphrase": _PASS})
    store = app.state.secret_store
    dev = devices.create_device(store, "phone")
    gone = devices.create_device(store, "old phone")
    devices.revoke_device(store, gone["device_id"])
    c.post("/api/account/lock")
    assert _relay(app, None, dev["device_id"], "/api/account/status") == 200
    assert _relay(app, None, dev["device_id"], "/api/devices") == 423  # the API enforces lock
    assert _relay(app, None, gone["device_id"], "/api/account/status") == 401


def test_a_phone_paired_before_the_digest_set_heals_on_its_next_auth(app) -> None:
    """Phones paired before v0.9.35 have no plaintext digest — which the locked path
    checks — so their first auth while unlocked records it; they keep working across
    a Lock instead of reading as revoked."""
    c = TestClient(app)
    c.post("/api/account/setup", json={"passphrase": _PASS})
    store = app.state.secret_store
    dev = devices.create_device(store, "old phone")
    devices._update_known(dev["device_id"], add=False)  # as if paired before v0.9.35
    assert not devices.is_known_device_id(dev["device_id"])
    assert devices.verify_device(store, dev["device_id"], dev["credential"])
    c.post("/api/account/lock")
    assert _relay(app, None, dev["device_id"], "/api/account/status") == 200


def test_store_getter_is_resolved_per_message() -> None:
    calls: list[int] = []

    def getter():
        calls.append(1)

    async def run() -> None:
        session = {"authed": False, "device_id": None, "inflight": 0, "pc": object()}
        chan = _Channel()
        chan.close = lambda: None
        await webrtc_peer._serve_message(chan, json.dumps({"type": "hello", "nonce": ""}),
                                         None, getter, session)

    asyncio.run(run())
    assert calls == [1]


def test_no_route_left_on_the_old_header(app) -> None:
    """X-SB-Local is not a credential: without one, it opens nothing."""
    TestClient(app).post("/api/account/setup", json={"passphrase": _PASS})
    anon = TestClient(app, sb_auth=False, headers={"x-sb-local": "1"})
    assert anon.get("/api/mcp/token").status_code == 401
