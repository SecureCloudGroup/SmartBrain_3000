"""Docker healthcheck for the broker — an APP-LEVEL probe, deliberately not a TCP one.

The failure this exists for (observed live, 2026-08-22): after 7 weeks of uptime the
websocket library kept ACCEPTING connections while the handler layer was starved —
every client timed out, yet any TCP or handshake-level check would have called the
container healthy for as long as the wedge lasted. Health is therefore defined as the
full application loop answering: connect, send a hello that is invalid on purpose
(no desktop_id — refused BEFORE any admission cap or rate limit is touched), and
require the handler's error reply within the deadline. Exit 0 healthy, 1 not.
"""
import asyncio
import json
import os
import sys

import websockets

_PORT = int(os.environ.get("SIGNALING_PORT", "8089"))


async def probe() -> int:
    async with websockets.connect(
        f"ws://127.0.0.1:{_PORT}", open_timeout=5, close_timeout=2
    ) as ws:
        await ws.send(json.dumps({"type": "hello"}))  # invalid on purpose: no ids, no role
        reply = json.loads(await asyncio.wait_for(ws.recv(), 5))
        ok = isinstance(reply, dict) and bool(reply.get("type"))
        return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(probe()))
    except Exception as exc:  # any failure IS the unhealthy signal — exact cause to stderr
        print(f"unhealthy: {exc}", file=sys.stderr)
        sys.exit(1)
