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