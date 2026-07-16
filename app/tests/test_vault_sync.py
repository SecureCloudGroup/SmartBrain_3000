"""Stage D: check-for-updates + apply — a subscription that keeps itself current, safely.

The pin (publisher key, vault_id, seq floor) was set at subscribe time; every test here is about
ENFORCING it against a host that may be honest, stale, or hostile. The network fetchers are
monkeypatched to serve bytes from REAL ``pack(mode=open)`` exports across seq bumps, so everything
after the socket — verification order, the tree delta, the diff, kb.replace, the transaction, the
pin re-write, the audit — is the shipped code path.

The invariants under test:
  * an update applies IN PLACE: the local doc_id survives, so citations and deep links do;
  * §5 order and the pin's authority: rollbacks refused, key changes block and show both
    fingerprints, and nothing a hostile host serves is ever half-applied;
  * a tree host transfers only what changed; a zip host is refetched whole (the honest v1 cost);
  * anything the user edited stays theirs (kept_yours, origin flip).
"""

from __future__ import annotations

import io
import json
import os
import zipfile
from collections.abc import Iterator

import duckdb
import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import netguard, vault_format, vault_sync
from smartbrain_3000.secrets import SecretStore, gen_master_key
from smartbrain_3000.vaults import VaultStore

_PASS_A = "alice-correct-horse"
_PASS_B = "bob-correct-horse"
_LOCAL = {"x-sb-local": "1"}  # export/trust-publisher are Desktop-local only
_ZIP_URL = "https://vaults.example.com/packs/expert-pack.sbvault"
_TREE_BASE = "https://static.example.net/vault/"
_TREE_URL = _TREE_BASE + "manifest.json"


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


def _make_vault(client: TestClient, docs: list[tuple[str, str]], name: str = "Expert pack",
                ) -> tuple[str, list[str]]:
    vid = client.post("/api/vaults", json={"name": name}).json()["id"]
    ids = [client.post("/api/kb", json={"title": t, "content": c}).json()["id"] for t, c in docs]
    client.post(f"/api/vaults/{vid}/documents", json={"doc_ids": ids})
    return vid, ids


def _export(client: TestClient, vid: str, passphrase: str, mode: str = "open") -> bytes:
    r = client.post(f"/api/vaults/{vid}/export",
                    json={"passphrase": passphrase, "mode": mode}, headers=_LOCAL)
    assert r.status_code == 200, r.text
    return r.content


def _serve(monkeypatch, blob: bytes) -> list[str]:
    """Serve ``blob`` as a single-file (zip) host; return the URLs actually fetched."""
    fetched: list[str] = []

    def fake(url: str) -> bytes:
        fetched.append(url)
        return blob

    monkeypatch.setattr(netguard, "safe_fetch_vault", fake)
    return fetched


def _serve_tree(monkeypatch, blob: bytes, tampered: dict[str, bytes] | None = None) -> list[str]:
    """Serve ``blob``'s entries as a static TREE host (manifest / index / objects as separate
    files); return the URLs actually fetched. ``tampered`` swaps named entries' bytes — the
    compromised-host case. The zip fetcher is booby-trapped: a tree pin must never use it."""
    zf = zipfile.ZipFile(io.BytesIO(blob))
    entries = {n: zf.read(n) for n in zf.namelist()}
    fetched: list[str] = []

    def manifest(url: str) -> bytes:
        assert url == _TREE_URL, f"unexpected manifest fetch: {url}"
        fetched.append(url)
        return (tampered or {}).get("manifest.json", entries["manifest.json"])

    def obj(url: str, max_bytes: int) -> bytes:
        assert url.startswith(_TREE_BASE), f"unexpected object fetch: {url}"
        assert max_bytes > 0
        fetched.append(url)
        name = url[len(_TREE_BASE):]
        return (tampered or {}).get(name, entries[name])

    def no_zip(url: str) -> bytes:
        raise AssertionError("a tree host must never be asked for the whole zip")

    monkeypatch.setattr(netguard, "safe_fetch_vault_manifest", manifest)
    monkeypatch.setattr(netguard, "safe_fetch_vault_object", obj)
    monkeypatch.setattr(netguard, "safe_fetch_vault", no_zip)
    return fetched


def _index_rows(blob: bytes) -> list[dict]:
    """The signed per-doc rows (uid/title/hash/obj) of an OPEN export — index.bin is raw JSON."""
    return json.loads(zipfile.ZipFile(io.BytesIO(blob)).read("index.bin"))["docs"]


def _row(blob: bytes, title: str) -> dict:
    return next(r for r in _index_rows(blob) if r["title"] == title)


def _pin(client: TestClient, vault_id: str) -> dict:
    vault = next(v for v in client.get("/api/vaults").json()["vaults"] if v["id"] == vault_id)
    return vault


def _forge(vault_id: str, seq: int, docs: list[dict]) -> tuple[bytes, str]:
    """A self-consistent HOSTILE vault: same vault_id, valid signature — by a NEW key.

    Returns (blob, that key's pubkey). This is exactly what a bucket takeover looks like.
    """
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    attacker = SecretStore(conn, gen_master_key())
    blob = vault_format.pack(
        store=attacker, vault_id=vault_id, name="Expert pack", description="", seq=seq,
        mode=vault_format.OPEN, name_key=gen_master_key(), docs=docs)
    return blob, vault_format.read_manifest(blob)["publisher"]["pubkey"]


_DOCS = [
    ("Regulations", "the QUOKKA clause governs all filings"),
    ("Guidance", "for a WOMBAT exemption, file form 12B"),
    ("Appendix", "the NUMBAT schedule lists all fees"),
]


def _subscribed(alice: TestClient, bob: TestClient, monkeypatch, *, url: str = _ZIP_URL,
                docs: list[tuple[str, str]] = _DOCS) -> tuple[str, list[str], str, bytes]:
    """Alice publishes; Bob subscribes. Returns (alice vid, alice doc ids, bob vault id, blob)."""
    vid, ids = _make_vault(alice, docs)
    blob = _export(alice, vid, _PASS_A)
    if url == _TREE_URL:
        _serve_tree(monkeypatch, blob)
    else:
        _serve(monkeypatch, blob)
    r = bob.post("/api/vaults/subscribe", json={"url": url})
    assert r.status_code == 200, r.text
    return vid, ids, r.json()["id"], blob


# --- the product promise: an update lands in place, all-or-nothing --------------------------------

def test_update_applies_in_place_and_repins_seq(alice: TestClient, bob: TestClient, monkeypatch) -> None:
    # The publisher edits one doc, deletes one, adds one, republishes. The subscriber's edited doc
    # keeps its LOCAL id (citations/deep links survive — the whole point of kb.replace), the
    # deleted uid's doc goes away, the new uid lands, and the pin's seq floor moves.
    vid, ids, local_id, blob1 = _subscribed(alice, bob, monkeypatch)
    store: VaultStore = bob.app.state.vaults
    uid_b = _row(blob1, "Guidance")["uid"]
    uid_c = _row(blob1, "Appendix")["uid"]
    doc_b_before = store.member_map(local_id)[uid_b]["doc_id"]
    doc_c_before = store.member_map(local_id)[uid_c]["doc_id"]

    alice.app.state.kb.replace(ids[1], "Guidance", "for a WOMBAT exemption, file form 99X", {})
    alice.delete(f"/api/vaults/{vid}/documents/{ids[2]}")  # Appendix leaves the vault
    new_id = alice.post("/api/kb", json={"title": "Bulletin", "content": "new KOALA rules"}).json()["id"]
    alice.post(f"/api/vaults/{vid}/documents", json={"doc_ids": [new_id]})
    blob2 = _export(alice, vid, _PASS_A)
    _serve(monkeypatch, blob2)

    r = bob.post(f"/api/vaults/{local_id}/check-updates")
    assert r.status_code == 200, r.text
    assert r.json() == {"behind": True, "remote_seq": 3, "seq": 2, "rollback": False}

    r = bob.post(f"/api/vaults/{local_id}/update")
    assert r.status_code == 200, r.text
    assert r.json() == {"added": 1, "updated": 1, "deleted": 1, "kept_yours": 0, "seq": 3}

    mm = store.member_map(local_id)
    assert mm[uid_b]["doc_id"] == doc_b_before, "an update must NEVER change the local doc id"
    assert mm[uid_b]["hash"] == _row(blob2, "Guidance")["hash"], "the member hash re-pins too"
    assert uid_c not in mm
    assert bob.get(f"/api/kb/{doc_b_before}").json()["content"].endswith("form 99X")
    assert bob.get(f"/api/kb/{doc_c_before}").status_code == 404, "deleted upstream, deleted here"
    titles = {d["title"] for d in bob.get("/api/kb").json()["documents"]}
    assert titles == {"Regulations", "Guidance", "Bulletin"}

    pin = _pin(bob, local_id)["source"]
    assert pin["seq"] == 3 and pin["last_checked"], "the seq floor and last_checked moved together"

    # Ingress is audited: host only — never the URL path (it names the topic).
    rows = [e for e in bob.get("/api/audit").json()["entries"] if e["tool"] == "vault_update"]
    assert len(rows) == 1
    assert json.loads(rows[0]["args_summary"])["host"] == "vaults.example.com"
    assert "expert-pack" not in rows[0]["args_summary"]
    assert json.loads(rows[0]["result_summary"]) == {"added": 1, "updated": 1, "deleted": 1,
                                                     "kept_yours": 0}


def test_same_seq_is_up_to_date_and_a_no_op(alice: TestClient, bob: TestClient, monkeypatch) -> None:
    _vid, _ids, local_id, blob = _subscribed(alice, bob, monkeypatch)
    docs_before = bob.get("/api/kb").json()["documents"]

    r = bob.post(f"/api/vaults/{local_id}/check-updates")
    assert r.status_code == 200 and r.json()["behind"] is False and r.json()["rollback"] is False
    assert _pin(bob, local_id)["source"]["last_checked"], "a check records when it happened"

    r = bob.post(f"/api/vaults/{local_id}/update")
    assert r.status_code == 200
    assert r.json() == {"added": 0, "updated": 0, "deleted": 0, "kept_yours": 0, "seq": 2}
    assert bob.get("/api/kb").json()["documents"] == docs_before


def test_rollback_is_refused_and_nothing_changes(alice: TestClient, bob: TestClient, monkeypatch) -> None:
    # A validly-SIGNED older file is exactly what a frozen/rolled-back host serves. seq is signed,
    # so the pin's floor wins: refuse, apply nothing.
    vid, ids, local_id, blob1 = _subscribed(alice, bob, monkeypatch)
    alice.app.state.kb.replace(ids[0], "Regulations", "amended QUOKKA clause", {})
    blob2 = _export(alice, vid, _PASS_A)
    _serve(monkeypatch, blob2)
    assert bob.post(f"/api/vaults/{local_id}/update").json()["seq"] == 3

    _serve(monkeypatch, blob1)  # the host regresses to the seq-2 file
    r = bob.post(f"/api/vaults/{local_id}/check-updates")
    assert r.status_code == 200
    assert r.json()["rollback"] is True and r.json()["behind"] is False

    docs_before = {d["id"] for d in bob.get("/api/kb").json()["documents"]}
    r = bob.post(f"/api/vaults/{local_id}/update")
    assert r.status_code == 409 and "roll back" in r.json()["detail"]
    assert _pin(bob, local_id)["source"]["seq"] == 3, "the pin must not regress"
    assert {d["id"] for d in bob.get("/api/kb").json()["documents"]} == docs_before


def test_non_subscribed_vaults_get_a_400_on_all_three(bob: TestClient) -> None:
    vid, _ids = _make_vault(bob, [("Mine", "my own text")])
    for path in (f"/api/vaults/{vid}/check-updates", f"/api/vaults/{vid}/update"):
        r = bob.post(path)
        assert r.status_code == 400 and "not a URL subscription" in r.json()["detail"], path
    r = bob.post(f"/api/vaults/{vid}/trust-publisher", headers=_LOCAL,
                 json={"passphrase": _PASS_B, "offered_pubkey": "AAAA"})
    assert r.status_code == 400 and "not a URL subscription" in r.json()["detail"]
    assert bob.post("/api/vaults/nope/check-updates").status_code == 404


# --- the tree host: only what changed crosses the wire --------------------------------------------

def test_tree_host_subscribe_then_delta_update_fetches_only_changed_objects(
        alice: TestClient, bob: TestClient, monkeypatch) -> None:
    vid, ids, local_id, blob1 = _subscribed(alice, bob, monkeypatch, url=_TREE_URL)
    store: VaultStore = bob.app.state.vaults
    assert len(store.member_map(local_id)) == 3, "the tree subscribe landed everything"

    alice.app.state.kb.replace(ids[1], "Guidance", "for a WOMBAT exemption, file form 99X", {})
    blob2 = _export(alice, vid, _PASS_A)
    fetched = _serve_tree(monkeypatch, blob2)

    # A bare check reads ONLY the manifest — that is the tree host's entire point.
    assert bob.post(f"/api/vaults/{local_id}/check-updates").json()["behind"] is True
    assert fetched == [_TREE_URL]

    fetched.clear()
    r = bob.post(f"/api/vaults/{local_id}/update")
    assert r.status_code == 200, r.text
    assert r.json() == {"added": 0, "updated": 1, "deleted": 0, "kept_yours": 0, "seq": 3}
    changed_obj = _row(blob2, "Guidance")["obj"]
    assert fetched == [_TREE_URL, _TREE_BASE + "index.bin",
                       _TREE_BASE + f"objects/{changed_obj}.bin"], \
        "unchanged objects must never be downloaded"
    doc_id = store.member_map(local_id)[_row(blob2, "Guidance")["uid"]]["doc_id"]
    assert bob.get(f"/api/kb/{doc_id}").json()["content"].endswith("form 99X")


def test_tampered_tree_object_aborts_the_whole_update(alice: TestClient, bob: TestClient,
                                                      monkeypatch) -> None:
    # A compromised host swaps the CHANGED object's bytes (the name is legitimate — only the hash
    # chain catches it). The whole update must abort: docs, member map, and pin all stay on the
    # old seq. Half-applied is the one forbidden state.
    vid, ids, local_id, blob1 = _subscribed(alice, bob, monkeypatch, url=_TREE_URL)
    store: VaultStore = bob.app.state.vaults
    mm_before = store.member_map(local_id)
    docs_before = {d["id"]: bob.get(f"/api/kb/{d['id']}").json()["content"]
                   for d in bob.get("/api/kb").json()["documents"]}

    alice.app.state.kb.replace(ids[1], "Guidance", "for a WOMBAT exemption, file form 99X", {})
    blob2 = _export(alice, vid, _PASS_A)
    changed_obj = _row(blob2, "Guidance")["obj"]
    _serve_tree(monkeypatch, blob2,
                tampered={f"objects/{changed_obj}.bin": b"poisoned bytes, right length irrelevant"})

    r = bob.post(f"/api/vaults/{local_id}/update")
    assert r.status_code == 400 and "signed hash" in r.json()["detail"]

    assert store.member_map(local_id) == mm_before, "member provenance untouched"
    assert _pin(bob, local_id)["source"]["seq"] == 2, "pin untouched"
    for doc_id, content in docs_before.items():
        assert bob.get(f"/api/kb/{doc_id}").json()["content"] == content, "documents untouched"


def test_a_slow_tree_host_trips_the_update_time_budget_and_applies_nothing(
        alice: TestClient, bob: TestClient, monkeypatch) -> None:
    # A tree host can serve each object just under its 8s timeout while every object stays under the
    # byte cap — an update would run for hours. The wall-clock budget stops it BETWEEN fetches: a
    # clean SyncError (400), and because planning precedes the write transaction, nothing lands and
    # the pin holds. The clock is injected so the deadline trips deterministically, with no waiting.
    vid, ids, local_id, blob1 = _subscribed(alice, bob, monkeypatch, url=_TREE_URL)
    store: VaultStore = bob.app.state.vaults
    mm_before = store.member_map(local_id)
    docs_before = {d["id"]: bob.get(f"/api/kb/{d['id']}").json()["content"]
                   for d in bob.get("/api/kb").json()["documents"]}

    for i, did in enumerate(ids):  # the publisher changes every doc, so the update fetches several
        alice.app.state.kb.replace(did, _DOCS[i][0], _DOCS[i][1] + " (revised)", {})
    _serve_tree(monkeypatch, _export(alice, vid, _PASS_A))

    # The deadline is set on the first _monotonic() call; from the second check on we are past it, so
    # the budget trips after exactly one object is fetched.
    calls = {"n": 0}

    def fake_clock() -> float:
        calls["n"] += 1
        return 0.0 if calls["n"] <= 2 else float(vault_sync._MAX_UPDATE_SECONDS) + 10_000.0

    monkeypatch.setattr(vault_sync, "_monotonic", fake_clock)

    r = bob.post(f"/api/vaults/{local_id}/update")
    assert r.status_code == 400 and "took too long" in r.json()["detail"]
    assert store.member_map(local_id) == mm_before, "nothing applied — member provenance untouched"
    assert _pin(bob, local_id)["source"]["seq"] == 2, "pin unchanged"
    for doc_id, content in docs_before.items():
        assert bob.get(f"/api/kb/{doc_id}").json()["content"] == content, "documents untouched"


# --- the pin's authority: key substitution, blocking, and trusting a new key -----------------------

def test_key_substitution_blocks_and_trust_publisher_unblocks(alice: TestClient, bob: TestClient,
                                                              monkeypatch) -> None:
    _vid, _ids, local_id, blob = _subscribed(alice, bob, monkeypatch)
    pinned_pub = vault_format.read_manifest(blob)["publisher"]["pubkey"]
    vault_id = vault_format.read_manifest(blob)["vault_id"]
    forged, evil_pub = _forge(vault_id, 99, [
        {"uid": "evil-1", "title": "Poison", "content": "malicious REPLACEMENT text",
         "meta": {}, "chunks": 1}])
    fetched = _serve(monkeypatch, forged)
    docs_before = bob.get("/api/kb").json()["documents"]

    # A self-consistent hostile manifest is a KEY CHANGE, not a crypto error: 409, BOTH
    # fingerprints, nothing applied, and the subscription is blocked.
    r = bob.post(f"/api/vaults/{local_id}/check-updates")
    assert r.status_code == 409
    pinned_fp, offered_fp = vault_format.fingerprint(pinned_pub), vault_format.fingerprint(evil_pub)
    assert pinned_fp in r.json()["detail"] and offered_fp in r.json()["detail"]
    assert bob.get("/api/kb").json()["documents"] == docs_before
    vault = _pin(bob, local_id)
    assert vault["source"]["blocked"] == {"offered_pubkey": evil_pub}
    assert vault["blocked_fingerprint"] == offered_fp and vault["pinned_fingerprint"] == pinned_fp

    # Blocked persists — and short-circuits: no fetch happens until a human decides.
    fetch_count = len(fetched)
    for path in (f"/api/vaults/{local_id}/check-updates", f"/api/vaults/{local_id}/update"):
        r = bob.post(path)
        assert r.status_code == 409 and pinned_fp in r.json()["detail"]
    assert len(fetched) == fetch_count, "a blocked subscription must not keep fetching"

    # Trusting the new key is the most consequential act in the system, so it gates like export.
    ok_body = {"passphrase": _PASS_B, "offered_pubkey": evil_pub}
    assert bob.post(f"/api/vaults/{local_id}/trust-publisher", json=ok_body).status_code == 403
    assert bob.post(f"/api/vaults/{local_id}/trust-publisher", headers=_LOCAL,
                    json={"passphrase": "wrong", "offered_pubkey": evil_pub}).status_code == 401
    r = bob.post(f"/api/vaults/{local_id}/trust-publisher", headers=_LOCAL,
                 json={"passphrase": _PASS_B, "offered_pubkey": pinned_pub})
    assert r.status_code == 409, "a body naming any key but the blocked one is refused"
    assert _pin(bob, local_id)["source"]["blocked"], "still blocked after every refusal"

    r = bob.post(f"/api/vaults/{local_id}/trust-publisher", headers=_LOCAL, json=ok_body)
    assert r.status_code == 200 and r.json()["pinned_fingerprint"] == offered_fp
    pin = _pin(bob, local_id)["source"]
    assert pin["publisher_pubkey"] == evil_pub and "blocked" not in pin

    # Re-pinned: the very update that was refused now verifies and applies.
    r = bob.post(f"/api/vaults/{local_id}/update")
    assert r.status_code == 200, r.text
    assert r.json() == {"added": 1, "updated": 0, "deleted": 3, "kept_yours": 0, "seq": 99}
    titles = {d["title"] for d in bob.get("/api/kb").json()["documents"]}
    assert titles == {"Poison"}


def test_trust_publisher_refuses_a_stale_offered_key(alice: TestClient, bob: TestClient,
                                                     monkeypatch) -> None:
    # The race the body-echo exists for: the user confirmed key B out-of-band, but the host has
    # rotated AGAIN (to C) and a new check re-blocked on C. The stale confirmation of B must not
    # bless C — and must not re-pin B either, because B never proved anything.
    _vid, _ids, local_id, blob = _subscribed(alice, bob, monkeypatch)
    vault_id = vault_format.read_manifest(blob)["vault_id"]
    pinned_pub = vault_format.read_manifest(blob)["publisher"]["pubkey"]
    doc = [{"uid": "evil-1", "title": "Poison", "content": "x", "meta": {}, "chunks": 1}]
    forged_b, pub_b = _forge(vault_id, 99, doc)
    forged_c, pub_c = _forge(vault_id, 100, doc)

    _serve(monkeypatch, forged_b)
    assert bob.post(f"/api/vaults/{local_id}/check-updates").status_code == 409  # blocked on B
    assert bob.post(f"/api/vaults/{local_id}/trust-publisher", headers=_LOCAL,
                    json={"passphrase": _PASS_B, "offered_pubkey": pub_b}).status_code == 200
    _serve(monkeypatch, forged_c)  # rotated again
    assert bob.post(f"/api/vaults/{local_id}/check-updates").status_code == 409  # re-blocked on C

    r = bob.post(f"/api/vaults/{local_id}/trust-publisher", headers=_LOCAL,
                 json={"passphrase": _PASS_B, "offered_pubkey": pub_b})
    assert r.status_code == 409 and "changed since you confirmed" in r.json()["detail"]
    pin = _pin(bob, local_id)["source"]
    assert pin["publisher_pubkey"] == pub_b, "the stale confirmation moved nothing"
    assert pin["blocked"] == {"offered_pubkey": pub_c}
    assert pinned_pub != pub_b != pub_c


# --- the user's own copies stay theirs -------------------------------------------------------------

def test_owner_edited_doc_is_kept_and_flipped_not_clobbered(alice: TestClient, bob: TestClient,
                                                            monkeypatch) -> None:
    # Bob's copy of "Guidance" was edited locally (via the store — the API's C0 guard refuses,
    # which is exactly why an edit like this can only predate that guard or come via restore).
    # Upstream then edits the same doc. Decision #1: the local hash no longer matches the pinned
    # one, so the doc is the USER's now — skipped, origin flipped, reported. The rest applies.
    vid, ids, local_id, blob1 = _subscribed(alice, bob, monkeypatch)
    store: VaultStore = bob.app.state.vaults
    uid_b = _row(blob1, "Guidance")["uid"]
    doc_b = store.member_map(local_id)[uid_b]["doc_id"]
    bob.app.state.kb.replace(doc_b, "Guidance", "MY OWN careful annotations", {})

    alice.app.state.kb.replace(ids[1], "Guidance", "publisher's new guidance", {})
    alice.app.state.kb.replace(ids[0], "Regulations", "amended QUOKKA clause", {})
    _serve(monkeypatch, _export(alice, vid, _PASS_A))

    r = bob.post(f"/api/vaults/{local_id}/update")
    assert r.status_code == 200, r.text
    assert r.json() == {"added": 0, "updated": 1, "deleted": 0, "kept_yours": 1, "seq": 3}
    assert bob.get(f"/api/kb/{doc_b}").json()["content"] == "MY OWN careful annotations"
    assert store.origin_of(local_id, doc_b) == "owner", "the flip makes every future update skip it"
    # And the origin flip means the NEXT update counts it kept without even fetching a hash match.
    _serve(monkeypatch, _export(alice, vid, _PASS_A))
    assert bob.post(f"/api/vaults/{local_id}/update").json()["kept_yours"] == 1


def test_a_doc_that_normalizes_on_landing_updates_instead_of_false_detaching(
        alice: TestClient, bob: TestClient, monkeypatch) -> None:
    # THE Stage-D correctness bug. The publisher's SIGNED hash covers the RAW source_url, but landing
    # clips it to 2048 (kb stores the normalized copy). Compared against the signed hash, this
    # untouched doc would be judged "edited" on its very first real update — skipped and detached,
    # silently killing every future update for it. landed_hash records what we actually landed, so it
    # updates normally. Realistic trigger: a genuinely long ingested source_url.
    long_url = "https://example.com/s?q=" + "a" * 3000  # > 2048 -> _clean_meta clips it on landing
    vid = alice.post("/api/vaults", json={"name": "Expert pack"}).json()["id"]
    doc_id = alice.app.state.kb.add(
        "Regulations", "the QUOKKA clause governs all filings", {"source_url": long_url})
    alice.post(f"/api/vaults/{vid}/documents", json={"doc_ids": [doc_id]})
    blob1 = _export(alice, vid, _PASS_A)
    _serve(monkeypatch, blob1)
    local_id = bob.post("/api/vaults/subscribe", json={"url": _ZIP_URL}).json()["id"]

    store: VaultStore = bob.app.state.vaults
    uid = _row(blob1, "Regulations")["uid"]
    member = store.member_map(local_id)[uid]
    assert len(bob.get(f"/api/kb/{member['doc_id']}").json()["meta"]["source_url"]) == 2048, \
        "the url really was normalized on landing"
    assert member["landed_hash"] != member["hash"], \
        "landed (normalized) and signed (raw) hashes legitimately differ — the whole trap"

    # The publisher edits the CONTENT (bumping the signed hash) but Bob NEVER touched his copy.
    alice.app.state.kb.replace(
        doc_id, "Regulations", "the QUOKKA clause was AMENDED", {"source_url": long_url})
    _serve(monkeypatch, _export(alice, vid, _PASS_A))
    r = bob.post(f"/api/vaults/{local_id}/update")
    assert r.status_code == 200, r.text
    assert r.json()["updated"] == 1 and r.json()["kept_yours"] == 0, "updated, NOT false-detached"
    assert bob.get(f"/api/kb/{member['doc_id']}").json()["content"].endswith("AMENDED")
    assert store.origin_of(local_id, member["doc_id"]) == "import", "still the publisher's to update"


def test_a_legacy_member_without_landed_hash_updates_rather_than_detaching(
        alice: TestClient, bob: TestClient, monkeypatch) -> None:
    # A subscription pinned by #77 (before landed_hash existed) has member bodies of {uid, hash}
    # only. On the next update the guard finds landed_hash absent — it must give the doc the benefit
    # of the doubt (adopt the current local doc as the baseline) and UPDATE it, never detach on a
    # missing field. It also adopts a real landed_hash going forward.
    vid, ids, local_id, blob1 = _subscribed(alice, bob, monkeypatch)
    store: VaultStore = bob.app.state.vaults
    uid_b = _row(blob1, "Guidance")["uid"]
    member = store.member_map(local_id)[uid_b]
    doc_b = member["doc_id"]

    # Rewrite that member body the #77 way: a single {uid, hash} dict with NO landed_hash.
    nonce = os.urandom(12)
    ciphertext = store._aes.encrypt(
        nonce, json.dumps({"uid": uid_b, "hash": member["hash"]}).encode("utf-8"),
        store._member_aad(local_id, doc_b))
    store._conn.execute(
        "UPDATE vault_documents SET nonce = ?, ciphertext = ? WHERE vault_id = ? AND doc_id = ?;",
        [nonce, ciphertext, local_id, doc_b])
    assert store.member_map(local_id)[uid_b]["landed_hash"] is None

    alice.app.state.kb.replace(ids[1], "Guidance", "for a WOMBAT exemption, file form 99X", {})
    _serve(monkeypatch, _export(alice, vid, _PASS_A))
    r = bob.post(f"/api/vaults/{local_id}/update")
    assert r.status_code == 200, r.text
    assert r.json()["updated"] == 1 and r.json()["kept_yours"] == 0, "legacy member updates, not detached"
    assert bob.get(f"/api/kb/{doc_b}").json()["content"].endswith("99X")
    assert store.origin_of(local_id, doc_b) == "import"
    assert store.member_map(local_id)[uid_b]["landed_hash"] is not None, "adopted lazily on update"


def test_upstream_delete_of_an_owner_origin_member_keeps_the_doc(alice: TestClient, bob: TestClient,
                                                                 monkeypatch) -> None:
    # Bob authored a doc; the subscribed vault shipped identical text, so subscribe deduped it to
    # HIS copy (origin owner). The publisher later deletes that uid — Bob's document must survive.
    bob.post("/api/kb", json={"title": "My own notes", "content": _DOCS[0][1]})
    vid, ids, local_id, blob1 = _subscribed(alice, bob, monkeypatch)
    store: VaultStore = bob.app.state.vaults
    uid_a = _row(blob1, "Regulations")["uid"]
    doc_a = store.member_map(local_id)[uid_a]["doc_id"]
    assert store.origin_of(local_id, doc_a) == "owner"

    alice.delete(f"/api/vaults/{vid}/documents/{ids[0]}")
    _serve(monkeypatch, _export(alice, vid, _PASS_A))
    r = bob.post(f"/api/vaults/{local_id}/update")
    assert r.status_code == 200
    assert r.json()["deleted"] == 0 and r.json()["kept_yours"] == 1
    assert bob.get(f"/api/kb/{doc_a}").status_code == 200, "never delete the user's own document"
    assert uid_a not in store.member_map(local_id), "but the stale upstream source is pruned"


def test_over_the_doc_bound_refuses_and_the_transaction_rolls_back(alice: TestClient, bob: TestClient,
                                                                   monkeypatch) -> None:
    # The bound is on the vault AFTER the update — owner-added members count. The refusal comes
    # AFTER new docs were written inside the transaction, so this also proves the rollback: the
    # landed doc must vanish with the refusal.
    vid, ids, local_id, _blob = _subscribed(alice, bob, monkeypatch,
                                            docs=[_DOCS[0], _DOCS[1]])
    own = [bob.post("/api/kb", json={"title": f"Mine {i}", "content": f"my text {i}"}).json()["id"]
           for i in range(2)]
    bob.post(f"/api/vaults/{local_id}/documents", json={"doc_ids": own})
    assert bob.app.state.vaults.count_documents(local_id) == 4

    monkeypatch.setattr(vault_format, "MAX_VAULT_DOCS", 4)
    new_id = alice.post("/api/kb", json={"title": "Bulletin", "content": "new KOALA rules"}).json()["id"]
    alice.post(f"/api/vaults/{vid}/documents", json={"doc_ids": [new_id]})
    _serve(monkeypatch, _export(alice, vid, _PASS_A))

    docs_before = {d["id"] for d in bob.get("/api/kb").json()["documents"]}
    r = bob.post(f"/api/vaults/{local_id}/update")
    assert r.status_code == 400 and "over 4 documents" in r.json()["detail"]
    assert {d["id"] for d in bob.get("/api/kb").json()["documents"]} == docs_before, \
        "the doc added inside the transaction must be rolled back with it"
    assert _pin(bob, local_id)["source"]["seq"] == 2
    assert bob.app.state.vaults.count_documents(local_id) == 4


# --- files re-enter the same machinery --------------------------------------------------------------

def test_reimporting_a_newer_file_of_a_pinned_vault_is_an_update(alice: TestClient, bob: TestClient,
                                                                 monkeypatch) -> None:
    vid, ids, local_id, blob1 = _subscribed(alice, bob, monkeypatch)
    store: VaultStore = bob.app.state.vaults
    uid_b = _row(blob1, "Guidance")["uid"]
    doc_b = store.member_map(local_id)[uid_b]["doc_id"]

    alice.app.state.kb.replace(ids[1], "Guidance", "for a WOMBAT exemption, file form 99X", {})
    blob2 = _export(alice, vid, _PASS_A)

    r = bob.post("/api/vaults/import", content=blob2)  # an OPEN file needs no key
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["update"] is True and body["id"] == local_id
    assert body["updated"] == 1 and body["added"] == 0 and body["seq"] == 3
    assert len(bob.get("/api/vaults").json()["vaults"]) == 1, "an update, not a duplicate vault"
    assert store.member_map(local_id)[uid_b]["doc_id"] == doc_b
    assert bob.get(f"/api/kb/{doc_b}").json()["content"].endswith("form 99X")

    # The same file again: same seq — already up to date, a no-op, still no duplicate.
    r = bob.post("/api/vaults/import", content=blob2)
    assert r.status_code == 200
    assert r.json()["update"] is True and r.json()["updated"] == 0 and r.json()["seq"] == 3
    # An OLDER file: the same rollback refusal a hostile URL gets.
    r = bob.post("/api/vaults/import", content=blob1)
    assert r.status_code == 409 and "roll back" in r.json()["detail"]


def test_reimport_refusals_different_key_and_sealed_file(alice: TestClient, bob: TestClient,
                                                         monkeypatch) -> None:
    vid, _ids, local_id, blob = _subscribed(alice, bob, monkeypatch)
    manifest = vault_format.read_manifest(blob)

    # A file naming the pinned vault_id but signed by a stranger: refused with both fingerprints.
    forged, evil_pub = _forge(manifest["vault_id"], 99, [
        {"uid": "evil-1", "title": "Poison", "content": "x", "meta": {}, "chunks": 1}])
    r = bob.post("/api/vaults/import", content=forged)
    assert r.status_code == 409
    assert vault_format.fingerprint(manifest["publisher"]["pubkey"]) in r.json()["detail"]
    assert vault_format.fingerprint(evil_pub) in r.json()["detail"]

    # A SEALED export of a vault pinned as an OPEN subscription: a clear refusal, not a guess.
    sealed = _export(alice, vid, _PASS_A, mode="sealed")
    key = alice.post(f"/api/vaults/{vid}/key", json={"passphrase": _PASS_A},
                     headers=_LOCAL).json()["key"]
    r = bob.post(f"/api/vaults/import?key={key}", content=sealed)
    assert r.status_code == 409 and "public edition" in r.json()["detail"]
    assert _pin(bob, local_id)["source"]["seq"] == 2, "neither refusal moved the pin"


def test_import_vault_failure_keeps_nothing_and_a_retry_succeeds(alice: TestClient, bob: TestClient,
                                                                 monkeypatch) -> None:
    # The same atomicity subscribe got in #77, now on the FILE path: a mid-apply failure must not
    # strand a partial vault whose pin blocks every retry as a duplicate.
    vid, _ids = _make_vault(alice, [(t, c) for t, c in _DOCS[:2]])
    blob = _export(alice, vid, _PASS_A, mode="sealed")
    key = alice.post(f"/api/vaults/{vid}/key", json={"passphrase": _PASS_A},
                     headers=_LOCAL).json()["key"]

    knowledge = bob.app.state.kb
    real_add = knowledge.add
    calls = {"n": 0}

    def flaky_add(title, content, meta=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("disk full")
        return real_add(title, content, meta)

    monkeypatch.setattr(knowledge, "add", flaky_add)
    r = bob.post(f"/api/vaults/import?key={key}", content=blob)
    assert r.status_code == 500 and "nothing was kept" in r.json()["detail"]
    assert bob.get("/api/vaults").json()["vaults"] == [], "no partial vault may remain"
    assert bob.get("/api/kb").json()["documents"] == [], "no half-landed documents either"

    r = bob.post(f"/api/vaults/import?key={key}", content=blob)
    assert r.status_code == 200 and r.json()["added"] == 2


# --- kb.replace: the primitive everything above leans on -------------------------------------------

def test_kb_replace_keeps_the_id_and_drops_stale_embeddings(bob: TestClient) -> None:
    knowledge = bob.app.state.kb
    doc_id = knowledge.add("Guide", "original text", {})
    knowledge.put_embeddings(doc_id, [[1.0, 0.0, 0.0]], "test-model")
    assert knowledge.get_embedding(doc_id) is not None

    assert knowledge.replace(doc_id, "Guide v2", "rewritten text", {"filename": "g.pdf"}) is True
    doc = knowledge.get(doc_id)
    assert doc["title"] == "Guide v2" and doc["content"] == "rewritten text"
    assert doc["meta"] == {"filename": "g.pdf"}
    # Stale vectors describe text that no longer exists: gone, and queued for re-embedding.
    assert knowledge.get_embedding(doc_id) is None
    assert doc_id in knowledge.docs_needing_embedding("test-model")
    # The lexical index followed the new text immediately.
    assert [h["id"] for h in knowledge.search("rewritten")] == [doc_id]
    assert knowledge.search("original") == []
    assert knowledge.replace("no-such-id", "T", "c") is False
