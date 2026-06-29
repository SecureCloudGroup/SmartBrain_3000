"""Remote-access (WebRTC) configuration, read from the environment.

Defined in one place and used by both the signaling loop (main.py) and the pairing
payload (devices_routes.py), so the signaling URL + STUN/TURN settings never drift.
All values come from the WebRTC compose overlay; empty when the feature is unconfigured.
"""

from __future__ import annotations

import os
import socket
import struct
import time


# SecureCloudGroup's hosted, content-blind signaling node — the default so a fresh install can pair
# a phone with zero configuration. Self-hosters override with SMARTBRAIN_SIGNALING_URL=wss://<node>.
# Pre-wiring the URL exposes nothing on its own: remote access still reaches the broker only once the
# user pairs a device (the opt-in), and nothing is reachable until a device credential authenticates.
_DEFAULT_SIGNALING_URL = "wss://rtc.securecloudgroup.com"


def signaling_url() -> str:
    """The wss:// signaling broker URL — defaults to the hosted node; env-overridable for self-host."""
    return os.environ.get("SMARTBRAIN_SIGNALING_URL", _DEFAULT_SIGNALING_URL)


def desktop_id(boot: dict | None = None) -> str:
    """This Desktop's broker routing id — explicit override, else the dedicated random
    ``desktop_routing_id`` (NOT the install id, which /api/status exposes), with a
    legacy fallback to install_id for an older boot dict. (Arch H6)"""
    explicit = os.environ.get("SMARTBRAIN_DESKTOP_ID")
    if explicit:
        return explicit
    b = boot or {}
    return str(b.get("desktop_routing_id") or b.get("install_id", ""))


def _ice_priority(url: str, prefer_udp: bool = False) -> int:
    """Ordering key for ICE urls. stun first; then TURN by transport preference. aiortc uses
    only the FIRST turn url, so this single ordering decides the relay transport: UDP TURN is
    faster but dies on UDP-blocked networks; TCP/TLS TURN is slower but survives them."""
    assert isinstance(url, str), "ice url must be a string"
    low = url.lower()
    if low.startswith("stun"):
        return 0
    is_tcp = low.startswith("turns") or "transport=tcp" in low
    if prefer_udp:
        return 2 if is_tcp else 1  # UDP relay first (fast) when UDP works
    return 1 if is_tcp else 2  # TCP/TLS relay first (resilient) when UDP is blocked


_UDP_PROBE_TIMEOUT = 0.8
_UDP_PROBE_TTL = 30.0  # VPN state changes on the order of minutes; don't re-probe every offer
# Brief cache so per-connection setup doesn't re-run (or block on) the probe each time.
_udp_probe: dict = {"key": "", "ts": 0.0, "ok": False}


def _first_host_port(urls: list[str]) -> tuple[str, int] | None:
    """(host, port) of the first STUN/TURN url, used for the UDP reachability probe."""
    assert isinstance(urls, list), "urls must be a list"
    for url in urls:
        parts = url.split("?", 1)[0].split(":")  # scheme:host:port[?query]
        if len(parts) >= 3 and parts[2].isdigit():
            return parts[1], int(parts[2])
    return None


def _udp_stun_reachable(host: str, port: int) -> bool:
    """True if a UDP STUN binding to host:port gets any reply (i.e. UDP egress works)."""
    assert host and port, "host/port required"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(_UDP_PROBE_TIMEOUT)
    try:
        sock.sendto(struct.pack(">HH", 0x0001, 0) + b"\x21\x12\xa4\x42" + os.urandom(12), (host, port))
        sock.recvfrom(256)
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _udp_egress_ok(urls: list[str]) -> bool:
    """Cached: can the Desktop reach the STUN/TURN node over UDP? Decides UDP vs TCP TURN."""
    hostport = _first_host_port(urls)
    if hostport is None:
        return False
    key = f"{hostport[0]}:{hostport[1]}"
    now = time.monotonic()
    if _udp_probe["key"] == key and now - _udp_probe["ts"] < _UDP_PROBE_TTL:
        return bool(_udp_probe["ok"])
    ok = _udp_stun_reachable(*hostport)
    _udp_probe.update(key=key, ts=now, ok=ok)
    return ok


def ice_servers_adaptive() -> list[dict]:
    """ICE servers for the Desktop's aiortc, ordered by LIVE UDP reachability: UDP TURN first
    when UDP works (fast relay), TCP/TLS TURN first when it's blocked (resilient, e.g. a VPN).
    aiortc consumes only the first TURN url, so this single choice sets the relay transport.
    Used per-connection (webrtc_signaling) so it adapts as the VPN is toggled."""
    servers = ice_servers()
    if not servers:
        return servers
    prefer_udp = _udp_egress_ok(servers[0]["urls"])
    servers[0]["urls"].sort(key=lambda u: _ice_priority(u, prefer_udp))
    assert servers[0]["urls"], "ice server must carry at least one url"
    return servers


def adapt_pushed_ice(servers: list) -> list:
    """Reorder broker-pushed ICE urls by LIVE UDP reachability so the relay works even when UDP
    egress is blocked (Docker Desktop on macOS, UDP-blocking networks): aiortc consumes only the
    FIRST turn url, so we put TCP/TLS TURN first when UDP can't reach the node, UDP TURN first when
    it can. The minted username/credential are preserved untouched."""
    assert isinstance(servers, list), "servers must be a list"
    adapted: list = []
    for s in servers:
        if not isinstance(s, dict):
            continue
        urls = [u for u in (s.get("urls") or []) if u]
        if urls:
            prefer_udp = _udp_egress_ok(urls)
            urls = sorted(urls, key=lambda u: _ice_priority(u, prefer_udp))
        adapted.append({**s, "urls": urls})
    return adapted


def ice_servers() -> list[dict]:
    """STUN/TURN ICE servers (RTCIceServer shape) from env; [] if none set.

    CRITICAL: aiortc consumes only the FIRST TURN url it encounters. So we order
    TURN-over-TCP/TLS ahead of plain UDP TURN — otherwise a Desktop whose UDP is blocked
    (e.g. by a VPN) picks the UDP relay, gets no usable relay candidate, and ICE fails. The
    sort is stable, so relative order within a tier (and the single STUN) is preserved.
    """
    urls = [u.strip() for u in os.environ.get("SMARTBRAIN_ICE_URLS", "").split(",") if u.strip()]
    assert isinstance(urls, list), "urls must be a list"
    if not urls:
        return []
    urls.sort(key=_ice_priority)
    server: dict = {"urls": urls}
    user = os.environ.get("SMARTBRAIN_TURN_USERNAME")
    cred = os.environ.get("SMARTBRAIN_TURN_CREDENTIAL")
    if user and cred:
        server["username"], server["credential"] = user, cred
    assert server["urls"], "ice server must carry at least one url"
    return [server]
