"""Thread-safety of DB access (the ThreadLocalConn facade).

A shared DuckDB connection corrupts under concurrent threads (one thread's
execute clobbers another's pending result). These tests pin that the facade
isolates threads, and that real concurrent requests through the app stay clean.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import duckdb
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import db as dbmod

_THREADS = 16
_ITERS = 200


def test_threadlocalconn_isolates_thread_results() -> None:
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE t (tid INTEGER);")
    for tid in range(_THREADS):
        conn.execute("INSERT INTO t VALUES (?);", [tid])
    dbx = dbmod.ThreadLocalConn(conn)
    mismatches: list = []
    errors: list = []

    def worker(tid: int) -> None:
        try:
            for _ in range(_ITERS):
                rel = dbx.execute("SELECT tid FROM t WHERE tid = ?;", [tid])
                time.sleep(0)  # widen the execute->fetch window (exposes clobbering)
                if rel.fetchone()[0] != tid:
                    mismatches.append(tid)
        except Exception as exc:  # noqa: BLE001 - record, don't crash the thread
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors[:3]
    assert not mismatches, f"{len(mismatches)} result mismatches — threads not isolated"


def test_threadlocalconn_reuses_one_cursor_per_thread() -> None:
    dbx = dbmod.ThreadLocalConn(duckdb.connect(":memory:"))
    assert dbx._cursor() is dbx._cursor()  # same thread -> same cursor

    other: list = []
    t = threading.Thread(target=lambda: other.append(dbx._cursor()))
    t.start()
    t.join()
    assert other[0] is not dbx._cursor()  # a different thread -> a different cursor


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def test_concurrent_requests_stay_consistent(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    client.post("/api/kb", json={"title": "Doc", "content": "hello world"})
    client.post("/api/tasks", json={"title": "task one", "notes": "", "due_date": None})
    errors: list = []
    routes = ["/api/tasks", "/api/schedules", "/api/kb", "/api/memories", "/api/audit"]

    def hammer(_: int) -> None:
        try:
            for _i in range(12):
                for route in routes:  # bounded
                    r = client.get(route)
                    if r.status_code != 200:
                        errors.append(f"{route} -> {r.status_code}")
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors[:5]
