"""Tests for the signaling broker + the Desktop signaling loop — Phase 3b.

The broker lives at the repo root (``signaling/server.py``) so the VPS image stays
tiny (websockets only); these tests import it via the repo root and skip cleanly if
it isn't mounted. The full-loop test wires broker + run_signaling + a real aiortc
"phone" + the auth-on-channel peer, proving the whole local path end to end.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import pathlib
import sys
from collections.abc import Iterator

import duckdb
import pytest
import websockets
from fastapi.testclient import TestClient

from smartbrain_3000 import db, devices, routing_key, webrtc_signaling
from smartbrain_3000.secrets import SecretStore, gen_master_key

_SIGNALING_DIR = pathlib.Path(__file__).resolve().parents[2] / "signaling"
if _SIGNALING_DIR.is_dir():
    sys.path.insert(0, str(_SIGNALING_DIR))
# These 9 tests exercise the production WebRTC broker. In the app-only container `signaling/`
# isn't mounted, so they skip — which silently hid them from "1 skipped". A release-gate run
# sets SMARTBRAIN_REQUIRE_SIGNALING_TESTS=1 (with server.py on PYTHONPATH) so a missing broker
# FAILS LOUDLY instead of skipping. See app/tests/README for how to run them.
if os.environ.get("SMARTBRAIN_REQUIRE_SIGNALING_TESTS") and importlib.util.find_spec("server") is None:
    raise RuntimeError(
        "SMARTBRAIN_REQUIRE_SIGNALING_TESTS is set but the signaling broker (server.py) is not "
        "importable — put signaling/ on PYTHONPATH so these tests actually run."
    )
broker_mod = pytest.importorskip("server", reason="signaling/ not mounted")


def _keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return Ed25519PrivateKey.generate()


_KEY = _keypair()  # the "real" Desktop's routing key for the tests below


def _pub_b64(key) -> str:
    return base64.b64encode(key.public_key().public_bytes_raw()).decode("ascii")


async def _register(ws, desktop_id: str, token: str = "secret", key=None, pubkey: str | None = None) -> dict:
    """Desktop hello + proof-of-possession; returns the broker's final reply.

    ``key=None`` uses the shared test key; ``pubkey`` overrides the advertised key so a
    test can present a key it cannot sign for.
    """
    key = key or _KEY
    hello = {"role": "desktop", "desktop_id": desktop_id, "token": token,
             "pubkey": _pub_b64(key) if pubkey is None else pubkey}
    await ws.send(json.dumps(hello))
    msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
    if msg.get("type") != "challenge":
        return msg
    nonce = base64.b64decode(msg["nonce"])
    sig = key.sign(b"sb-register-v1" + nonce + desktop_id.encode())
    await ws.send(json.dumps({"type": "prove", "sig": base64.b64encode(sig).decode("ascii")}))
    return json.loads(await asyncio.wait_for(ws.recv(), 5))


def _conn_with_routing_key():
    """An in-memory DB carrying a routing key, as record_boot leaves it."""
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
    db.meta_set(conn, "desktop_routing_key", routing_key.generate_private_key_b64())
    return conn


async def _serve_broker(token: str = "secret", **kwargs):
    """Start the broker on an ephemeral loopback port; return (server, ws_url, broker)."""
    broker = broker_mod.Broker(token, **kwargs)
    server = await websockets.serve(broker.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"ws://127.0.0.1:{port}", broker


def test_broker_relays_and_authorizes() -> None:
    async def run() -> None:
        server, url, _ = await _serve_broker("secret")
        try:
            async with websockets.connect(url) as bad:  # wrong token -> rejected
                assert (await _register(bad, "d1", token="nope"))["type"] == "error"

            async with websockets.connect(url) as desk:
                assert (await _register(desk, "d1", token="secret"))["type"] == "registered"
                async with websockets.connect(url) as phone:
                    await phone.send(json.dumps({"role": "phone", "desktop_id": "d1"}))
                    await phone.send(json.dumps({"type": "offer", "sdp": "SDP-OFFER"}))
                    relayed = json.loads(await asyncio.wait_for(desk.recv(), 5))
                    assert relayed["type"] == "offer" and relayed["sdp"] == "SDP-OFFER"
                    assert relayed["from"].startswith("phone:")
                    await desk.send(json.dumps({"type": "answer", "to": relayed["from"], "sdp": "SDP-ANSWER"}))
                    ans = json.loads(await asyncio.wait_for(phone.recv(), 5))
                    assert ans["type"] == "answer" and ans["sdp"] == "SDP-ANSWER"
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_broker_reports_desktop_offline() -> None:
    async def run() -> None:
        server, url, _ = await _serve_broker("secret")
        try:
            async with websockets.connect(url) as phone:
                await phone.send(json.dumps({"role": "phone", "desktop_id": "absent"}))
                await phone.send(json.dumps({"type": "offer", "sdp": "x"}))
                msg = json.loads(await asyncio.wait_for(phone.recv(), 5))
                assert msg["type"] == "error" and "offline" in msg["detail"]
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_broker_rejects_when_token_unset() -> None:
    # Fail-closed: with no configured token, no desktop can register (no open broker).
    async def run() -> None:
        broker = broker_mod.Broker("")
        server = await websockets.serve(broker.handle, "127.0.0.1", 0)
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        try:
            async with websockets.connect(url) as desk:
                assert (await _register(desk, "d1", token="anything"))["type"] == "error"
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


@pytest.fixture()
def app_client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as client:
        yield client


def test_full_loop_phone_to_app(app_client: TestClient) -> None:
    """broker + run_signaling + aiortc phone + auth-on-channel -> GET /api/health."""
    from aiortc import RTCPeerConnection, RTCSessionDescription

    store = SecretStore(duckdb.connect(":memory:"), gen_master_key())
    dev = devices.create_device(store, "phone")

    async def run() -> dict:
        server, url, _ = await _serve_broker("secret")
        stop = asyncio.Event()
        desk = asyncio.create_task(webrtc_signaling.run_signaling(
            signaling_url=url, desktop_id="d1", token="secret",
            get_store=lambda: store, stop=stop, http_client=app_client,  # inject the app
            conn=_conn_with_routing_key(),
        ))
        phone = RTCPeerConnection()
        channel = phone.createDataChannel("sb-api")
        loop = asyncio.get_event_loop()
        authed, reply = loop.create_future(), loop.create_future()

        @channel.on("open")
        def _open() -> None:
            channel.send(json.dumps({"type": "auth", "device_id": dev["device_id"], "credential": dev["credential"]}))

        @channel.on("message")
        def _msg(data) -> None:
            m = json.loads(data)
            if m.get("type") == "auth_ok" and not authed.done():
                authed.set_result(True)
                channel.send(json.dumps({"id": "1", "method": "GET", "path": "/api/health", "headers": {}, "body_b64": ""}))
            elif "status" in m and not reply.done():
                reply.set_result(m)

        try:
            async with websockets.connect(url) as pws:
                await pws.send(json.dumps({"role": "phone", "desktop_id": "d1"}))
                await phone.setLocalDescription(await phone.createOffer())
                await pws.send(json.dumps({"type": "offer", "sdp": phone.localDescription.sdp}))
                ans = json.loads(await asyncio.wait_for(pws.recv(), 20))
                assert ans["type"] == "answer"
                await phone.setRemoteDescription(RTCSessionDescription(sdp=ans["sdp"], type="answer"))
                await asyncio.wait_for(authed, 20)
                return await asyncio.wait_for(reply, 20)
        finally:
            stop.set()
            desk.cancel()
            await phone.close()
            server.close()
            await server.wait_closed()

    resp = asyncio.run(run())
    assert resp["status"] == 200
    import base64
    assert b'"status":"ok"' in base64.b64decode(resp["body_b64"])


async def _open_phone(url: str, desktop_id: str):
    """Connect a phone, send its hello, and wait until the broker has admitted it.

    The broker emits no message on phone admit-success, so we send a dummy offer with
    no desktop registered: the resulting ``desktop offline`` reply both confirms the
    admit AND proves the phone is in ``_phone_loop`` (per-desktop count incremented).
    """
    ws = await websockets.connect(url)
    await ws.send(json.dumps({"role": "phone", "desktop_id": desktop_id}))
    await ws.send(json.dumps({"type": "offer", "sdp": "probe"}))
    msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
    assert msg["type"] == "error" and "offline" in msg["detail"], "expected offline ack"
    return ws


async def _is_busy_reject(ws, detail: str) -> bool:
    """True iff the broker's first message is an error with the expected detail."""
    msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
    return msg.get("type") == "error" and msg.get("detail") == detail


def test_per_desktop_phone_cap_rejects_overflow() -> None:
    async def run() -> None:
        server, url, _ = await _serve_broker(
            "secret", max_phones_per_desktop=2, max_phones=64, rate_limit=999,
        )
        try:
            held = [await _open_phone(url, "d1") for _ in range(2)]
            try:
                async with websockets.connect(url) as extra:
                    await extra.send(json.dumps({"role": "phone", "desktop_id": "d1"}))
                    assert await _is_busy_reject(extra, "busy"), "third phone must be rejected"
                # A different desktop_id is still admitted (per-desktop cap is per-id, not global).
                other = await _open_phone(url, "d2")
                await other.close()
            finally:
                for ws in held:
                    await ws.close()
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_global_phone_cap_rejects_overflow() -> None:
    async def run() -> None:
        server, url, _ = await _serve_broker(
            "secret", max_phones=2, max_phones_per_desktop=64, rate_limit=999,
        )
        try:
            # Spread across desktop_ids so per-desktop cap can't be what rejects.
            a = await _open_phone(url, "d1")
            b = await _open_phone(url, "d2")
            try:
                async with websockets.connect(url) as extra:
                    await extra.send(json.dumps({"role": "phone", "desktop_id": "d3"}))
                    assert await _is_busy_reject(extra, "busy"), "third phone must be rejected"
            finally:
                await a.close()
                await b.close()
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_phone_rate_limit_rejects_burst() -> None:
    async def run() -> None:
        # Concurrent cap high so rate-limit fires first; window large so timestamps don't age out.
        server, url, broker = await _serve_broker(
            "secret", max_phones=64, max_phones_per_desktop=64,
            rate_limit=3, rate_window_secs=60.0,
        )
        try:
            # Connect-and-close 3 phones — concurrent count returns to 0 each time, but the
            # rate-limit bucket accumulates timestamps that don't age out within the window.
            # _open_phone waits for an "offline" ack so each admit is guaranteed processed.
            for _ in range(3):
                ws = await _open_phone(url, "d1")
                await ws.close()
            # Wait (bounded) for each disconnect's finally to release the per-desktop slot.
            for _ in range(50):
                if broker._phones_per_desktop.get("d1", 0) == 0:
                    break
                await asyncio.sleep(0.02)
            async with websockets.connect(url) as ws:
                await ws.send(json.dumps({"role": "phone", "desktop_id": "d1"}))
                assert await _is_busy_reject(ws, "rate_limited"), "4th rapid connect must rate-limit"
            # A different desktop_id has its own bucket and is unaffected (gets the offline ack).
            other = await _open_phone(url, "d2")
            await other.close()
            assert broker._phones_per_desktop.get("d1", 0) == 0, "concurrent count must drop"
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_phone_disconnect_releases_per_desktop_slot() -> None:
    async def run() -> None:
        server, url, broker = await _serve_broker(
            "secret", max_phones=64, max_phones_per_desktop=2, rate_limit=999,
        )
        try:
            held = [await _open_phone(url, "d1") for _ in range(2)]
            # Third is rejected while both slots are held.
            async with websockets.connect(url) as extra:
                await extra.send(json.dumps({"role": "phone", "desktop_id": "d1"}))
                assert await _is_busy_reject(extra, "busy"), "slots full -> busy"
            # Free one slot; the broker must observe the disconnect before retrying.
            await held[0].close()
            for _ in range(50):  # bounded wait for the server-side finally to run
                if broker._phones_per_desktop.get("d1", 0) <= 1:
                    break
                await asyncio.sleep(0.02)
            assert broker._phones_per_desktop.get("d1", 0) <= 1, "slot must release on disconnect"
            # A fresh connection now succeeds (no error frame on admit).
            async with websockets.connect(url) as fresh:
                await fresh.send(json.dumps({"role": "phone", "desktop_id": "d1"}))
                await fresh.send(json.dumps({"type": "offer", "sdp": "x"}))
                msg = json.loads(await asyncio.wait_for(fresh.recv(), 5))
                # Desktop is offline (we never registered one), so we expect "desktop offline",
                # NOT "busy"/"rate_limited" — proving admit succeeded.
                assert msg["type"] == "error" and "offline" in msg["detail"]
            await held[1].close()
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_broker_caps_read_from_env(monkeypatch) -> None:
    monkeypatch.setenv("SIGNALING_MAX_PHONES", "7")
    monkeypatch.setenv("SIGNALING_MAX_PHONES_PER_DESKTOP", "3")
    monkeypatch.setenv("SIGNALING_PHONE_RATE_LIMIT", "5")
    monkeypatch.setenv("SIGNALING_PHONE_RATE_WINDOW_SECS", "11.5")
    broker = broker_mod._broker_from_env("secret", [])
    assert broker._max_phones == 7
    assert broker._max_phones_per_desktop == 3
    assert broker._rate_limit == 5
    assert broker._rate_window == 11.5


# --- ephemeral TURN + open (tokenless) mode -------------------------------------------------

def _verify_ephemeral(ice: list, secret: str, urls: list) -> None:
    """A pushed ICE server must carry the exact urls + a coturn use-auth-secret credential."""
    import base64
    import hashlib
    import hmac

    assert ice and ice[0]["urls"] == urls, "ephemeral ICE must echo the node TURN urls"
    user, cred = ice[0]["username"], ice[0]["credential"]
    expected = base64.b64encode(hmac.new(secret.encode(), user.encode(), hashlib.sha1).digest()).decode()
    assert cred == expected, "credential must be base64(HMAC-SHA1(secret, username))"
    assert user.split(":")[0].isdigit(), "username must start with a unix expiry"


def test_mint_turn_credentials_matches_coturn_scheme() -> None:
    import base64
    import hashlib
    import hmac

    user, cred = broker_mod.mint_turn_credentials("s3cr3t", ttl=3600, name="sb")
    assert user.endswith(":sb") and user.split(":")[0].isdigit()
    expected = base64.b64encode(hmac.new(b"s3cr3t", user.encode(), hashlib.sha1).digest()).decode()
    assert cred == expected


def test_open_mode_admits_desktop_without_token() -> None:
    async def run() -> None:
        broker = broker_mod.Broker("", open_mode=True)
        server = await websockets.serve(broker.handle, "127.0.0.1", 0)
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        try:
            async with websockets.connect(url) as desk:
                assert (await _register(desk, "d-open", token=""))["type"] == "registered"  # NO token
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_ephemeral_ice_pushed_to_desktop_and_phone() -> None:
    secret, urls = "turnsecret", ["turn:rtc.example:3478", "turn:rtc.example:3478?transport=tcp"]

    async def run() -> None:
        broker = broker_mod.Broker("", open_mode=True, turn_urls=urls, turn_secret=secret)
        server = await websockets.serve(broker.handle, "127.0.0.1", 0)
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        try:
            async with websockets.connect(url) as desk:
                assert (await _register(desk, "d1", token=""))["type"] == "registered"
                ice_msg = json.loads(await asyncio.wait_for(desk.recv(), 5))
                assert ice_msg["type"] == "ice"
                _verify_ephemeral(ice_msg["iceServers"], secret, urls)
                async with websockets.connect(url) as phone:
                    await phone.send(json.dumps({"role": "phone", "desktop_id": "d1"}))
                    pmsg = json.loads(await asyncio.wait_for(phone.recv(), 5))
                    assert pmsg["type"] == "ice"
                    _verify_ephemeral(pmsg["iceServers"], secret, urls)
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_static_mode_regular_phone_gets_no_ice() -> None:
    # No TURN secret + a non-pairing desktop_id -> the broker must NOT push ICE (back-compat:
    # regular phones use the static creds from their stored payload). First frame is the offline ack.
    async def run() -> None:
        broker = broker_mod.Broker("secret", pair_ice=[{"urls": ["stun:x:3478"]}])
        server = await websockets.serve(broker.handle, "127.0.0.1", 0)
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        try:
            async with websockets.connect(url) as phone:
                await phone.send(json.dumps({"role": "phone", "desktop_id": "d1"}))
                await phone.send(json.dumps({"type": "offer", "sdp": "x"}))
                msg = json.loads(await asyncio.wait_for(phone.recv(), 5))
                assert msg["type"] == "error" and "offline" in msg["detail"]
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_static_mode_paircode_phone_gets_pair_ice() -> None:
    # sbpair-* rooms still get the static pair ICE in non-ephemeral mode (unchanged behavior).
    async def run() -> None:
        broker = broker_mod.Broker("secret", pair_ice=[{"urls": ["stun:x:3478"]}])
        server = await websockets.serve(broker.handle, "127.0.0.1", 0)
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        try:
            async with websockets.connect(url) as phone:
                await phone.send(json.dumps({"role": "phone", "desktop_id": "sbpair-abc"}))
                msg = json.loads(await asyncio.wait_for(phone.recv(), 5))
                assert msg["type"] == "ice" and msg["iceServers"][0]["urls"] == ["stun:x:3478"]
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_open_mode_desktop_registration_rate_limited() -> None:
    async def run() -> None:
        broker = broker_mod.Broker("", open_mode=True, reg_rate_limit=2, reg_rate_window_secs=60.0)
        server = await websockets.serve(broker.handle, "127.0.0.1", 0)
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        try:
            for i in range(2):  # two registrations allowed (connect-and-close frees the slot)
                async with websockets.connect(url) as d:
                    assert (await _register(d, f"d{i}", token=""))["type"] == "registered"
            async with websockets.connect(url) as d:  # third within the window -> rate-limited
                m = await _register(d, "d3", token="")
                assert m["type"] == "error" and m["detail"] == "rate_limited"
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_duplicate_desktop_registration_refused() -> None:
    """A live desktop_id cannot be taken over by a second registration.

    The id is a routing key, not a secret — it rides the pairing QR and every phone
    hello — so in open mode anyone who learns one could otherwise displace the real
    Desktop and receive that user's phone offers.
    """
    async def run() -> None:
        server, url, broker = await _serve_broker("", open_mode=True)
        try:
            async with websockets.connect(url) as first:
                assert (await _register(first, "d1", token=""))["type"] == "registered"

                async with websockets.connect(url) as impostor:
                    m = await _register(impostor, "d1", token="")
                    assert m["type"] == "error" and m["detail"] == "already registered"

                # The incumbent still owns the slot and still receives offers.
                assert broker._desktops.get("d1") is not None
                async with websockets.connect(url) as phone:
                    await phone.send(json.dumps({"role": "phone", "desktop_id": "d1"}))
                    await phone.send(json.dumps({"type": "offer", "sdp": "SDP-OFFER"}))
                    relayed = json.loads(await asyncio.wait_for(first.recv(), 5))
                    assert relayed["type"] == "offer" and relayed["sdp"] == "SDP-OFFER"
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_desktop_id_reusable_after_the_holder_disconnects() -> None:
    """Refusing a duplicate must not lock the id out after a genuine drop."""
    async def run() -> None:
        server, url, _ = await _serve_broker("", open_mode=True)
        try:
            async with websockets.connect(url) as first:
                assert (await _register(first, "d1", token=""))["type"] == "registered"
            for _ in range(50):  # let the server observe the close
                await asyncio.sleep(0.01)
                async with websockets.connect(url) as again:
                    m = await _register(again, "d1", token="")
                    if m["type"] == "registered":
                        return
            raise AssertionError("desktop_id never became reusable after disconnect")
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_connection_without_a_hello_is_dropped() -> None:
    """Every admission cap runs after the hello, so silence must not hold a socket."""
    async def run() -> None:
        broker = broker_mod.Broker("", open_mode=True)
        server = await websockets.serve(broker.handle, "127.0.0.1", 0)
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        original = broker_mod._HELLO_TIMEOUT_SECS
        broker_mod._HELLO_TIMEOUT_SECS = 0.05  # keep the test fast
        try:
            async with websockets.connect(url) as silent:
                with pytest.raises(Exception):  # closed by the broker, never admitted
                    await asyncio.wait_for(silent.recv(), 5)
            assert broker._desktops == {} and broker._phones == {}
        finally:
            broker_mod._HELLO_TIMEOUT_SECS = original
            server.close()
            await server.wait_closed()

    asyncio.run(run())


# --- desktop proof-of-possession (G1/G2) --------------------------------------------------

def _open_broker(**kwargs):
    """Sync helper for the async tests below: (broker, server, url) on loopback."""
    broker = broker_mod.Broker("", open_mode=True, **kwargs)
    return broker


async def _serve(broker):
    server = await websockets.serve(broker.handle, "127.0.0.1", 0)
    return server, f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"


def test_unsigned_desktop_hello_is_unauthorized() -> None:
    """No pubkey and legacy off: refused even in open mode, even for a never-seen id."""
    async def run() -> None:
        server, url = await _serve(_open_broker())
        try:
            async with websockets.connect(url) as d:
                await d.send(json.dumps({"role": "desktop", "desktop_id": "d1"}))
                m = json.loads(await asyncio.wait_for(d.recv(), 5))
                assert m == {"type": "error", "detail": "unauthorized"}
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_bad_signature_is_unauthorized_and_does_not_bind() -> None:
    async def run() -> None:
        broker = _open_broker()
        server, url = await _serve(broker)
        try:
            async with websockets.connect(url) as d:
                # Advertise the real key but sign with a stranger's: the proof fails.
                m = await _register(d, "d1", token="", key=_keypair(), pubkey=_pub_b64(_KEY))
                assert m == {"type": "error", "detail": "unauthorized"}
            assert broker._bindings.get("d1") is None, "a failed proof must not bind"
            async with websockets.connect(url) as d:  # the real key still binds afterwards
                assert (await _register(d, "d1", token=""))["type"] == "registered"
            assert broker._bindings.get("d1") == _pub_b64(_KEY)
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_bound_desktop_id_refuses_a_different_key() -> None:
    """TOFU: after d1 is bound to the real key, a stranger's key (even signing correctly
    for itself) is refused — and the real key still registers after the stranger tried."""
    async def run() -> None:
        server, url = await _serve(_open_broker())
        try:
            async with websockets.connect(url) as d:
                assert (await _register(d, "d1", token=""))["type"] == "registered"
            async with websockets.connect(url) as impostor:
                m = await _register(impostor, "d1", token="", key=_keypair())
                assert m == {"type": "error", "detail": "unauthorized"}
            async with websockets.connect(url) as d:
                assert (await _register(d, "d1", token=""))["type"] == "registered"
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_token_mode_checks_token_before_proof() -> None:
    """Wrong token -> unauthorized WITHOUT a challenge (no work spent on strangers)."""
    async def run() -> None:
        server, url, _ = await _serve_broker("secret")
        try:
            async with websockets.connect(url) as d:
                await d.send(json.dumps({"role": "desktop", "desktop_id": "d1", "token": "nope",
                                         "pubkey": _pub_b64(_KEY)}))
                m = json.loads(await asyncio.wait_for(d.recv(), 5))
                assert m == {"type": "error", "detail": "unauthorized"}
            async with websockets.connect(url) as d:  # right token still needs the proof
                m = await _register(d, "d1", token="secret", key=_keypair(), pubkey=_pub_b64(_KEY))
                assert m == {"type": "error", "detail": "unauthorized"}
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_legacy_unsigned_hello_only_for_unbound_ids() -> None:
    async def run() -> None:
        server, url = await _serve(_open_broker(allow_legacy=True))
        try:
            async with websockets.connect(url) as legacy:  # never-bound id: admitted
                await legacy.send(json.dumps({"role": "desktop", "desktop_id": "old"}))
                assert json.loads(await asyncio.wait_for(legacy.recv(), 5))["type"] == "registered"
            async with websockets.connect(url) as d:
                assert (await _register(d, "d1", token=""))["type"] == "registered"
            async with websockets.connect(url) as legacy:  # bound id: an unsigned hello cannot take it
                await legacy.send(json.dumps({"role": "desktop", "desktop_id": "d1"}))
                m = json.loads(await asyncio.wait_for(legacy.recv(), 5))
                assert m == {"type": "error", "detail": "unauthorized"}
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_bindings_persist_across_restart(tmp_path) -> None:
    state = str(tmp_path / "bindings.json")

    async def run() -> None:
        server, url = await _serve(_open_broker(state_file=state))
        try:
            async with websockets.connect(url) as d:
                assert (await _register(d, "d1", token=""))["type"] == "registered"
        finally:
            server.close()
            await server.wait_closed()
        assert json.loads(pathlib.Path(state).read_text())["d1"]["pubkey"] == _pub_b64(_KEY)
        # A fresh broker (restart) loads the binding: the stranger is still refused.
        server, url = await _serve(_open_broker(state_file=state))
        try:
            async with websockets.connect(url) as impostor:
                m = await _register(impostor, "d1", token="", key=_keypair())
                assert m == {"type": "error", "detail": "unauthorized"}
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


_DAY = 86400.0


def test_bindings_full_map_refuses_rather_than_evicting_live_ids(tmp_path) -> None:
    """A flood of new ids cannot push a victim's fresh binding out (fail closed)."""
    b = broker_mod._Bindings(str(tmp_path / "b.json"), max_entries=2, ttl_secs=30 * _DAY)
    now = 1_800_000_000.0
    assert b.bind("victim", "kv", now=now) and b.bind("x1", "k1", now=now)
    for i in range(50):  # the flood
        assert b.bind(f"flood{i}", "kf", now=now + i) is False
    assert b.get("victim") == "kv" and b.get("x1") == "k1" and len(b) == 2
    # Persisted with last_seen; a reload keeps both.
    again = broker_mod._Bindings(str(tmp_path / "b.json"), max_entries=2, ttl_secs=30 * _DAY)
    assert again.get("victim") == "kv" and again._map["victim"]["seen"] == now


def test_bindings_stale_id_is_reclaimable_but_touched_id_is_not(tmp_path) -> None:
    b = broker_mod._Bindings(str(tmp_path / "b.json"), max_entries=2, ttl_secs=30 * _DAY)
    t0 = 1_800_000_000.0
    assert b.bind("old", "ko", now=t0) and b.bind("live", "kl", now=t0)
    b.touch("live", now=t0 + 31 * _DAY)  # the live Desktop registered again recently
    assert b.bind("new", "kn", now=t0 + 31 * _DAY)  # old (31 days unseen) reclaimed
    assert b.get("old") is None and b.get("live") == "kl" and b.get("new") == "kn"
    # An unreadable file starts empty rather than refusing every Desktop.
    (tmp_path / "bad.json").write_text("{not json")
    assert len(broker_mod._Bindings(str(tmp_path / "bad.json"), max_entries=2, ttl_secs=1.0)) == 0


def test_broker_refuses_new_desktop_when_binding_map_is_full_of_live_ids() -> None:
    async def run() -> None:
        broker = _open_broker(max_desktops=2)
        server, url = await _serve(broker)
        try:
            for did in ("d1", "d2"):
                async with websockets.connect(url) as d:
                    assert (await _register(d, did, token="", key=_keypair()))["type"] == "registered"
            async with websockets.connect(url) as d:  # third id: proven, but no slot to bind
                m = await _register(d, "d3", token="", key=_keypair())
                assert m == {"type": "error", "detail": "busy"}
            assert broker._bindings.get("d1") and broker._bindings.get("d2") and not broker._bindings.get("d3")
            async with websockets.connect(url) as d:  # a bound id still registers (touch, not bind)
                m = await _register(d, "d1", token="", key=_keypair())
                assert m["detail"] == "unauthorized"  # wrong key for d1
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_ip_hello_map_sweep_bounds_the_map(monkeypatch) -> None:
    monkeypatch.setattr(broker_mod, "_RATE_MAP_MAX_KEYS", 2)
    b = _open_broker(ip_hello_limit=5, ip_hello_window_secs=60.0)
    for ip in ("a", "b", "c", "d", "e"):
        assert b._admit_ip_hello(ip)
    assert len(b._ip_hellos) <= 2  # live buckets past the cap: oldest dropped until it fits
    b._ip_hellos = {"x": [0.0], "y": [0.0], "z": [0.0]}  # all expired
    assert b._admit_ip_hello("w")
    assert set(b._ip_hellos) == {"w"}


def test_pairing_rooms_are_signed_but_never_bound() -> None:
    """A re-used code must not hit 'unauthorized' because a previous session bound the room."""
    async def run() -> None:
        broker = _open_broker()
        server, url = await _serve(broker)
        try:
            async with websockets.connect(url) as d:
                assert (await _register(d, "sbpair-abc", token="", key=_keypair()))["type"] == "registered"
            async with websockets.connect(url) as d:  # different Desktop, same code-room
                assert (await _register(d, "sbpair-abc", token="", key=_keypair()))["type"] == "registered"
            assert broker._bindings.get("sbpair-abc") is None
            async with websockets.connect(url) as d:  # but an unsigned pairing hello is refused
                await d.send(json.dumps({"role": "desktop", "desktop_id": "sbpair-abc"}))
                assert json.loads(await asyncio.wait_for(d.recv(), 5))["detail"] == "unauthorized"
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_register_desktop_helper_completes_challenge() -> None:
    """The Desktop-side helper (used by run_signaling AND the pairing host) signs with the
    stored routing key and binds; a second conn (other key) is then refused."""
    async def run() -> None:
        broker = _open_broker()
        server, url = await _serve(broker)
        conn = _conn_with_routing_key()
        try:
            async with websockets.connect(url) as ws:
                await webrtc_signaling.register_desktop(ws, desktop_id="d1", token="", conn=conn)
            assert broker._bindings.get("d1") == routing_key.public_key_b64(conn)
            async with websockets.connect(url) as ws:
                with pytest.raises(RuntimeError, match="unauthorized"):
                    await webrtc_signaling.register_desktop(
                        ws, desktop_id="d1", token="", conn=_conn_with_routing_key())
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


# --- per-client limits (G3/G4/G5) ---------------------------------------------------------

def test_per_ip_concurrent_connection_cap() -> None:
    async def run() -> None:
        server, url = await _serve(_open_broker(max_conns_per_ip=2))
        try:
            held = [await _open_phone(url, f"d{i}") for i in range(2)]
            try:
                async with websockets.connect(url) as extra:  # refused BEFORE any hello
                    assert await _is_busy_reject(extra, "busy")
            finally:
                for ws in held:
                    await ws.close()
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_per_ip_hello_rate_limit() -> None:
    async def run() -> None:
        server, url = await _serve(_open_broker(ip_hello_limit=2, ip_hello_window_secs=60.0))
        try:
            for i in range(2):
                ws = await _open_phone(url, f"d{i}")
                await ws.close()
            async with websockets.connect(url) as third:
                await third.send(json.dumps({"role": "phone", "desktop_id": "d9"}))
                assert await _is_busy_reject(third, "rate_limited")
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


class _FakeWs:
    def __init__(self, peer: str, forwarded: str = "") -> None:
        self.remote_address = (peer, 1234)
        self.request = type("R", (), {"headers": {"X-Forwarded-For": forwarded} if forwarded else {}})()


def test_client_ip_honours_forwarded_header_only_from_trusted_proxy() -> None:
    trusted = broker_mod._parse_networks("172.16.0.0/12,127.0.0.1/32")
    assert broker_mod._client_ip(_FakeWs("172.18.0.2", "203.0.113.9, 172.18.0.2"), trusted) == "203.0.113.9"
    # Rightmost UNTRUSTED hop wins: a client-supplied leftmost entry is ignored.
    assert broker_mod._client_ip(_FakeWs("172.18.0.2", "1.2.3.4, 203.0.113.9, 172.18.0.3"), trusted) == "203.0.113.9"
    # Every hop trusted (or header empty): fall back to the socket peer.
    assert broker_mod._client_ip(_FakeWs("172.18.0.2", "172.18.0.3"), trusted) == "172.18.0.2"
    # A direct client forging the header is keyed on its real peer address.
    assert broker_mod._client_ip(_FakeWs("198.51.100.4", "203.0.113.9"), trusted) == "198.51.100.4"
    assert broker_mod._client_ip(_FakeWs("172.18.0.2"), trusted) == "172.18.0.2"


def test_phone_offer_rate_limit_closes_socket() -> None:
    async def run() -> None:
        server, url = await _serve(_open_broker(offer_limit=2, offer_window_secs=60.0))
        try:
            async with websockets.connect(url) as phone:
                await phone.send(json.dumps({"role": "phone", "desktop_id": "d1"}))
                for _ in range(2):
                    await phone.send(json.dumps({"type": "offer", "sdp": "x"}))
                    assert "offline" in json.loads(await asyncio.wait_for(phone.recv(), 5))["detail"]
                await phone.send(json.dumps({"type": "offer", "sdp": "x"}))
                assert await _is_busy_reject(phone, "rate_limited")
                with pytest.raises(Exception):  # the broker closes the socket after the refusal
                    await asyncio.wait_for(phone.recv(), 5)
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_desktop_answer_rate_limit_drops_excess() -> None:
    async def run() -> None:
        broker = _open_broker(answer_limit=1, answer_window_secs=60.0)
        server, url = await _serve(broker)
        try:
            async with websockets.connect(url) as desk, websockets.connect(url) as phone:
                assert (await _register(desk, "d1", token=""))["type"] == "registered"
                await phone.send(json.dumps({"role": "phone", "desktop_id": "d1"}))
                await phone.send(json.dumps({"type": "offer", "sdp": "x"}))
                pid = json.loads(await asyncio.wait_for(desk.recv(), 5))["from"]
                await desk.send(json.dumps({"type": "answer", "to": pid, "sdp": "one"}))
                await desk.send(json.dumps({"type": "answer", "to": pid, "sdp": "two"}))
                assert json.loads(await asyncio.wait_for(phone.recv(), 5))["sdp"] == "one"
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(phone.recv(), 0.3)
                assert broker._dropped_answers == 1
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_cross_room_answer_is_dropped() -> None:
    """A desktop can only answer phones that dialled ITS id, never another desktop's phone."""
    async def run() -> None:
        broker = _open_broker()
        server, url = await _serve(broker)
        try:
            async with websockets.connect(url) as d1, websockets.connect(url) as d2, \
                    websockets.connect(url) as phone:
                assert (await _register(d1, "d1", token=""))["type"] == "registered"
                assert (await _register(d2, "d2", token="", key=_keypair()))["type"] == "registered"
                await phone.send(json.dumps({"role": "phone", "desktop_id": "d1"}))
                await phone.send(json.dumps({"type": "offer", "sdp": "x"}))
                pid = json.loads(await asyncio.wait_for(d1.recv(), 5))["from"]
                await d2.send(json.dumps({"type": "answer", "to": pid, "sdp": "EVIL"}))
                await d1.send(json.dumps({"type": "answer", "to": pid, "sdp": "GOOD"}))
                assert json.loads(await asyncio.wait_for(phone.recv(), 5))["sdp"] == "GOOD"
                assert broker._dropped_answers == 1
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_turn_credentials_named_per_client() -> None:
    secret, urls = "s", ["turn:n:3478"]

    async def run() -> None:
        server, url = await _serve(_open_broker(turn_urls=urls, turn_secret=secret, turn_ttl=3600))
        try:
            async with websockets.connect(url) as desk, websockets.connect(url) as phone:
                assert (await _register(desk, "d1", token=""))["type"] == "registered"
                dname = json.loads(await asyncio.wait_for(desk.recv(), 5))["iceServers"][0]["username"]
                await phone.send(json.dumps({"role": "phone", "desktop_id": "d1"}))
                pname = json.loads(await asyncio.wait_for(phone.recv(), 5))["iceServers"][0]["username"]
                assert dname.split(":", 1)[1] != pname.split(":", 1)[1] != "sb"
                assert "d1" not in dname  # the routing id must not appear in coturn logs
                assert int(dname.split(":")[0]) <= int(__import__("time").time()) + 3600
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_new_limits_read_from_env(monkeypatch) -> None:
    monkeypatch.setenv("SIGNALING_MAX_CONNS_PER_IP", "3")
    monkeypatch.setenv("SIGNALING_IP_HELLO_LIMIT", "4")
    monkeypatch.setenv("SIGNALING_IP_HELLO_WINDOW_SECS", "5.5")
    monkeypatch.setenv("SIGNALING_OFFER_LIMIT", "6")
    monkeypatch.setenv("SIGNALING_OFFER_WINDOW_SECS", "7.5")
    monkeypatch.setenv("SIGNALING_ANSWER_LIMIT", "8")
    monkeypatch.setenv("SIGNALING_ANSWER_WINDOW_SECS", "9.5")
    monkeypatch.setenv("SIGNALING_TRUSTED_PROXIES", "10.0.0.0/8")
    monkeypatch.setenv("SIGNALING_ALLOW_LEGACY", "1")
    monkeypatch.setenv("SIGNALING_TURN_TTL", "3600")
    monkeypatch.delenv("SIGNALING_STATE_FILE", raising=False)
    b = broker_mod._broker_from_env("secret", [])
    assert (b._max_conns_per_ip, b._ip_hello_limit, b._ip_hello_window) == (3, 4, 5.5)
    assert (b._offer_limit, b._offer_window, b._answer_limit, b._answer_window) == (6, 7.5, 8, 9.5)
    assert [str(n) for n in b._trusted] == ["10.0.0.0/8"] and b._allow_legacy and b._turn_ttl == 3600
    monkeypatch.setenv("SIGNALING_BINDING_TTL_DAYS", "2")
    assert broker_mod._broker_from_env("secret", [])._bindings._ttl == 2 * 86400.0
    assert "bindings=0 in-memory" in b.status_summary() and "legacy=ALLOWED" in b.status_summary()
    assert broker_mod._DEFAULT_TURN_TTL == 3600 and broker_mod._DEFAULT_TRUSTED_PROXIES == "127.0.0.1/32,::1/128"


def test_record_boot_generates_routing_key_but_keeps_it_out_of_boot() -> None:
    conn = duckdb.connect(":memory:")
    db.run_migrations(conn)
    boot = db.record_boot(conn)
    assert "desktop_routing_key" not in boot  # /api/status echoes boot; the key must not leak
    first = db.meta_get(conn, "desktop_routing_key")
    assert first and len(base64.b64decode(first)) == 32
    db.record_boot(conn)
    assert db.meta_get(conn, "desktop_routing_key") == first, "generated once, stable across boots"
    sig = base64.b64decode(routing_key.sign(conn, b"m"))
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    Ed25519PublicKey.from_public_bytes(base64.b64decode(routing_key.public_key_b64(conn))).verify(sig, b"m")
