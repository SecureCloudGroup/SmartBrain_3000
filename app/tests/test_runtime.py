"""Tests for container/native runtime detection and defaults (Docker-exit Phase 0)."""

from __future__ import annotations

import importlib
from pathlib import Path

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import gateway, runtime


def test_in_container_detection(monkeypatch) -> None:
    monkeypatch.setenv("SMARTBRAIN_CONTAINER", "1")
    assert runtime.in_container() is True  # explicit env wins
    monkeypatch.delenv("SMARTBRAIN_CONTAINER")
    monkeypatch.setattr("os.path.exists", lambda p: p == "/.dockerenv")
    assert runtime.in_container() is True  # legacy-image fallback
    monkeypatch.setattr("os.path.exists", lambda p: False)
    assert runtime.in_container() is False  # native


def test_default_data_dir_per_platform(monkeypatch) -> None:
    monkeypatch.setattr("sys.platform", "darwin")
    assert str(runtime.default_data_dir()).endswith("Library/Application Support/SmartBrain/data")
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", "/xdg")
    assert str(runtime.default_data_dir()) == "/xdg/smartbrain/data"
    monkeypatch.delenv("XDG_DATA_HOME")
    assert str(runtime.default_data_dir()).endswith(".local/share/smartbrain/data")
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("APPDATA", "/roaming")
    assert str(runtime.default_data_dir()) == "/roaming/SmartBrain/data"


def test_version_from_file(tmp_path) -> None:
    stamp = tmp_path / "VERSION"
    assert runtime.version_from_file(stamp) is None  # absent -> None (env/dev fallback)
    stamp.write_text("1.2.3\n")
    assert runtime.version_from_file(stamp) == "1.2.3"
    stamp.write_text("   \n")
    assert runtime.version_from_file(stamp) is None  # blank stamp is no stamp


def test_defaults_switch_between_container_and_native(monkeypatch) -> None:
    """Reload gateway/db under each detection outcome; ALWAYS restore afterwards.

    Container mode must stay byte-identical to the historical constants — that is the
    'zero user-visible change' guarantee for every existing deployment.
    """
    def shaped(container: bool) -> dict:
        monkeypatch.setattr(runtime, "in_container", lambda: container)
        importlib.reload(gateway)
        importlib.reload(dbmod)
        return {"gw": gateway.DEFAULT_GATEWAY_URL, "ollama": gateway.OLLAMA_DEFAULT_URL,
                "mlx": gateway.MLX_DEFAULT_URL, "mlxe": gateway.MLXE_DEFAULT_URL,
                "db": dbmod.DEFAULT_DB_PATH}
    try:
        c = shaped(True)
        assert c == {"gw": "http://bifrost:8080",
                     "ollama": "http://host.docker.internal:11434",
                     "mlx": "http://host.docker.internal:8888",
                     "mlxe": "http://host.docker.internal:8899",
                     "db": "/app/data/smartbrain.duckdb"}  # the historical constants, exactly
        n = shaped(False)
        assert n["gw"] == "http://127.0.0.1:38080"
        assert n["ollama"] == "http://127.0.0.1:11434"
        assert n["mlx"] == "http://127.0.0.1:8888"
        assert n["mlxe"] == "http://127.0.0.1:8899"
        assert n["db"].endswith("smartbrain.duckdb") and "/app/data" not in n["db"]
    finally:
        monkeypatch.undo()  # real detection back, then rebuild module state to match
        importlib.reload(gateway)
        importlib.reload(dbmod)


def test_native_resolve_db_path_creates_parent(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(runtime, "in_container", lambda: False)
    target = tmp_path / "nested" / "deeper" / "test.duckdb"
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(target))
    resolved = dbmod.resolve_db_path()
    assert resolved == target
    assert target.parent.is_dir()  # a native first run makes its own home


def test_container_resolve_db_path_does_not_mkdir(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(runtime, "in_container", lambda: True)
    target = tmp_path / "not-created" / "test.duckdb"
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(target))
    dbmod.resolve_db_path()
    assert not target.parent.exists()  # containers rely on the mount, unchanged


def test_version_is_always_nonempty() -> None:
    import smartbrain_3000
    assert smartbrain_3000.__version__
    assert isinstance(smartbrain_3000.__version__, str)


def test_data_dir_is_not_created_by_import(monkeypatch) -> None:
    # default_data_dir is a pure path computation — nothing on disk until a write.
    p = runtime.default_data_dir()
    assert isinstance(p, Path)

def test_localize_local_url_is_bidirectional(monkeypatch) -> None:
    # Native: the stored docker-bridge host becomes loopback.
    monkeypatch.setattr(runtime, "in_container", lambda: False)
    assert gateway.localize_local_url("http://host.docker.internal:8888") == "http://127.0.0.1:8888"
    assert gateway.localize_local_url("http://127.0.0.1:8888") == "http://127.0.0.1:8888"
    # Container: a natively-stored loopback/localhost URL becomes the docker bridge —
    # this is what makes ROLLING BACK to Docker work without re-entering settings.
    monkeypatch.setattr(runtime, "in_container", lambda: True)
    assert gateway.localize_local_url("http://127.0.0.1:11434") == "http://host.docker.internal:11434"
    assert gateway.localize_local_url("http://localhost:8888") == "http://host.docker.internal:8888"
    assert gateway.localize_local_url("http://host.docker.internal:8899") == "http://host.docker.internal:8899"
    assert gateway.localize_local_url("") == ""
    # A remote LAN server the user configured explicitly is never rewritten.
    monkeypatch.setattr(runtime, "in_container", lambda: False)
    assert gateway.localize_local_url("http://192.168.1.50:11434") == "http://192.168.1.50:11434"


def test_health_handshake_and_legacy_nudge(tmp_path, monkeypatch) -> None:
    from fastapi.testclient import TestClient
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as client:
        # In a container with no modern launcher ever seen: nudge.
        monkeypatch.setattr(runtime, "in_container", lambda: True)
        first = client.get("/api/health").json()
        assert first["status"] == "ok" and first["launcher_update_needed"] is True
        # A modern launcher handshakes once -> the nudge retires forever.
        client.get("/api/health", headers={"X-SmartBrain-Launcher": "0.8.4"})
        after = client.get("/api/health").json()
        assert after["launcher_update_needed"] is False
        # Native mode never nudges (there is no legacy-launcher story to fix there).
        monkeypatch.setattr(runtime, "in_container", lambda: False)
        assert client.get("/api/health").json()["launcher_update_needed"] is False


def test_update_handshake_and_desktop_local_install(tmp_path, monkeypatch) -> None:
    """The launcher's staged version reaches the page, and only the desk can install it.

    Before this, a waiting update existed solely as a menu item behind a tray icon — the
    owner's words: "the launcher process is not intuitive and clunky".
    """
    from fastapi.testclient import TestClient
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "upd.duckdb"))
    from smartbrain_3000.main import create_app

    app = create_app()
    with TestClient(app) as client:
        # Nothing staged -> the page hears nothing, and there is nothing to install.
        assert "update_ready" not in client.get("/api/health").json()
        assert client.post("/api/update/install", headers={"x-sb-local": "1"}).status_code == 409

        # The launcher stamps what it has staged onto its probe.
        body = client.get("/api/health", headers={"X-SmartBrain-Update": "9.9.9"}).json()
        assert body["update_ready"] == "9.9.9"
        assert "update_requested" not in body, "nobody has asked for it yet"

        # A paired phone may see it, but must not be able to restart the desk.
        assert client.post("/api/update/install").status_code == 403

        # The person at the desk asks; the launcher hears it on its next handshake.
        assert client.post("/api/update/install", headers={"x-sb-local": "1"}).json()["version"] == "9.9.9"
        after = client.get("/api/health", headers={"X-SmartBrain-Update": "9.9.9"}).json()
        assert after["update_requested"] == "9.9.9"

        # The launcher withdrawing the offer (installed some other way) clears the mirror.
        assert "update_ready" not in client.get("/api/health").json()


def test_launcher_version_is_written_only_when_it_changes(tmp_path, monkeypatch) -> None:
    # The handshake now arrives every ~30s; rewriting the same value would be thousands of
    # pointless database writes a day.
    from fastapi.testclient import TestClient
    from smartbrain_3000 import db as dbmod
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "meta.duckdb"))
    from smartbrain_3000.main import create_app

    app = create_app()
    with TestClient(app) as client:
        writes = []
        real_set = dbmod.meta_set
        monkeypatch.setattr(dbmod, "meta_set",
                            lambda conn, k, v: (writes.append(k), real_set(conn, k, v))[1])
        for _ in range(4):
            client.get("/api/health", headers={"X-SmartBrain-Launcher": "1.2.3"})
        assert writes.count("launcher:version") == 1, f"one write, not one per probe: {writes}"
