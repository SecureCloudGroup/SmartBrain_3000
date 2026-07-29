"""The chat time note speaks the USER's timezone.

The note used to inject bare UTC and let each model do its own timezone
arithmetic for a zone it couldn't know — the live failure was a 9B model
greeting an 11:32 PM (EDT) user with "Good morning! It's 5:32 am on Wednesday".
Now the browser reports its IANA zone via the health handshake and the note
states the local time outright, keeping the UTC anchor for cross-zone math.
"""

from __future__ import annotations

from datetime import datetime, timezone

import duckdb
from fastapi.testclient import TestClient

from smartbrain_3000 import chat_routes
from smartbrain_3000 import db as dbmod


def _conn(tz: str | None) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    if tz is not None:
        dbmod.meta_set(conn, "user:timezone", tz)
    return conn


def _freeze(monkeypatch, *utc_parts: int) -> None:
    class _Fixed(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001 - datetime signature
            return datetime(*utc_parts, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(chat_routes, "datetime", _Fixed)


def test_time_line_speaks_the_users_zone(monkeypatch) -> None:
    # The exact live failure instant: 03:32 UTC on Jul 29 was 11:32 PM Jul 28 in
    # New York — the model must be HANDED that, never asked to derive it.
    _freeze(monkeypatch, 2026, 7, 29, 3, 32)
    line = chat_routes._time_line(_conn("America/New_York"))
    assert line["role"] == "system"
    assert line["content"] == ("Current date and time: Tuesday, July 28, 2026, "
                               "11:32 PM EDT (2026-07-29 03:32 UTC).")


def test_time_line_unknown_zone_falls_back_to_server_local(monkeypatch) -> None:
    _freeze(monkeypatch, 2026, 7, 29, 3, 32)
    for conn in (_conn("Mars/Olympus_Mons"), _conn(""), _conn(None), None):
        line = chat_routes._time_line(conn)
        assert line["content"].startswith("Current date and time: ")
        assert "(2026-07-29 03:32 UTC)." in line["content"]  # anchor survives any fallback


def test_time_line_twelve_hour_edges(monkeypatch) -> None:
    # %-I is not portable (Windows); the manual 12-hour clock must handle both edges.
    _freeze(monkeypatch, 2026, 7, 29, 0, 5)
    assert "12:05 AM" in chat_routes._time_line(_conn("Etc/UTC"))["content"]
    _freeze(monkeypatch, 2026, 7, 29, 12, 5)
    assert "12:05 PM" in chat_routes._time_line(_conn("Etc/UTC"))["content"]


def test_health_records_the_browser_zone(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "tz.duckdb"))
    from smartbrain_3000.main import create_app

    app = create_app()
    with TestClient(app) as client:
        # A valid IANA zone is recorded; re-sending the same zone is idempotent.
        for _ in range(2):
            assert client.get("/api/health",
                              headers={"X-SmartBrain-Timezone": "America/New_York"},
                              ).json()["status"] == "ok"
        assert dbmod.meta_get(app.state.dbx, "user:timezone") == "America/New_York"
        # Garbage and oversized zone names are refused, and never break health.
        for bad in ("Not/AZone", "x" * 65):
            assert client.get("/api/health",
                              headers={"X-SmartBrain-Timezone": bad},
                              ).json()["status"] == "ok"
        assert dbmod.meta_get(app.state.dbx, "user:timezone") == "America/New_York"
