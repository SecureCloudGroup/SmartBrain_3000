"""Tests for remote_config.py — env-driven WebRTC remote-access config.

Read by both ``main._webrtc_loop`` and the pairing payload, so the signaling URL,
desktop id, and ICE servers stay consistent across the two consumers.
"""

from __future__ import annotations

from smartbrain_3000 import remote_config


# --- signaling_url --------------------------------------------------------

def test_signaling_url_defaults_to_hosted_node(monkeypatch) -> None:
    # Zero-config: a fresh install points at SecureCloudGroup's hosted broker so a phone can pair
    # with no setup. (Pre-wiring the URL exposes nothing until the user pairs a device.)
    monkeypatch.delenv("SMARTBRAIN_SIGNALING_URL", raising=False)
    url = remote_config.signaling_url()
    assert url == "wss://rtc.securecloudgroup.com"
    assert url.startswith("wss://")  # the broker is wss-only (invariant for the loop)


def test_signaling_url_reads_env(monkeypatch) -> None:
    monkeypatch.setenv("SMARTBRAIN_SIGNALING_URL", "wss://broker.example/ws")
    url = remote_config.signaling_url()
    assert url == "wss://broker.example/ws"
    assert url.startswith("wss://")  # the broker is wss-only (invariant for the loop)


# --- desktop_id (precedence: env > routing_id > install_id > "") ----------

def test_desktop_id_uses_routing_id_when_present(monkeypatch) -> None:
    monkeypatch.delenv("SMARTBRAIN_DESKTOP_ID", raising=False)
    boot = {"install_id": "inst-1", "desktop_routing_id": "rt-2"}
    assert remote_config.desktop_id(boot) == "rt-2"
    # Arch H6: routing id is NOT the install id (different identity).
    assert remote_config.desktop_id(boot) != boot["install_id"]


def test_desktop_id_legacy_boot_falls_back_to_install_id(monkeypatch) -> None:
    # An older boot dict (pre-H6) has only install_id; remote_config must still
    # produce a usable routing key rather than empty out.
    monkeypatch.delenv("SMARTBRAIN_DESKTOP_ID", raising=False)
    assert remote_config.desktop_id({"install_id": "legacy-1"}) == "legacy-1"


def test_desktop_id_env_override_wins(monkeypatch) -> None:
    monkeypatch.setenv("SMARTBRAIN_DESKTOP_ID", "operator-override")
    boot = {"install_id": "inst-1", "desktop_routing_id": "rt-2"}
    assert remote_config.desktop_id(boot) == "operator-override"
    # Override beats BOTH ids in the boot dict.
    assert remote_config.desktop_id({"install_id": "inst-1"}) == "operator-override"


def test_desktop_id_none_boot_returns_empty_when_no_env(monkeypatch) -> None:
    monkeypatch.delenv("SMARTBRAIN_DESKTOP_ID", raising=False)
    # No env, no boot, no install id -> empty string (caller decides what to do).
    assert remote_config.desktop_id(None) == ""
    assert remote_config.desktop_id({}) == ""


# --- ice_servers ----------------------------------------------------------

def test_ice_servers_empty_when_env_unset(monkeypatch) -> None:
    monkeypatch.delenv("SMARTBRAIN_ICE_URLS", raising=False)
    monkeypatch.delenv("SMARTBRAIN_TURN_USERNAME", raising=False)
    monkeypatch.delenv("SMARTBRAIN_TURN_CREDENTIAL", raising=False)
    out = remote_config.ice_servers()
    assert out == []
    assert isinstance(out, list)


def test_ice_servers_parses_comma_separated_urls(monkeypatch) -> None:
    monkeypatch.setenv("SMARTBRAIN_ICE_URLS", "stun:stun1.example:3478, stun:stun2.example:3478")
    monkeypatch.delenv("SMARTBRAIN_TURN_USERNAME", raising=False)
    monkeypatch.delenv("SMARTBRAIN_TURN_CREDENTIAL", raising=False)
    out = remote_config.ice_servers()
    assert len(out) == 1  # one RTCIceServer record carrying the urls array
    assert out[0]["urls"] == ["stun:stun1.example:3478", "stun:stun2.example:3478"]
    assert "username" not in out[0] and "credential" not in out[0]  # STUN doesn't auth


def test_ice_servers_orders_tcp_turn_before_udp(monkeypatch) -> None:
    # aiortc uses only the FIRST TURN url; TCP/TLS must come before UDP so a UDP-blocked
    # Desktop (e.g. on a VPN) still gets a usable relay candidate instead of ICE failing.
    monkeypatch.setenv(
        "SMARTBRAIN_ICE_URLS",
        "stun:n:3478, turn:n:3478, turn:n:3478?transport=tcp, turns:n:5349?transport=tcp",
    )
    monkeypatch.delenv("SMARTBRAIN_TURN_USERNAME", raising=False)
    monkeypatch.delenv("SMARTBRAIN_TURN_CREDENTIAL", raising=False)
    urls = remote_config.ice_servers()[0]["urls"]
    assert urls[0].startswith("stun")  # STUN kept first (direct P2P when UDP works)
    first_turn = next(u for u in urls if u.startswith(("turn:", "turns:")))
    assert "transport=tcp" in first_turn or first_turn.startswith("turns"), urls


def test_ice_servers_adaptive_prefers_udp_when_reachable(monkeypatch) -> None:
    # UDP works -> aiortc's single TURN pick should be UDP (faster relay than TCP).
    monkeypatch.setenv("SMARTBRAIN_ICE_URLS", "stun:n:3478, turn:n:3478?transport=tcp, turn:n:3478")
    monkeypatch.delenv("SMARTBRAIN_TURN_USERNAME", raising=False)
    monkeypatch.delenv("SMARTBRAIN_TURN_CREDENTIAL", raising=False)
    monkeypatch.setattr(remote_config, "_udp_egress_ok", lambda urls: True)
    urls = remote_config.ice_servers_adaptive()[0]["urls"]
    first_turn = next(u for u in urls if u.startswith("turn"))
    assert "transport=tcp" not in first_turn, urls  # UDP TURN chosen


def test_ice_servers_adaptive_falls_back_to_tcp_when_udp_blocked(monkeypatch) -> None:
    # UDP blocked (e.g. VPN) -> fall back to TCP/TLS TURN so the relay still works.
    monkeypatch.setenv("SMARTBRAIN_ICE_URLS", "stun:n:3478, turn:n:3478, turn:n:3478?transport=tcp")
    monkeypatch.delenv("SMARTBRAIN_TURN_USERNAME", raising=False)
    monkeypatch.delenv("SMARTBRAIN_TURN_CREDENTIAL", raising=False)
    monkeypatch.setattr(remote_config, "_udp_egress_ok", lambda urls: False)
    urls = remote_config.ice_servers_adaptive()[0]["urls"]
    first_turn = next(u for u in urls if u.startswith("turn"))
    assert "transport=tcp" in first_turn, urls  # TCP TURN chosen


def test_first_host_port_parses_stun_turn_urls() -> None:
    assert remote_config._first_host_port(["stun:example.net:3478"]) == ("example.net", 3478)
    assert remote_config._first_host_port(["turn:h:3478?transport=tcp"]) == ("h", 3478)
    assert remote_config._first_host_port(["bogus"]) is None


def test_ice_servers_includes_turn_credentials_when_set(monkeypatch) -> None:
    monkeypatch.setenv("SMARTBRAIN_ICE_URLS", "turn:turn.example:3478")
    monkeypatch.setenv("SMARTBRAIN_TURN_USERNAME", "u1")
    monkeypatch.setenv("SMARTBRAIN_TURN_CREDENTIAL", "s3cr3t")
    out = remote_config.ice_servers()
    assert len(out) == 1
    assert out[0] == {
        "urls": ["turn:turn.example:3478"],
        "username": "u1",
        "credential": "s3cr3t",
    }


def test_ice_servers_drops_blank_entries(monkeypatch) -> None:
    # Trailing comma / extra whitespace must not produce empty URL strings the
    # client would later try to dial.
    monkeypatch.setenv("SMARTBRAIN_ICE_URLS", "stun:stun.example:3478, ,  ,")
    out = remote_config.ice_servers()
    assert len(out) == 1
    assert out[0]["urls"] == ["stun:stun.example:3478"]
    assert all(u.strip() for u in out[0]["urls"])


def test_ice_servers_partial_turn_credentials_ignored(monkeypatch) -> None:
    # An admin who set username but forgot credential must NOT produce a
    # half-configured TURN entry that the browser would reject.
    monkeypatch.setenv("SMARTBRAIN_ICE_URLS", "turn:turn.example:3478")
    monkeypatch.setenv("SMARTBRAIN_TURN_USERNAME", "u1")
    monkeypatch.delenv("SMARTBRAIN_TURN_CREDENTIAL", raising=False)
    out = remote_config.ice_servers()
    assert len(out) == 1
    assert "username" not in out[0] and "credential" not in out[0]


# --- adapt_pushed_ice (broker-pushed ephemeral ICE -> UDP-resilient ordering) ----------

_PUSHED = [{
    "urls": ["stun:n:3478", "turn:n:3478", "turn:n:3478?transport=tcp"],
    "username": "1700000000:sb",
    "credential": "abc123",
}]


def test_adapt_pushed_ice_tcp_turn_first_when_udp_blocked(monkeypatch) -> None:
    # UDP egress blocked (Docker on macOS, UDP-blocking nets): aiortc uses the FIRST turn url, so
    # TCP TURN must come before UDP TURN or the Desktop never gets a usable relay candidate.
    monkeypatch.setattr(remote_config, "_udp_egress_ok", lambda urls: False)
    out = remote_config.adapt_pushed_ice(_PUSHED)
    urls = out[0]["urls"]
    assert urls[0].startswith("stun")  # STUN stays first
    first_turn = next(u for u in urls if u.startswith("turn"))
    assert "transport=tcp" in first_turn, urls
    # credentials preserved untouched
    assert out[0]["username"] == "1700000000:sb" and out[0]["credential"] == "abc123"


def test_adapt_pushed_ice_udp_turn_first_when_reachable(monkeypatch) -> None:
    monkeypatch.setattr(remote_config, "_udp_egress_ok", lambda urls: True)
    out = remote_config.adapt_pushed_ice(_PUSHED)
    first_turn = next(u for u in out[0]["urls"] if u.startswith("turn"))
    assert "transport=tcp" not in first_turn, out[0]["urls"]  # UDP relay preferred when UDP works


def test_adapt_pushed_ice_empty_is_safe() -> None:
    assert remote_config.adapt_pushed_ice([]) == []
