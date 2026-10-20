"""POST /api/account/unlock is an open route that runs Argon2id (64 MiB, t=3) per attempt.

These tests pin its guessing / memory-exhaustion defence: failed attempts back off
process-wide (exponential, capped, reset on success), concurrent derivations are capped
(excess requests get 429 at once instead of queueing), and the recovery-key form of the
route is covered by the same limit. Every 429 carries a detail and a Retry-After.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

GOOD = "correct-horse"


@pytest.fixture()
def clock(monkeypatch) -> list[float]:
    from smartbrain_3000 import account

    now = [1000.0]
    monkeypatch.setattr(account, "_monotonic", lambda: now[0])
    return now


@pytest.fixture()
def derivations(monkeypatch) -> list[str]:
    """Count every real Argon2 derivation the unlock route asks for (both credential forms)."""
    from smartbrain_3000 import keyvault

    calls: list[str] = []
    real_unlock, real_recovery = keyvault.unlock, keyvault.unlock_with_recovery

    def spy_unlock(conn, passphrase):
        calls.append("passphrase")
        return real_unlock(conn, passphrase)

    def spy_recovery(conn, recovery_key):
        calls.append("recovery")
        return real_recovery(conn, recovery_key)

    monkeypatch.setattr(keyvault, "unlock", spy_unlock)
    monkeypatch.setattr(keyvault, "unlock_with_recovery", spy_recovery)
    return calls


@pytest.fixture()
def client(tmp_path, monkeypatch, clock) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        test_client.recovery_key = test_client.post(
            "/api/account/setup", json={"passphrase": GOOD}).json()["recovery_key"]
        test_client.post("/api/account/lock")
        yield test_client


def _unlock(client: TestClient, **body: str):
    return client.post("/api/account/unlock", json=body)


def _assert_limited(r) -> int:
    assert r.status_code == 429, r.text
    assert r.json()["detail"], "a 429 must say why"
    retry = int(r.headers["Retry-After"])
    assert retry >= 1
    return retry


def test_wrong_passphrases_back_off_before_any_derivation(client, clock, derivations) -> None:
    from smartbrain_3000 import account

    for _ in range(account._FREE_FAILURES):
        assert _unlock(client, passphrase="wrong-pass").status_code == 401
    assert len(derivations) == account._FREE_FAILURES
    retry = _assert_limited(_unlock(client, passphrase="wrong-pass"))
    # Even the RIGHT passphrase waits out the delay: the attempt is refused before Argon2 runs.
    _assert_limited(_unlock(client, passphrase=GOOD))
    assert len(derivations) == account._FREE_FAILURES, "a limited attempt must not derive"
    clock[0] += retry
    assert _unlock(client, passphrase=GOOD).status_code == 200


def test_success_resets_the_backoff(client, clock, derivations) -> None:
    from smartbrain_3000 import account

    for _ in range(account._FREE_FAILURES):
        _unlock(client, passphrase="wrong-pass")
    clock[0] += _assert_limited(_unlock(client, passphrase="wrong-pass"))
    assert _unlock(client, passphrase=GOOD).status_code == 200
    client.post("/api/account/lock")
    for _ in range(account._FREE_FAILURES):  # a fresh allowance, no 429
        assert _unlock(client, passphrase="wrong-pass").status_code == 401


def test_backoff_doubles_and_is_capped(client, clock, monkeypatch) -> None:
    from smartbrain_3000 import account, keyvault

    def fast_wrong(conn, passphrase):  # skip Argon2: this test drives many failures
        raise ValueError("bad passphrase")

    monkeypatch.setattr(keyvault, "unlock", fast_wrong)
    for _ in range(account._FREE_FAILURES):
        _unlock(client, passphrase="x")
    seen: list[int] = []
    for _ in range(40):  # far past the cap
        retry = _assert_limited(_unlock(client, passphrase="x"))
        seen.append(retry)
        clock[0] += retry
        assert _unlock(client, passphrase="x").status_code == 401
    assert seen[:4] == [1, 2, 4, 8], seen
    assert max(seen) == int(account._BACKOFF_MAX_S), "the delay is capped, never a permanent lockout"
    assert seen[-1] == int(account._BACKOFF_MAX_S)


def test_recovery_key_unlock_shares_the_limit(client, clock, derivations) -> None:
    from smartbrain_3000 import account

    for i in range(account._FREE_FAILURES):  # mix the two credential forms
        body = {"recovery_key": "AAAA-BBBB"} if i % 2 else {"passphrase": "wrong-pass"}
        assert _unlock(client, **body).status_code == 401
    retry = _assert_limited(_unlock(client, recovery_key=client.recovery_key))
    assert len(derivations) == account._FREE_FAILURES
    clock[0] += retry
    assert _unlock(client, recovery_key=client.recovery_key).status_code == 200


def test_missing_credential_is_not_a_failed_attempt(client, derivations) -> None:
    from smartbrain_3000 import account

    for _ in range(account._FREE_FAILURES + 3):
        assert _unlock(client).status_code == 400
    assert derivations == []
    assert _unlock(client, passphrase=GOOD).status_code == 200


def test_concurrent_unlock_gets_429_instead_of_queueing(client, monkeypatch) -> None:
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
        target=lambda: results.append(_unlock(client, passphrase=GOOD).status_code))
    worker.start()
    try:
        assert entered.wait(10), "first unlock never started"
        r = _unlock(client, passphrase=GOOD)
        assert _assert_limited(r) == 1
        assert "in progress" in r.json()["detail"]
    finally:
        release.set()
        worker.join(10)
    assert results == [200]
    client.post("/api/account/lock")
    assert _unlock(client, passphrase=GOOD).status_code == 200, "a busy 429 is not a failure"
