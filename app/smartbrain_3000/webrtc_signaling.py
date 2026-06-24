"""Desktop signaling client — Phase 3b.

Maintains a long-lived **outbound** WSS to the self-hosted signaling broker, so the
home Desktop is reachable from anywhere without any inbound port or router change.
It registers this install's ``desktop_id``, and for each phone offer relayed by the
broker it creates a peer (:func:`webrtc_peer.answer_offer`) and ships the answer
back. The broker only ever sees SDP — the device credential authenticates over the
encrypted DataChannel, never here.

Driven from ``main.py``'s lifespan and gated by ``SMARTBRAIN_WEBRTC_ENABLED`` (off
by default). ``websockets`` and ``aiortc`` are imported lazily so they are only
loaded when remote access is actually turned on.
"""

from __future__ import annotations

import asyncio
import json
import logging

from . import webrtc_bridge, webrtc_peer

log = logging.getLogger(__name__)

_RECONNECT_MAX = 30       # seconds — cap on backoff between reconnect attempts
_BACKOFF_START = 1
_BACKOFF_STABLE_SECS = 30  # only reset backoff after the link has been UP this long
_MAX_PEERS = 8            # bound concurrent device connections
_PEER_CONNECT_TIMEOUT = 30  # seconds — reap a peer that never finishes connecting
_MAX_MSG = 256 * 1024
_CONNECTED = ("connected", "completed")

# Strong refs to fire-and-forget tasks (reaper, stop watcher). CPython issue
# 91887: a task whose only reference is the loop's weakref set can be GC'd
# mid-flight. Tasks add themselves here on creation and remove themselves via
# add_done_callback on completion.
_pending_tasks: set[asyncio.Task] = set()


def _track(task: asyncio.Task) -> asyncio.Task:
    """Hold a strong ref to ``task`` until it completes (defeats GC of bg tasks)."""
    assert task is not None, "task required"
    assert isinstance(task, asyncio.Task), "asyncio.Task required"
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)
    return task


async def _reap_unconnected(pc, phone_id: str, peers: dict) -> None:
    """Close + forget a peer that hasn't connected within the timeout (frees the cap)."""
    await asyncio.sleep(_PEER_CONNECT_TIMEOUT)
    if peers.get(phone_id) is pc and pc.connectionState not in _CONNECTED:
        peers.pop(phone_id, None)
        await pc.close()


async def _on_offer(ws, msg: dict, get_store, http_client, ice_servers, peers: dict) -> None:
    """Answer one phone offer (if unlocked + under the peer cap) and return the SDP."""
    phone_id = str(msg.get("from") or "")
    store = get_store()
    if store is None:  # app is locked — nothing to reach; ignore the offer
        return
    old = peers.pop(phone_id, None)  # a re-offer replaces its own prior peer (no orphan/leak)
    if old is not None:
        await old.close()
    if len(peers) >= _MAX_PEERS:
        log.warning("webrtc: peer cap reached; dropping offer")
        return
    # ice_servers may be a callable (re-evaluated per offer to pick UDP vs TCP TURN by live
    # network state); resolve it off the event loop since it may run a short UDP probe.
    resolved_ice = (await asyncio.to_thread(ice_servers)) if callable(ice_servers) else ice_servers
    try:
        pc, answer_sdp = await webrtc_peer.answer_offer(
            str(msg.get("sdp") or ""), store=store, http_client=http_client, ice_servers=resolved_ice
        )
    except Exception as exc:  # malformed offer -> drop
        log.warning("webrtc: answer_offer failed: %s", type(exc).__name__)
        return
    peers[phone_id] = pc

    @pc.on("connectionstatechange")
    async def _on_state() -> None:
        if pc.connectionState in ("closed", "failed", "disconnected"):
            peers.pop(phone_id, None)

    # Half-open peers can't lock the cap. Strong-ref the reaper task — without it
    # CPython may GC the task before its sleep elapses (issue 91887).
    _track(asyncio.ensure_future(_reap_unconnected(pc, phone_id, peers)))
    await ws.send(json.dumps({"type": "answer", "to": phone_id, "sdp": answer_sdp}))


async def _close_on_stop(ws, stop) -> None:
    """Close the WSS as soon as ``stop`` fires, so shutdown isn't blocked on recv()."""
    await stop.wait()
    await ws.close()


async def _arm_backoff_reset(state: dict) -> None:
    """After ``_BACKOFF_STABLE_SECS`` of uptime, flag the loop to reset its backoff.

    A flaky connect->drop cycle (e.g. broker accepts, kills the link a second
    later) used to reset backoff at handshake, pinning the loop at max forever.
    We only mark the link as STABLE after it has been up long enough that the
    next drop is plausibly a fresh fault, not the same one repeating.
    """
    assert isinstance(state, dict), "state dict required"
    await asyncio.sleep(_BACKOFF_STABLE_SECS)
    state["stable"] = True


async def run_signaling(
    *, signaling_url, desktop_id, token, get_store, ice_servers=None, stop=None, http_client=None
) -> None:
    """Connect to the broker and answer phone offers until ``stop`` is set.

    Bounded reconnect with exponential backoff; a dropped connection is retried so
    the Desktop stays reachable. ``get_store`` is called per offer so a lock/unlock
    is observed without restarting the loop. ``http_client`` defaults to the app's
    loopback client; tests inject one bound to the app.
    """
    import websockets  # lazy: only when remote access is enabled

    assert signaling_url and desktop_id, "signaling url + desktop id required"
    owns_client = http_client is None
    http_client = http_client or webrtc_bridge.loopback_client()
    peers: dict = {}
    backoff = _BACKOFF_START
    try:
        while stop is None or not stop.is_set():
            stable: dict = {"stable": False}
            reset_arm: asyncio.Task | None = None
            try:
                async with websockets.connect(signaling_url, max_size=_MAX_MSG) as ws:
                    await ws.send(json.dumps({"role": "desktop", "desktop_id": desktop_id, "token": token}))
                    # Arm — not apply — a backoff reset. The reset only takes effect
                    # if the link stays UP for _BACKOFF_STABLE_SECS (B14): a
                    # connect->drop loop won't clear the backoff at handshake.
                    reset_arm = _track(asyncio.ensure_future(_arm_backoff_reset(stable)))
                    watcher = (
                        _track(asyncio.ensure_future(_close_on_stop(ws, stop))) if stop is not None else None
                    )
                    try:
                        async for raw in ws:
                            msg = json.loads(raw)
                            if msg.get("type") == "offer":
                                await _on_offer(ws, msg, get_store, http_client, ice_servers, peers)
                    finally:
                        if watcher is not None:
                            watcher.cancel()
            except Exception as exc:  # any disconnect -> bounded retry
                log.warning("webrtc: signaling link down (%s); reconnecting", type(exc).__name__)
            finally:
                if reset_arm is not None:
                    reset_arm.cancel()
            if stable["stable"]:  # link stayed UP long enough — reset backoff
                backoff = _BACKOFF_START
            if stop is not None and stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(_RECONNECT_MAX, backoff * 2)
    finally:
        for pc in list(peers.values()):
            await pc.close()
        if owns_client:  # don't close a client the caller injected (e.g. tests)
            http_client.close()
