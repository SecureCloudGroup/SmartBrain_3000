"""uvicorn entrypoint with env-driven host / port / TLS config.

Serving config lives here (not hardcoded in the Dockerfile) so TLS can be
enabled for LAN / mobile access by pointing SMARTBRAIN_TLS_CERT and
SMARTBRAIN_TLS_KEY at a certificate + key. With neither set the app serves
plain HTTP — correct for desktop, which reaches it at http://localhost (a
secure context where PWA features already work).
"""

from __future__ import annotations

import os

import uvicorn

_DEFAULT_PORT = 33000


def build_config() -> dict:
    """Build uvicorn kwargs from the environment; enable TLS iff cert + key set.

    Defaults to binding loopback; the container opts into 0.0.0.0 via
    SMARTBRAIN_HOST (compose maps it back to host loopback only).
    """
    host = os.environ.get("SMARTBRAIN_HOST", "127.0.0.1")
    port = int(os.environ.get("SMARTBRAIN_PORT", str(_DEFAULT_PORT)))
    assert 1 <= port <= 65535, "port out of range"
    cert = os.environ.get("SMARTBRAIN_TLS_CERT")
    key = os.environ.get("SMARTBRAIN_TLS_KEY")
    # Security-critical: refuse a half-configured TLS (it would silently fall
    # back to plain HTTP). A hard raise, not an assert, so `python -O` keeps it.
    if bool(cert) != bool(key):
        raise RuntimeError("TLS needs both SMARTBRAIN_TLS_CERT and SMARTBRAIN_TLS_KEY (or neither)")
    config: dict = {"host": host, "port": port}
    if cert and key:
        config["ssl_certfile"] = cert
        config["ssl_keyfile"] = key
    assert ("ssl_certfile" in config) == ("ssl_keyfile" in config), "TLS pair must be symmetric"
    return config


def main() -> None:
    """Run the app under uvicorn with the resolved serving config."""
    config = build_config()
    assert "host" in config and "port" in config, "config must set host and port"
    assert isinstance(config["port"], int), "port must be an int"
    uvicorn.run("smartbrain_3000.main:app", **config)


if __name__ == "__main__":
    main()
