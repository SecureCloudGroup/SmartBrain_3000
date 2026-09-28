"""The app's SmartBrain Library API client: a sealed outbox the scheduler sends from.

Never touches the network: every send goes through a fake ``net`` (and conftest switches the real
API off for the app's own tick)."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import library_client, netguard
from smartbrain_3000 import ni as nimod
from smartbrain_3000.secrets import gen_master_key


@pytest.fixture()
def store(monkeypatch) -> nimod.NIStore:
    monkeypatch.setenv("SMARTBRAIN_LIBRARY_API", "https://library.example.org/v1")
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return nimod.NIStore(conn, gen_master_key())


class _Net:
    def __init__(self, fail: dict | None = None) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.fail = fail or {}

    def safe_post_json(self, url: str, body: dict) -> dict:
        path = url.rsplit("/", 1)[1]
        if path in self.fail:
            raise netguard.FetchError("refused", status=self.fail[path])
        self.sent.append((url, body))
        return {"ok": True}


def test_a_vote_sends_only_the_source_id_and_verdict(store) -> None:
    library_client.queue_vote(store, "coops-tide-hilo")
    net = _Net()
    assert library_client.flush(store, net=net) == {"sent": 1, "dropped": 0, "waiting": 0}
    url, body = net.sent[0]
    assert url == "https://library.example.org/v1/votes"
    assert set(body) == {"source_id", "verdict", "app_version"} and body["verdict"] == "yes"
    assert library_client.pending(store) == 0


def test_the_outbox_is_sealed_at_rest(store) -> None:
    library_client.queue_vote(store, "coops-tide-hilo")
    raw = store.conn.execute("SELECT ciphertext FROM ni_snapshots WHERE slot = 'outbox'").fetchone()[0]
    assert b"coops" not in bytes(raw)


def test_a_refusal_is_dropped_and_an_outage_backs_off(store) -> None:
    library_client.queue_vote(store, "a-source")
    library_client.queue_suggestion(store, _local_record())
    now = datetime.now(UTC)
    out = library_client.flush(store, net=_Net(fail={"votes": 422, "suggestions": 503}), now=now)
    assert out == {"sent": 0, "dropped": 1, "waiting": 1}  # the 422 never becomes acceptable
    assert library_client.flush(store, net=_Net(), now=now + timedelta(minutes=5))["sent"] == 0  # backing off
    assert library_client.flush(store, net=_Net(), now=now + timedelta(minutes=16))["sent"] == 1


def test_switched_off_means_nothing_is_sent(store, monkeypatch) -> None:
    monkeypatch.setenv("SMARTBRAIN_LIBRARY_API", "")
    library_client.queue_vote(store, "a-source")
    net = _Net()
    assert library_client.flush(store, net=net)["waiting"] == 1 and net.sent == []


def test_a_vote_queued_during_a_send_is_kept(store) -> None:
    library_client.queue_vote(store, "first")

    class _Slow(_Net):
        def safe_post_json(self, url, body):
            t = threading.Thread(target=library_client.queue_vote, args=(store, "second"))
            t.start()
            t.join()
            return super().safe_post_json(url, body)

    library_client.flush(store, net=_Slow())
    assert [b["source_id"] for _, b in _drain(store)] == ["second"]


def _drain(store):
    net = _Net()
    library_client.flush(store, net=net)
    return net.sent


def _local_record() -> dict:
    return {"id": "local-abc", "name": "My river gauge", "description": "levels",
            "provider": {"id": "local", "name": "water.example.org", "url": "https://water.example.org",
                         "authority": "community"},
            "tier": "local", "categories": ["water/water_levels"], "kinds": ["lookup"],
            "coverage": {"geo": "local", "entity": ""},
            "access": {"kind": "http_json", "url_template": "https://water.example.org/g?site={site}",
                       "params": [{"name": "site", "kind": "none", "example": "0123", "required": True}],
                       "auth": "none", "headers": {"X-Token": "t"}, "docs_url": "https://water.example.org"},
            "terms": {"status": "unverified", "note": "added by you", "terms_url": ""},
            "freshness": {"cadence": "irregular"}, "examples": ["my gauge"], "notes": "",
            "origin": {"by": "user", "at": "now"}, "validation": {"status": "unvalidated"},
            "votes": {"yes": 3, "no": 0}}


def test_a_suggestion_carries_the_template_never_a_value() -> None:
    rec = library_client.suggestion_record(_local_record())
    text = json.dumps(rec)
    assert "0123" not in text and "my gauge" not in text and "X-Token" not in text
    assert not {"id", "tier", "origin", "votes", "validation"} & set(rec)
    assert rec["access"]["params"] == [{"name": "site", "kind": "none", "required": True, "example": None}]
    assert rec["access"]["url_template"] == "https://water.example.org/g?site={site}"
