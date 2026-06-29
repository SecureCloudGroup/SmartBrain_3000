"""Tests for app lifespan/startup wiring (split create_app + pooled gateway client)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import main


@pytest.fixture()
def app_client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "main.duckdb"))
    with TestClient(main.create_app()) as c:
        yield c


def test_gw_client_pool_created_and_closed_with_app(app_client: TestClient) -> None:
    # The lifespan must install a long-lived pooled httpx.Client (B22) and close it on shutdown.
    state = app_client.app.state
    assert isinstance(state.gw_client, httpx.Client), "gateway pool must be an httpx.Client"
    assert not state.gw_client.is_closed, "pool must be open while the app is running"


def test_pool_closed_after_lifespan_exits(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "lc.duckdb"))
    app = main.create_app()
    with TestClient(app):
        pool = app.state.gw_client
        assert not pool.is_closed
    assert pool.is_closed, "shutdown must close the pooled gateway client"


def test_health_and_status_still_work(app_client: TestClient) -> None:
    # Split create_app must not change observable startup behavior (B15).
    h = app_client.get("/api/health")
    assert h.status_code == 200 and h.json()["status"] == "ok"
    s = app_client.get("/api/status")
    assert s.status_code == 200 and "install_id" in s.json()
    # Arch H6: the WebRTC broker routing id must NOT leak via the status route.
    assert "desktop_routing_id" not in s.json()


def test_desktop_routing_id_is_random_and_distinct(tmp_path, monkeypatch) -> None:
    # Arch H6: routing id is its own random value, recorded in boot, != install_id.
    import duckdb
    from smartbrain_3000 import db as dbmod, remote_config

    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    boot = dbmod.record_boot(conn)
    assert boot["desktop_routing_id"] and boot["desktop_routing_id"] != boot["install_id"]
    assert remote_config.desktop_id(boot) == boot["desktop_routing_id"]
    # Legacy boot dict (no routing id) falls back to install_id.
    assert remote_config.desktop_id({"install_id": "legacy"}) == "legacy"
    # Explicit env override wins.
    monkeypatch.setenv("SMARTBRAIN_DESKTOP_ID", "override-x")
    assert remote_config.desktop_id(boot) == "override-x"


def test_drain_swallows_crashed_task(caplog) -> None:
    # B12: a RuntimeError from a startup task must not leak through finally.
    async def crashed() -> None:
        raise RuntimeError("task blew up")

    async def good() -> None:
        return None

    async def runner() -> None:
        tasks = (asyncio.create_task(crashed()), asyncio.create_task(good()))
        await asyncio.sleep(0)  # let both tasks run to completion
        await main._drain_startup_tasks(tasks)

    with caplog.at_level("WARNING"):
        asyncio.run(runner())
    assert any("task crashed" in rec.message for rec in caplog.records)


def test_drain_handles_none_tasks() -> None:
    # The drain helper must skip the None placeholder for the (optional) webrtc task.
    async def runner() -> None:
        await main._drain_startup_tasks((None, None))

    asyncio.run(runner())  # must not raise


# --- lifespan: staged-restore application + clean shutdown ---------------

def _seed_smartbrain_db(path) -> None:
    """Build a real (key_wraps-bearing) DB at ``path`` so it passes is_smartbrain_db."""
    from smartbrain_3000 import db as dbmod, keyvault

    conn = dbmod.open_db(path)
    dbmod.run_migrations(conn)
    keyvault.set_passphrase(conn, "pw-for-test")
    conn.close()


def test_lifespan_applies_staged_restore_and_logs(tmp_path, monkeypatch, caplog) -> None:
    # Stage a valid backup as <db>.restore before app startup. The lifespan must:
    #   1) log a warning that the restore is being applied,
    #   2) actually swap the file (a .pre-restore sibling appears),
    #   3) leave no .restore file behind (consumed).
    from smartbrain_3000 import db as dbmod

    db_path = tmp_path / "live.duckdb"
    _seed_smartbrain_db(db_path)  # the "previous" DB
    staged_src = tmp_path / "new.duckdb"
    _seed_smartbrain_db(staged_src)
    dbmod.staged_restore_path(db_path).write_bytes(staged_src.read_bytes())

    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(db_path))
    with caplog.at_level("WARNING"):
        with TestClient(main.create_app()) as client:
            assert client.get("/api/health").status_code == 200

    assert list(tmp_path.glob("live.duckdb.pre-restore-*"))  # old DB displaced (unique timestamped name)
    assert not dbmod.staged_restore_path(db_path).exists()  # staged file consumed
    assert any("staged database restore" in rec.message for rec in caplog.records)


def test_lifespan_enters_and_exits_cleanly(tmp_path, monkeypatch) -> None:
    # The scheduler + (optional) webrtc background tasks must not deadlock the
    # TestClient enter/exit cycle. A second enter/exit must also work — the
    # gateway pool, conn, and stop-events are reset on each cycle.
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "lifecycle.duckdb"))
    monkeypatch.delenv("SMARTBRAIN_WEBRTC_ENABLED", raising=False)  # default off

    app1 = main.create_app()
    with TestClient(app1) as c1:
        assert c1.get("/api/health").status_code == 200
    assert app1.state.gw_client.is_closed  # shutdown closed the pool

    app2 = main.create_app()
    with TestClient(app2) as c2:
        assert c2.get("/api/health").status_code == 200
    assert app2.state.gw_client.is_closed


def test_lifespan_webrtc_enabled_no_url_logs_and_skips(tmp_path, monkeypatch, caplog) -> None:
    # SMARTBRAIN_WEBRTC_ENABLED set but the signaling URL explicitly emptied (a self-hoster opting
    # OUT of the hosted default): the webrtc task must exit immediately (logging a warning) and
    # shutdown must still complete fast. NOTE: an ABSENT URL now defaults to the hosted node, so we
    # set it empty here — both to exercise the skip path AND to avoid a real network connect in tests.
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "webrtc.duckdb"))
    monkeypatch.setenv("SMARTBRAIN_WEBRTC_ENABLED", "1")
    monkeypatch.setenv("SMARTBRAIN_SIGNALING_URL", "")

    with caplog.at_level("WARNING"):
        with TestClient(main.create_app()) as client:
            assert client.get("/api/health").status_code == 200

    msgs = " ".join(rec.message for rec in caplog.records)
    assert "SIGNALING_URL is unset" in msgs or "remote access off" in msgs


def test_remote_access_lazy_activates_on_pairing(tmp_path, monkeypatch) -> None:
    # Lazy-start (default, no SMARTBRAIN_WEBRTC_ENABLED): a fresh, never-paired vault must NOT
    # activate remote access; pairing a device (the opt-in) must. SIGNALING_URL is emptied so the
    # activated loop doesn't dial a real node during the test.
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "lazy.duckdb"))
    monkeypatch.delenv("SMARTBRAIN_WEBRTC_ENABLED", raising=False)
    monkeypatch.setenv("SMARTBRAIN_SIGNALING_URL", "")
    app = main.create_app()
    with TestClient(app) as client:
        assert not app.state.webrtc_active.is_set()  # fresh + locked -> no opt-in
        assert client.post("/api/account/setup", json={"passphrase": "Passw0rd"}).status_code == 200
        assert not app.state.webrtc_active.is_set()  # unlocked, no devices -> still off
        assert client.post("/api/devices", json={"label": "phone"}).status_code == 200
        assert app.state.webrtc_active.is_set()  # pairing is the opt-in -> remote activates
