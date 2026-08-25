"""The aggregate /api/status surface (Settings → Status page's data source)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "t.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as c:
        yield c


def test_status_works_locked(client: TestClient) -> None:
    """A locked app still shows version, lock state, and the voice-model download —
    the things a user stares at before they can even unlock."""
    r = client.get("/api/status/overview")
    assert r.status_code == 200
    body = r.json()
    assert body["unlocked"] is False
    assert body["version"]
    assert body["voice_local"]["phase"] in ("absent", "downloading", "loading", "ready", "error")
    assert "knowledge" not in body  # encrypted-store sections wait for unlock


def test_status_unlocked_has_every_section(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "status-test-pass"})
    body = client.get("/api/status/overview").json()
    assert body["unlocked"] is True
    assert body["voice"]["engine"] in ("server", "local")
    assert set(body["local_models"]) == {"ollama_configured", "mlx_configured", "mlxe_configured"}
    assert body["knowledge"] == {"documents": 0, "embedded_chunks": 0}
    assert body["schedules"]["total"] >= 0
    assert body["feeds"]["count"] == 0
    assert body["devices"]["paired"] == 0
