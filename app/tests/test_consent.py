"""Tests for remembered tool consent (consent.py + the agent/approve wiring)."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import consent, db, gateway


def _conn(tmp_path):
    conn = db.open_db(tmp_path / "c.duckdb")
    db.run_migrations(conn)  # creates meta
    return conn


def test_remember_only_reviewed(tmp_path) -> None:
    conn = _conn(tmp_path)
    assert consent.remember(conn, "remember_fact") is True   # REVIEWED write -> remembered
    assert consent.remember(conn, "delete_task") is False     # IRREVERSIBLE -> refused
    assert consent.remember(conn, "no_such_tool") is False    # unknown -> refused
    assert consent.remembered(conn) == {"remember_fact"}


def test_model_addressed_egress_is_never_remembered(tmp_path) -> None:
    """A tool the model can AIM must keep re-asking, whatever its tier.

    web_fetch and kb_ingest_url take a `url` straight from the model, so a remembered
    one would let injected instructions reach an attacker's server unattended. That
    per-call review is the control, and it must not be waivable.
    """
    conn = _conn(tmp_path)
    for name in ("web_fetch", "kb_ingest_url"):
        assert consent.remember(conn, name) is False, name
        assert consent.is_rememberable(name) is False, name
    assert consent.remembered(conn) == set()


def test_fixed_destination_egress_stays_rememberable(tmp_path) -> None:
    """Egress the USER pointed somewhere is not an exfiltration channel.

    A search goes to the configured provider and email reads go to the user's own
    mailbox: the model supplies a query, never an address, so there is no attacker
    endpoint to receive anything. These are also the most-used tools — making them
    re-ask forever is a real cost for no security gain.
    """
    conn = _conn(tmp_path)
    for name in ("web_search", "web_research", "email_read", "email_list"):
        assert consent.remember(conn, name) is True, name
        assert consent.is_rememberable(name) is True, name
    assert consent.remember(conn, "email_send") is False  # IRREVERSIBLE, unchanged
    assert consent.remembered(conn) == {"web_search", "web_research", "email_read", "email_list"}


def test_remembered_ignores_egress_written_by_an_older_build(tmp_path) -> None:
    """Filters on READ too, so a row written before this rule cannot auto-approve."""
    conn = _conn(tmp_path)
    db.meta_set(conn, "remembered_tools", json.dumps(["web_fetch", "remember_fact"]))
    assert consent.remembered(conn) == {"remember_fact"}


def test_forget(tmp_path) -> None:
    conn = _conn(tmp_path)
    consent.remember(conn, "remember_fact")
    consent.forget(conn, "remember_fact")
    assert consent.remembered(conn) == set()


def test_remembered_corrupt_config_is_empty(tmp_path) -> None:
    conn = _conn(tmp_path)
    db.meta_set(conn, "remembered_tools", "{not valid json")
    assert consent.remembered(conn) == set()  # safest: remember nothing -> re-ask


def test_remembered_tier_filters_poisoned_entries(tmp_path) -> None:
    # Defense-in-depth: even if an IRREVERSIBLE/unknown name is written straight to
    # the meta row, the read path must drop it (only REVIEWED survives).
    conn = _conn(tmp_path)
    db.meta_set(conn, "remembered_tools", json.dumps(["delete_task", "no_such_tool", "remember_fact"]))
    assert consent.remembered(conn) == {"remember_fact"}


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / "test.duckdb"))
    from smartbrain_3000.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def _reviewed_tool_call(*a, **k):
    """A model response proposing a REVIEWED write (remember_fact)."""
    args = json.dumps({"text": "I like tea"})
    return {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "remember_fact", "arguments": args}},
    ]}}]}


def test_approve_with_remember_roundtrip(client: TestClient, monkeypatch) -> None:
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    monkeypatch.setattr(gateway, "chat_with_tools", _reviewed_tool_call)
    assert client.get("/api/agent/remembered").json()["tools"] == []  # nothing remembered yet
    # A REVIEWED write parks for approval (consent empty).
    turn = client.post("/api/agent/turn", json={"messages": [{"role": "user", "content": "remember"}], "model": "m"})
    body = turn.json()
    assert body["status"] == "awaiting_approval"
    pid = body["pending"][0]["id"]
    # Approve + remember -> consent recorded.
    assert client.post(f"/api/agent/pending/{pid}/approve", json={"remember": True}).status_code == 200
    assert client.get("/api/agent/remembered").json()["tools"] == ["remember_fact"]
    # Revoke.
    assert client.delete("/api/agent/remembered/remember_fact").status_code == 200
    assert client.get("/api/agent/remembered").json()["tools"] == []


# --- per-site "Always allow" for the URL tools ----------------------------


def test_remember_mode_per_tool_tier(tmp_path) -> None:
    """"tool" for whole-tool consent, "site" for the URL tools, None otherwise."""
    for name in ("remember_fact", "add_task", "save_note",
                 "web_search", "web_research", "email_read", "email_list"):
        assert consent.remember_mode(name) == "tool", name
    for name in ("web_fetch", "kb_ingest_url"):
        assert consent.remember_mode(name) == "site", name
    for name in ("delete_task", "email_send", "kb_search", "list_tasks", "no_such"):
        assert consent.remember_mode(name) is None, name


def test_remember_site_stores_lowercase_host(tmp_path) -> None:
    """The stored host is lowercased/normalized by urllib — case-insensitive by design."""
    conn = _conn(tmp_path)
    assert consent.remember_site(conn, "web_fetch", "HTTPS://News.Example.COM/latest?x=1") is True
    assert consent.remembered(conn) == {"web_fetch@news.example.com"}


def test_remember_site_refuses_non_site_tools(tmp_path) -> None:
    """Whole-tool remember for web_fetch/kb_ingest_url stays refused; site is the ONLY path."""
    conn = _conn(tmp_path)
    for name in ("web_fetch", "kb_ingest_url"):
        assert consent.remember(conn, name) is False, name          # whole-tool refused (unchanged)
    assert consent.remember_site(conn, "remember_fact", "https://x/") is False   # not site-mode
    assert consent.remember_site(conn, "email_send", "https://x/") is False       # irreversible
    assert consent.remembered(conn) == set()


def test_remember_site_refuses_bad_urls(tmp_path) -> None:
    """A URL with no valid host must not create an entry the reader would just drop."""
    conn = _conn(tmp_path)
    for bad in ("", "not a url", "file:///etc/passwd", "http://", "http:///path", "javascript:alert(1)"):
        assert consent.remember_site(conn, "web_fetch", bad) is False, bad
    assert consent.remembered(conn) == set()


def test_allowed_exact_host_match_and_no_subdomain_inheritance(tmp_path) -> None:
    """A remembered host matches EXACTLY: a sibling subdomain still parks."""
    conn = _conn(tmp_path)
    consent.remember_site(conn, "web_fetch", "https://news.example.com/a")
    entries = consent.remembered(conn)
    assert consent.allowed_in(entries, "web_fetch", {"url": "https://news.example.com/other"}) is True
    assert consent.allowed_in(entries, "web_fetch", {"url": "https://www.news.example.com/x"}) is False
    assert consent.allowed_in(entries, "web_fetch", {"url": "https://example.com/x"}) is False
    # A different site-mode tool (kb_ingest_url) is not auto-approved by a web_fetch entry.
    assert consent.allowed_in(entries, "kb_ingest_url", {"url": "https://news.example.com/x"}) is False
    # Whole-tool tools still work by name (no URL involved).
    entries2 = entries | {"remember_fact"}
    assert consent.allowed_in(entries2, "remember_fact", {"text": "hi"}) is True


def test_allowed_missing_or_malformed_url_is_false(tmp_path) -> None:
    """`allowed` must be TOTAL — a bad url returns False, never raises."""
    conn = _conn(tmp_path)
    consent.remember_site(conn, "web_fetch", "https://news.example.com/a")
    entries = consent.remembered(conn)
    for args in ({}, {"url": None}, {"url": ""}, {"url": 123}, {"url": "not a url"}):
        assert consent.allowed(conn, "web_fetch", args) is False, args
        assert consent.allowed_in(entries, "web_fetch", args) is False, args


def test_self_defending_read_drops_malformed_and_stale_entries(tmp_path) -> None:
    """The reader must accept ONLY entries that satisfy the current tool tier + mode."""
    conn = _conn(tmp_path)
    # A grab bag of things a corrupt writer or a schema change could leave behind.
    db.meta_set(conn, "remembered_tools", json.dumps([
        "remember_fact",                # valid: whole-tool
        "web_fetch@news.example.com",   # valid: site
        "web_fetch",                    # invalid: site-mode tool can't be whole-tool remembered
        "remember_fact@example.com",    # invalid: whole-tool tool has no site entries
        "@example.com",                 # invalid: empty tool name
        "web_fetch@",                   # invalid: empty host
        "web_fetch@a@b",                # invalid: multiple @
        "web_fetch@bad host!",          # invalid: host syntax
        "delete_task",                  # invalid: IRREVERSIBLE (mode == None)
        "no_such_tool",                 # invalid: unknown tool
    ]))
    assert consent.remembered(conn) == {"remember_fact", "web_fetch@news.example.com"}


def _web_fetch_response(url: str) -> dict:
    """A model response proposing web_fetch(url)."""
    args = json.dumps({"url": url})
    return {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "wf1", "type": "function", "function": {"name": "web_fetch", "arguments": args}},
    ]}}]}


def _always_returns(response: dict):
    """A chat_with_tools stub that always returns the given response."""
    return lambda *a, **k: response


def test_approve_with_remember_on_web_fetch_stores_site_and_auto_runs(
    client: TestClient, monkeypatch
) -> None:
    """End-to-end: approve web_fetch with remember, then the SAME host runs without parking."""
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    from smartbrain_3000 import netguard

    monkeypatch.setattr(netguard, "safe_fetch",
                        lambda url: {"final_url": url, "status": 200, "text": "ok"})
    # First turn: park a web_fetch, approve + remember -> stores web_fetch@example.com.
    monkeypatch.setattr(gateway, "chat_with_tools",
                        _always_returns(_web_fetch_response("https://example.com/one")))
    turn = client.post("/api/agent/turn",
                       json={"messages": [{"role": "user", "content": "fetch"}], "model": "m"})
    pid = turn.json()["pending"][0]["id"]
    approve = client.post(f"/api/agent/pending/{pid}/approve", json={"remember": True})
    assert approve.status_code == 200
    assert client.get("/api/agent/remembered").json()["sites"] == [
        {"tool": "web_fetch", "host": "example.com"}
    ]
    # Second turn: same host + a plain reply — must NOT park.
    seq = iter([_web_fetch_response("https://example.com/two"),
                {"choices": [{"message": {"content": "done"}}]}])
    monkeypatch.setattr(gateway, "chat_with_tools", lambda *a, **k: next(seq))
    result = client.post("/api/agent/turn",
                         json={"messages": [{"role": "user", "content": "again"}], "model": "m"}).json()
    assert result["status"] == "complete" and result["message"] == "done"
    # Third turn: a DIFFERENT host still parks — the injected-URL case survives.
    monkeypatch.setattr(gateway, "chat_with_tools",
                        _always_returns(_web_fetch_response("https://attacker.example.net/steal")))
    other = client.post("/api/agent/turn",
                        json={"messages": [{"role": "user", "content": "fetch attacker"}], "model": "m"}).json()
    assert other["status"] == "awaiting_approval"


def test_remembered_endpoint_returns_sites_and_forget_removes_one(
    client: TestClient, monkeypatch
) -> None:
    """The GET returns UI-consumable site records; DELETE with ?host= removes just one."""
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    from smartbrain_3000 import netguard

    monkeypatch.setattr(netguard, "safe_fetch",
                        lambda url: {"final_url": url, "status": 200, "text": "ok"})
    for url in ("https://a.example.com/x", "https://b.example.com/y"):
        monkeypatch.setattr(gateway, "chat_with_tools",
                            _always_returns(_web_fetch_response(url)))
        pid = client.post("/api/agent/turn",
                          json={"messages": [{"role": "user", "content": url}], "model": "m"}
                          ).json()["pending"][0]["id"]
        client.post(f"/api/agent/pending/{pid}/approve", json={"remember": True})
    body = client.get("/api/agent/remembered").json()
    assert body["tools"] == []
    assert body["sites"] == [
        {"tool": "web_fetch", "host": "a.example.com"},
        {"tool": "web_fetch", "host": "b.example.com"},
    ]
    # Forget one host; the other survives.
    assert client.delete("/api/agent/remembered/web_fetch", params={"host": "a.example.com"}).status_code == 200
    assert client.get("/api/agent/remembered").json()["sites"] == [
        {"tool": "web_fetch", "host": "b.example.com"}
    ]


def test_pending_payload_carries_remember_mode_and_host(
    client: TestClient, monkeypatch
) -> None:
    """The tile hints tell the UI which button (whole-tool vs "Always allow <host>")."""
    client.post("/api/account/setup", json={"passphrase": "correct-horse"})
    monkeypatch.setattr(gateway, "chat_with_tools",
                        _always_returns(_web_fetch_response("https://news.example.com/latest")))
    client.post("/api/agent/turn",
                json={"messages": [{"role": "user", "content": "fetch"}], "model": "m"})
    tile = client.get("/api/agent/pending").json()["pending"][0]
    assert tile["tool"] == "web_fetch"
    assert tile["remember_mode"] == "site"
    assert tile["remember_host"] == "news.example.com"
    assert tile["rememberable"] is True  # phone bundle sees the button
    # A whole-tool tool carries the "tool" mode and no host.
    client.post(f"/api/agent/pending/{tile['id']}/deny")
    monkeypatch.setattr(gateway, "chat_with_tools", _reviewed_tool_call)
    client.post("/api/agent/turn",
                json={"messages": [{"role": "user", "content": "note"}], "model": "m"})
    tile2 = client.get("/api/agent/pending").json()["pending"][0]
    assert tile2["remember_mode"] == "tool" and tile2["remember_host"] is None
    assert tile2["rememberable"] is True
