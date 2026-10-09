"""Browser components on the Status API (§35) — readable while locked, sizes kept apart
from the voice model — and the backup invariant: an encrypted backup is the database
alone, so an engine under ``<data>/browsers`` can never ride along."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import db as dbmod
from smartbrain_3000.browsers import install, router, walls

ROW_KEYS = {"name", "phase", "pct", "version", "age_days", "eligible", "reason", "sandbox",
            "last_self_test", "error", "health"}


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    with install._state_lock:  # earlier browser tests leave process-wide install state behind
        install._state.clear()
    router.BREAKER.reset()
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "t.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as c:
        yield c


def test_browser_rows_are_readable_while_locked(client: TestClient) -> None:
    body = client.get("/api/status/overview").json()
    assert body["unlocked"] is False
    rows = {r["name"]: r for r in body["browsers"]}
    assert set(rows) == {"obscura", "obscura-stealth"}
    for row in rows.values():
        assert set(row) == ROW_KEYS
        assert (row["phase"], row["eligible"], row["reason"]) == ("disabled", False, "disabled")
        assert (row["version"], row["health"], row["last_self_test"]) == ("0.2.4", "ok", "never")
    assert body["storage"]["browser_bytes"] == 0


def test_enabled_rows_fail_closed_without_a_wall(client: TestClient, monkeypatch) -> None:
    monkeypatch.delenv(install.DISABLE_ENV)
    monkeypatch.delenv(walls.NULLWALL_ENV, raising=False)
    rows = {r["name"]: r for r in client.get("/api/status/overview").json()["browsers"]}
    for row in rows.values():
        assert row["eligible"] is False and row["sandbox"] == "none"
        assert row["phase"] in ("unavailable",) and row["reason"] in walls.REASONS


def test_browser_bytes_are_counted_apart_from_the_voice_model(client: TestClient, tmp_path) -> None:
    engine = tmp_path / "browsers" / "obscura" / "0.2.4"
    engine.mkdir(parents=True)
    (engine / "obscura").write_bytes(b"x" * 1234)
    storage = client.get("/api/status/overview").json()["storage"]
    assert storage["browser_bytes"] == 1234 and storage["models_bytes"] == 0
    assert storage["total_bytes"] >= storage["db_bytes"] + 1234


def test_backups_never_include_browser_engines(client: TestClient, tmp_path) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    marker = b"BROWSER-ENGINE-BYTES-" + b"z" * 64
    engine = tmp_path / "browsers" / "obscura" / "0.2.4"
    engine.mkdir(parents=True)
    (engine / "obscura").write_bytes(marker)
    (tmp_path / "browsers" / "obscura" / "0.2.4.tar.gz.part").write_bytes(marker)
    r = client.post("/api/backup", json={"passphrase": "correct-horse"})
    assert r.status_code == 200 and r.content
    assert marker not in r.content
    saved = tmp_path / "copy.duckdb"
    saved.write_bytes(r.content)
    assert dbmod.is_smartbrain_db(saved)  # the backup is the database, and only that
