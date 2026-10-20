"""Every route that checks the passphrase / Recovery Key shares the unlock throttle.

POST /api/account/passphrase, POST /api/account/passphrase/reset and the backup/export
re-auth (data_routes._reauthorize) each verify the same secret as unlock, so an
already-authenticated session (a stale or stolen paired session, a passer-by at an
unlocked Desktop) could otherwise guess at Argon2 speed with parallel 64 MiB derivations.
These tests pin that they use the SAME slot and failure counter as unlock: a wrong guess
anywhere counts everywhere, a success anywhere resets it, a wrong credential keeps each
route's own status code, and a non-credential error (validation, missing field) is not
counted as a guess.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

GOOD = "correct-horse"
NEW = "brand-new-pass"


@pytest.fixture()
def clock(monkeypatch) -> list[float]:
    from smartbrain_3000 import account

    now = [1000.0]
    monkeypatch.setattr(account, "_monotonic", lambda: now[0])
    return now


@pytest.fixture()
def client(tmp_path, monkeypatch, clock) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:  # setup leaves it unlocked, Desktop session
        test_client.recovery_key = test_client.post(
            "/api/account/setup", json={"passphrase": GOOD}).json()["recovery_key"]
        yield test_client


def _change(client: TestClient, current: str, new: str = NEW):
    return client.post("/api/account/passphrase",
                       json={"current_passphrase": current, "new_passphrase": new})


def _reset(client: TestClient, recovery_key: str, new: str = NEW):
    return client.post("/api/account/passphrase/reset",
                       json={"recovery_key": recovery_key, "new_passphrase": new})


def _export(client: TestClient, **body: str):
    return client.post("/api/export", json=body)


def _backup(client: TestClient, **body: str):
    return client.post("/api/backup", json=body)


# Each route's wrong-credential attempt and the status it has always returned for one.
WRONG = {
    "change": (lambda c: _change(c, "wrong-pass"), 401),
    "reset": (lambda c: _reset(c, "AAAA-BBBB-CCCC"), 401),
    "export": (lambda c: _export(c, passphrase="wrong-pass"), 401),
    "backup": (lambda c: _backup(c, passphrase="wrong-pass"), 401),
    "export-recovery": (lambda c: _export(c, recovery_key="AAAA-BBBB-CCCC"), 401),
}


def _assert_limited(r) -> int:
    assert r.status_code == 429, r.text
    assert r.json()["detail"], "a 429 must say why"
    retry = int(r.headers["Retry-After"])
    assert retry >= 1
    return retry


@pytest.mark.parametrize("route", sorted(WRONG))
def test_wrong_credentials_back_off(client, clock, route) -> None:
    from smartbrain_3000 import account

    attempt, status = WRONG[route]
    for _ in range(account._FREE_FAILURES):
        assert attempt(client).status_code == status
    retry = _assert_limited(attempt(client))
    clock[0] += retry
    assert attempt(client).status_code == status, "the wait is a delay, not a lockout"


def test_failures_count_across_routes_and_toward_unlock(client, clock) -> None:
    from smartbrain_3000 import account

    routes = sorted(WRONG)
    for i in range(account._FREE_FAILURES):  # spread the guesses over every route
        attempt, status = WRONG[routes[i % len(routes)]]
        assert attempt(client).status_code == status
    _assert_limited(_export(client, passphrase=GOOD))  # even the right one waits
    client.post("/api/account/lock")
    retry = _assert_limited(client.post("/api/account/unlock", json={"passphrase": GOOD}))
    clock[0] += retry
    assert client.post("/api/account/unlock", json={"passphrase": GOOD}).status_code == 200


def test_success_on_any_route_resets_the_counter(client, clock) -> None:
    from smartbrain_3000 import account

    for _ in range(account._FREE_FAILURES - 1):
        assert _change(client, "wrong-pass").status_code == 401
    assert _backup(client, passphrase=GOOD).status_code == 200  # resets
    for _ in range(account._FREE_FAILURES):  # a fresh allowance, no 429
        assert _reset(client, "AAAA-BBBB-CCCC").status_code == 401
    clock[0] += _assert_limited(_change(client, GOOD))
    assert _change(client, GOOD).status_code == 200
    client.post("/api/account/lock")
    assert client.post("/api/account/unlock", json={"passphrase": NEW}).status_code == 200


def test_non_credential_errors_are_not_counted(client) -> None:
    from smartbrain_3000 import account

    for _ in range(account._FREE_FAILURES + 3):
        assert _change(client, GOOD, new="short").status_code == 422  # new passphrase too short
        assert _change(client, "", new=NEW).status_code == 422
        assert _reset(client, client.recovery_key, new="short").status_code == 422
        assert _export(client).status_code == 400  # no credential
        assert _backup(client).status_code == 400
    assert _reset(client, client.recovery_key).status_code == 200


def test_recheck_while_a_derivation_runs_gets_429(client, monkeypatch) -> None:
    from smartbrain_3000 import keyvault

    entered, release = threading.Event(), threading.Event()
    real_unlock = keyvault.unlock

    def slow_unlock(conn, passphrase):
        entered.set()
        assert release.wait(10), "test hung"
        return real_unlock(conn, passphrase)

    monkeypatch.setattr(keyvault, "unlock", slow_unlock)
    results: list[int] = []
    worker = threading.Thread(
        target=lambda: results.append(_export(client, passphrase=GOOD).status_code))
    worker.start()
    try:
        assert entered.wait(10), "first check never started"
        for r in (_change(client, GOOD), _backup(client, passphrase=GOOD),
                  _reset(client, client.recovery_key),
                  client.post("/api/account/unlock", json={"passphrase": GOOD})):
            assert _assert_limited(r) == 1
            assert "in progress" in r.json()["detail"]
    finally:
        release.set()
        worker.join(10)
    assert results == [200]
    assert _backup(client, passphrase=GOOD).status_code == 200, "a busy 429 is not a failure"
