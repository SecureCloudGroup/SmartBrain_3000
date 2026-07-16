"""Subscribing to a PUBLIC vault by URL (Stage C): fetch, verify, re-seal — and PIN the publisher.

First contact IS the trust model: the publisher key seen at subscribe time is pinned in the new
vault's encrypted body, and every later update must verify against that pin — never against
whatever key a future download claims. These tests drive the full stack through the route; the
network fetch is monkeypatched to serve bytes from a REAL open export, so everything after the
socket — container checks, signature, pin resolution, re-seal, audit — is the shipped code path.
The hostile-URL test deliberately does NOT monkeypatch: the real netguard must refuse localhost.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Iterator

import duckdb
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import gateway, netguard, vault_format
from smartbrain_3000.secrets import SecretStore, gen_master_key
from smartbrain_3000.vaults import VaultStore

_PASS_A = "alice-correct-horse"
_PASS_B = "bob-correct-horse"
_LOCAL = {"x-sb-local": "1"}  # export is Desktop-local only (the WebRTC bridge cannot forward this)
_URL = "https://vaults.example.com/packs/expert-pack.sbvault"


def _app(tmp_path, monkeypatch, name: str, passphrase: str) -> TestClient:
    monkeypatch.setenv("SMARTBRAIN_DB_PATH", str(tmp_path / f"{name}.duckdb"))
    from smartbrain_3000.main import create_app

    client = TestClient(create_app())
    client.__enter__()
    client.post("/api/account/setup", json={"passphrase": passphrase})
    return client


@pytest.fixture()
def alice(tmp_path, monkeypatch) -> Iterator[TestClient]:
    c = _app(tmp_path, monkeypatch, "alice", _PASS_A)
    yield c
    c.__exit__(None, None, None)


@pytest.fixture()
def bob(tmp_path, monkeypatch) -> Iterator[TestClient]:
    c = _app(tmp_path, monkeypatch, "bob", _PASS_B)
    yield c
    c.__exit__(None, None, None)


def _make_vault(client: TestClient, docs: list[tuple[str, str]], name: str = "Expert pack") -> str:
    vid = client.post("/api/vaults", json={"name": name}).json()["id"]
    ids = [client.post("/api/kb", json={"title": t, "content": c}).json()["id"] for t, c in docs]
    client.post(f"/api/vaults/{vid}/documents", json={"doc_ids": ids})
    return vid


def _export(client: TestClient, vid: str, passphrase: str, mode: str = "open") -> bytes:
    r = client.post(f"/api/vaults/{vid}/export",
                    json={"passphrase": passphrase, "mode": mode}, headers=_LOCAL)
    assert r.status_code == 200, r.text
    return r.content


def _serve(monkeypatch, blob: bytes) -> list[str]:
    """Serve ``blob`` for any safe_fetch_vault call; return the URLs the route actually fetched."""
    fetched: list[str] = []

    def fake(url: str) -> bytes:
        fetched.append(url)
        return blob

    monkeypatch.setattr(netguard, "safe_fetch_vault", fake)
    return fetched


def _index_rows(blob: bytes) -> list[dict]:
    """The signed per-doc rows (uid/hash/obj) of an OPEN export — index.bin is raw JSON."""
    return json.loads(zipfile.ZipFile(io.BytesIO(blob)).read("index.bin"))["docs"]


_DOCS = [
    ("Regulations", "the QUOKKA clause governs all filings"),
    ("Guidance", "for a WOMBAT exemption, file form 12B"),
]


# --- the product promise: paste a URL, get a searchable vault, publisher pinned ------------------

def test_subscribe_end_to_end_pins_the_publisher(alice: TestClient, bob: TestClient, monkeypatch) -> None:
    vid = _make_vault(alice, _DOCS)
    blob = _export(alice, vid, _PASS_A)
    manifest = vault_format.read_manifest(blob)
    fetched = _serve(monkeypatch, blob)

    # The pasted URL carries a fragment: it must never reach the fetch, the pin, or the audit —
    # a sealed-share link keeps its key material there, so the rule is absolute.
    r = bob.post("/api/vaults/subscribe", json={"url": _URL + "#k=SECRETFRAG"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"] == 2 and body["duplicates"] == 0
    assert body["name"] == "Expert pack"  # the open manifest carries the topic
    assert body["publisher"] == vault_format.fingerprint(manifest["publisher"]["pubkey"])
    assert body["url_host"] == "vaults.example.com"
    assert fetched == [_URL], "the route must fetch the fragment-stripped URL, exactly once"

    # The documents landed RE-SEALED under Bob's master key and are searchable right away.
    hits = bob.get("/api/kb/search", params={"q": "quokka", "mode": "lexical"}).json()["results"]
    assert [h["title"] for h in hits] == ["Regulations"]
    doc = bob.get("/api/kb").json()["documents"][0]
    assert bob.get(f"/api/kb/{doc['id']}").json()["content"]

    # THE pin, in the vault's encrypted body: url (no fragment), publisher key, vault_id, seq.
    vaults = bob.get("/api/vaults").json()["vaults"]
    assert len(vaults) == 1 and vaults[0]["kind"] == "imported"
    pin = vaults[0]["source"]
    assert pin["url"] == _URL and "#" not in pin["url"]
    assert pin["publisher_pubkey"] == manifest["publisher"]["pubkey"]
    assert pin["vault_id"] == manifest["vault_id"] and pin["seq"] == manifest["seq"]
    assert pin["mode"] == "open" and pin["last_checked"] is None and pin["added_at"]
    # The card's identity: the PINNED fingerprint rides the list response (never a badge without it).
    assert vaults[0]["pinned_fingerprint"] == body["publisher"]

    # Member provenance (migration 23): every landed doc records the upstream {uid, hash} — the
    # exact SIGNED hash from the index, keyed by uid, origin import.
    store: VaultStore = bob.app.state.vaults
    mm = store.member_map(vaults[0]["id"])
    expected = {row["uid"]: row["hash"] for row in _index_rows(blob)}
    assert {uid: m["hash"] for uid, m in mm.items()} == expected
    assert all(m["origin"] == "import" for m in mm.values())

    # Subscribed documents get the same protections as file-imported ones (C0): read-only.
    member_ids = {m["doc_id"] for m in mm.values()}
    assert bob.patch(f"/api/kb/{next(iter(member_ids))}", json={"title": "Renamed"}).status_code == 409

    # Ingress is audited: host only — never the full URL (its path names the topic).
    rows = [e for e in bob.get("/api/audit").json()["entries"] if e["tool"] == "vault_subscribe"]
    assert len(rows) == 1
    row = rows[0]
    assert row["actor"] == "user" and row["ok"] is True
    # Parse the summary and compare the host field exactly — a substring check on a URL-ish
    # value reads as incomplete sanitization to scanners, and equality is the stronger assert.
    logged = json.loads(row["args_summary"])
    assert logged["host"] == "vaults.example.com" and body["publisher"] in row["args_summary"]
    assert "expert-pack" not in row["args_summary"], "the URL path must never reach the audit log"
    assert "SECRETFRAG" not in row["args_summary"]
    assert json.loads(row["result_summary"])["added"] == 2


def test_shipped_vectors_make_a_subscription_instantly_searchable(alice, bob, monkeypatch) -> None:
    # Same gate as file import: adopt shipped vectors only when model+chunking match exactly.
    monkeypatch.setattr(gateway, "embed", lambda *a, **k: [1.0, 0.0, 0.0])
    vid = _make_vault(alice, [("Doc", "alpha content")])
    alice.post("/api/kb/reindex")  # give Alice's document real vectors to ship
    _serve(monkeypatch, _export(alice, vid, _PASS_A))

    body = bob.post("/api/vaults/subscribe", json={"url": _URL}).json()
    assert body["vectors_used"] is True
    assert bob.get("/api/kb/index-status").json()["pending"] == 0, "no re-embedding needed"


# --- refusals: everything that must NOT land ------------------------------------------------------

def test_a_sealed_vault_url_is_refused_with_directions(alice: TestClient, bob: TestClient, monkeypatch) -> None:
    # v1 URL-subscribe is open-only: a sealed vault's key must never ride a URL, so the answer is
    # a clear pointer at the file+key path — not a crypto error.
    vid = _make_vault(alice, [("Doc", "sealed body")])
    _serve(monkeypatch, _export(alice, vid, _PASS_A, mode="sealed"))

    r = bob.post("/api/vaults/subscribe", json={"url": _URL})
    assert r.status_code == 400
    assert "sealed" in r.json()["detail"] and "key" in r.json()["detail"]
    assert bob.get("/api/vaults").json()["vaults"] == [], "nothing may land from a refused URL"
    assert bob.get("/api/kb").json()["documents"] == []


def test_subscribing_twice_is_a_conflict_not_a_second_copy(alice: TestClient, bob: TestClient, monkeypatch) -> None:
    vid = _make_vault(alice, _DOCS)
    _serve(monkeypatch, _export(alice, vid, _PASS_A))
    assert bob.post("/api/vaults/subscribe", json={"url": _URL}).status_code == 200

    r = bob.post("/api/vaults/subscribe", json={"url": _URL})
    assert r.status_code == 409
    assert "already" in r.json()["detail"] and "update" in r.json()["detail"]
    assert len(bob.get("/api/vaults").json()["vaults"]) == 1, "no second copy"


def test_same_vault_id_from_a_different_publisher_is_refused(alice: TestClient, bob: TestClient, monkeypatch) -> None:
    # The TOFU pin doing its job: a hostile host (or a stranger) republishing the SAME vault_id
    # under a DIFFERENT key must be refused outright — accepting it would let anyone hijack the
    # identity of a vault the user already trusts.
    vid = _make_vault(alice, _DOCS)
    blob = _export(alice, vid, _PASS_A)
    _serve(monkeypatch, blob)
    assert bob.post("/api/vaults/subscribe", json={"url": _URL}).status_code == 200

    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    attacker = SecretStore(conn, gen_master_key())  # a different publisher Ed25519 key
    forged = vault_format.pack(
        store=attacker, vault_id=vault_format.read_manifest(blob)["vault_id"],
        name="Expert pack", description="", seq=99, mode=vault_format.OPEN,
        name_key=gen_master_key(),
        docs=[{"uid": "evil-1", "title": "Poison", "content": "malicious REPLACEMENT text",
               "meta": {}, "chunks": 1}],
    )
    _serve(monkeypatch, forged)

    docs_before = bob.get("/api/kb").json()["documents"]
    r = bob.post("/api/vaults/subscribe", json={"url": "https://elsewhere.example.org/pack.sbvault"})
    assert r.status_code == 409 and "different publisher" in r.json()["detail"]
    assert len(bob.get("/api/vaults").json()["vaults"]) == 1
    assert bob.get("/api/kb").json()["documents"] == docs_before, "nothing may land from a refusal"


def test_localhost_is_refused_by_the_real_netguard(bob: TestClient) -> None:
    # NO monkeypatch: the request must die inside the real SSRF guard — subscribing can never be
    # used to make the Desktop fetch from itself or the LAN — and the refusal must say so in plain
    # words, not "cannot resolve host".
    r = bob.post("/api/vaults/subscribe", json={"url": "http://localhost:33000/api/backup"})
    assert r.status_code == 400
    assert "public internet" in r.json()["detail"]
    assert bob.get("/api/vaults").json()["vaults"] == []


def test_a_dead_host_is_a_clean_refusal_not_a_500(bob: TestClient, monkeypatch) -> None:
    # Transport failures (refused, timeout, TLS) are ordinary fates for a user-typed URL. They must
    # surface as a clean 400 — an unhandled exception would 500 AND put the full URL in a log line,
    # which the host-only hygiene rule exists to prevent. Resolution is pinned to a public address
    # (deterministic offline) and the failure is injected at the connect seam, so the real netguard
    # path up to the socket runs.
    import socket

    import httpx

    def refuse(*_a, **_k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
    monkeypatch.setattr(netguard, "_send_pinned", refuse)
    r = bob.post("/api/vaults/subscribe", json={"url": "https://vaults.example.com/pack.sbvault"})
    assert r.status_code == 400
    assert "could not fetch" in r.json()["detail"]
    assert "pack.sbvault" not in r.json()["detail"], "the URL must not echo into the error"


def test_garbage_bytes_are_refused_cleanly(bob: TestClient, monkeypatch) -> None:
    # A URL that serves anything but a vault must be a clean 400 (the #72 container guards), never
    # an unhandled 500 — this is the not-a-vault-URL error the UI shows inline.
    _serve(monkeypatch, b"<!doctype html><html>this is not a vault</html>")
    r = bob.post("/api/vaults/subscribe", json={"url": _URL})
    assert r.status_code == 400 and "not a vault" in r.json()["detail"]
    assert bob.get("/api/vaults").json()["vaults"] == []
    # A whitespace- or fragment-only "URL" must be a clean 400 too, not an internal assert.
    assert bob.post("/api/vaults/subscribe", json={"url": "  #k=frag "}).status_code == 400


# --- file import keeps parity: same member provenance, same map -----------------------------------

def test_file_import_also_records_member_provenance(alice: TestClient, bob: TestClient) -> None:
    # Both ingress paths feed the SAME future update mechanism, so a file import must write the
    # same {uid, hash} member bodies a subscribe does — including on the duplicate path, where the
    # user's own copy is kept: its uid must map as owner-origin so an update knows to skip it.
    bob.post("/api/kb", json={"title": "My own notes", "content": _DOCS[0][1]})
    vid = _make_vault(alice, _DOCS)
    blob = _export(alice, vid, _PASS_A, mode="sealed")
    key = alice.post(f"/api/vaults/{vid}/key",
                     json={"passphrase": _PASS_A}, headers=_LOCAL).json()["key"]

    body = bob.post(f"/api/vaults/import?key={key}", content=blob).json()
    assert body["added"] == 1 and body["duplicates"] == 1

    store: VaultStore = bob.app.state.vaults
    mm = store.member_map(body["id"])
    # Every vault uid is mapped — the sealed index is encrypted, so recover uids via open_vault.
    _, docs = vault_format.open_vault(blob, vault_format.decode_vault_key(key))
    assert set(mm) == {d["uid"] for d in docs}
    assert {m["hash"] for m in mm.values()} == {d["hash"] for d in docs}
    origins = sorted(m["origin"] for m in mm.values())
    assert origins == ["import", "owner"], "the kept-yours duplicate maps as owner (update: skip)"


def test_member_map_skips_rows_the_user_added_themselves(bob: TestClient) -> None:
    # An owner-added membership has no upstream source (NULL body columns): it is invisible to the
    # update map but fully present in the member list — not the publisher's to touch, not an error.
    vid = _make_vault(bob, [("Mine", "my own document")])
    store: VaultStore = bob.app.state.vaults
    assert store.member_map(vid) == {}
    assert len(store.members(vid)) == 1


# --- migration 23 on a populated database ----------------------------------------------------------

def test_migration_23_preserves_membership_rows(tmp_path) -> None:
    # Upgrade safety: rows written before the provenance columns existed survive with NULL bodies,
    # member_map skips them, and the new columns are immediately usable on the same rows.
    conn = dbmod.open_db(tmp_path / "pre23.duckdb")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(id INTEGER PRIMARY KEY, applied_at TIMESTAMP DEFAULT current_timestamp);"
    )
    for migration_id, sql in dbmod._MIGRATIONS:  # fabricate a DB at migration 22
        if migration_id > 22:
            break
        conn.execute(sql)
        conn.execute("INSERT INTO schema_migrations (id) VALUES (?);", [migration_id])

    store = VaultStore(conn, gen_master_key())
    vid = store.create("Field notes")
    conn.execute(  # a membership recorded the pre-23 way (no nonce/ciphertext in the INSERT)
        "INSERT INTO vault_documents (vault_id, doc_id, origin) VALUES (?, ?, 'owner');",
        [vid, "doc-1"],
    )

    assert dbmod.run_migrations(conn) == 1  # exactly migration 23
    row = conn.execute(
        "SELECT doc_id, origin, nonce, ciphertext FROM vault_documents;").fetchone()
    assert str(row[0]) == "doc-1" and str(row[1]) == "owner"
    assert row[2] is None and row[3] is None, "pre-existing rows keep NULL bodies"
    assert store.member_map(vid) == {}  # NULL-body rows are skipped, not errors

    # The upgraded row can take provenance and round-trip it through the encrypted body.
    store.note_member_source(vid, "doc-1", "u1", "h" * 64)
    assert store.member_map(vid) == {"u1": {"doc_id": "doc-1", "hash": "h" * 64, "origin": "owner"}}
    assert dbmod.run_migrations(conn) == 0  # idempotent
    conn.close()
