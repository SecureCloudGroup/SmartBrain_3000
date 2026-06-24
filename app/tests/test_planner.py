"""Tests for the encrypted planner: storage layer + HTTP API (H3)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta

import duckdb
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import db as dbmod
from smartbrain_3000.planner import Planner
from smartbrain_3000.secrets import gen_master_key


def _planner(master_key: bytes | None = None) -> Planner:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return Planner(conn, master_key or gen_master_key())


# --- storage layer --------------------------------------------------------

def test_add_get_task_roundtrip() -> None:
    p = _planner()
    tid = p.add_task("Buy milk", "2%", "2026-06-10")
    task = p.get_task(tid)
    assert task["title"] == "Buy milk" and task["notes"] == "2%"
    assert task["status"] == "open" and task["due_date"] == "2026-06-10"


def test_task_without_due_date() -> None:
    p = _planner()
    tid = p.add_task("Someday")
    assert p.get_task(tid)["due_date"] is None


def test_task_new_fields_roundtrip() -> None:
    p = _planner()
    tid = p.add_task(
        "Report", "draft", "2026-06-10",
        due_time="15:00", priority="high", recur="weekly", tags=["work", "q3", "work"],
    )
    t = p.get_task(tid)
    assert t["due_time"] == "15:00" and t["priority"] == "high" and t["recur"] == "weekly"
    assert t["tags"] == ["work", "q3"]  # trimmed + de-duped


def test_invalid_priority_recur_clamped() -> None:
    p = _planner()
    tid = p.add_task("t", priority="urgent", recur="hourly")  # not allowed -> defaults
    t = p.get_task(tid)
    assert t["priority"] == "medium" and t["recur"] == "none"


def test_recurring_done_rolls_forward_and_stays_open() -> None:
    p = _planner()
    # An overdue daily task must roll forward to >= today, not old_date+1 (R1 regression).
    tid = p.add_task("Standup", due_date="2026-06-10", recur="daily")
    p.set_status(tid, "done")
    t = p.get_task(tid)
    assert t["status"] == "open"
    assert t["due_date"] == (date.today() + timedelta(days=1)).isoformat()


def test_recurring_overdue_rolls_to_future_not_past() -> None:
    # 30-day-overdue daily task: completing it must NOT leave it overdue (the bug).
    p = _planner()
    old = (date.today() - timedelta(days=30)).isoformat()
    tid = p.add_task("Old standup", due_date=old, recur="daily")
    p.set_status(tid, "done")
    assert p.get_task(tid)["due_date"] >= date.today().isoformat()
    # Weekly: next slot is also in the future.
    wid = p.add_task("Old weekly", due_date=old, recur="weekly")
    p.set_status(wid, "done")
    assert p.get_task(wid)["due_date"] >= date.today().isoformat()


def test_nonrecurring_done_closes() -> None:
    p = _planner()
    tid = p.add_task("Once", due_date="2026-06-10")
    p.set_status(tid, "done")
    assert p.get_task(tid)["status"] == "done"


def test_set_status_and_update() -> None:
    p = _planner()
    tid = p.add_task("t", "n", "2026-01-01")
    p.set_status(tid, "done")
    assert p.get_task(tid)["status"] == "done"
    p.update_task(tid, "t2", "n2", None)
    task = p.get_task(tid)
    assert task["title"] == "t2" and task["due_date"] is None


def test_set_status_rejects_bad_value() -> None:
    p = _planner()
    tid = p.add_task("t")
    with pytest.raises(AssertionError):
        p.set_status(tid, "archived")


def test_delete_task() -> None:
    p = _planner()
    tid = p.add_task("t")
    p.delete_task(tid)
    assert p.get_task(tid) is None


def test_list_orders_open_first_then_due_date() -> None:
    p = _planner()
    later = p.add_task("later", due_date="2026-12-01")
    soon = p.add_task("soon", due_date="2026-01-01")
    done = p.add_task("done-task", due_date="2026-01-01")
    p.set_status(done, "done")
    order = [t["id"] for t in p.list_tasks()]
    assert order.index(soon) < order.index(later)  # earlier due first
    assert order.index(later) < order.index(done)  # open before done


def test_task_encrypted_at_rest_and_wrong_key() -> None:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    p = Planner(conn, gen_master_key())
    p.add_task("super-secret-title", "super-secret-notes", "2026-01-01")
    raw = b"".join(bytes(r[0]) for r in conn.execute("SELECT ciphertext FROM tasks;").fetchall())
    assert b"super-secret-title" not in raw and b"super-secret-notes" not in raw
    with pytest.raises(Exception):
        Planner(conn, gen_master_key()).list_tasks()


# --- HTTP API -------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def test_planner_requires_unlock(client: TestClient) -> None:
    assert client.get("/api/tasks").status_code == 423
    assert client.post("/api/tasks", json={"title": "x"}).status_code == 423


def test_task_crud_via_api(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    tid = client.post("/api/tasks", json={"title": "Pay rent", "due_date": "2026-06-01"}).json()["id"]
    assert client.get("/api/tasks").json()["tasks"][0]["title"] == "Pay rent"
    assert client.patch(f"/api/tasks/{tid}", json={"status": "done"}).json() == {"ok": True}
    assert client.get("/api/tasks").json()["tasks"][0]["status"] == "done"
    assert client.put(f"/api/tasks/{tid}", json={"title": "Pay rent (June)"}).json() == {"ok": True}
    assert client.delete(f"/api/tasks/{tid}").json() == {"ok": True}
    assert client.get("/api/tasks").json()["tasks"] == []


def test_task_rejects_bad_due_date(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    assert client.post("/api/tasks", json={"title": "x", "due_date": "June 1st"}).status_code == 422


def test_patch_missing_task_404(client: TestClient) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    assert client.patch("/api/tasks/nope", json={"status": "done"}).status_code == 404
