"""Publishing a vault OPEN: the Stage-B flow — export with no Vault Key at all.

Two properties carry the product here. The gate: an open export is DECRYPTED PLAINTEXT, the most
sensitive egress in the app, so it demands exactly the sealed export's Desktop-local + passphrase
re-auth. And K_name stability (plan decision #6): object names are HMAC(K_name, ...), so K_name is
fixed at the FIRST open publish — derived from the stored Vault Key when the vault was sealed-shared
before (the flip keeps every object name), minted and PERSISTED in the encrypted body when born open.
Without that persistence every republish would rename every object, and a tree-host subscriber's
"fetch only what changed" would degenerate into a full re-download — delta updates would die.

Route-level throughout (test_vault_share style): the library half is proven in test_vault_open.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import vault_format

_PASS_A = "alice-correct-horse"
_PASS_B = "bob-correct-horse"
_LOCAL = {"x-sb-local": "1"}  # export is Desktop-local only (the WebRTC bridge cannot forward this)


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


def _export(client: TestClient, vid: str, passphrase: str, mode: str = "sealed") -> bytes:
    r = client.post(f"/api/vaults/{vid}/export",
                    json={"passphrase": passphrase, "mode": mode}, headers=_LOCAL)
    assert r.status_code == 200, r.text
    return r.content


# --- zip introspection (read-only; the surgical-tamper helpers live in test_vault_open) ----------

def _entries(blob: bytes) -> dict[str, bytes]:
    zf = zipfile.ZipFile(io.BytesIO(blob))
    return {n: zf.read(n) for n in zf.namelist()}


def _manifest(blob: bytes) -> dict:
    return json.loads(_entries(blob)["manifest.json"])["sbvault"]


def _objects(blob: bytes) -> dict[str, bytes]:
    return {n: b for n, b in _entries(blob).items() if n.startswith("objects/")}


_DOCS = [
    ("Regulations", "the QUOKKA clause governs all filings"),
    ("Guidance", "for a WOMBAT exemption, file form 12B"),
]


# --- the gate: an open export is the most sensitive egress there is ------------------------------

def test_open_export_is_desktop_local_and_needs_the_passphrase(alice: TestClient) -> None:
    # Mirror of the sealed gate test: open hands out the PLAINTEXT, so it must never be one gate
    # weaker than sealed — no bridged device, no stale session, no passer-by.
    vid = _make_vault(alice, [("Doc", "body")])
    r = alice.post(f"/api/vaults/{vid}/export", json={"passphrase": _PASS_A, "mode": "open"})
    assert r.status_code == 403, "a bridged (non-desktop-local) open export must be refused"
    r = alice.post(f"/api/vaults/{vid}/export",
                   json={"passphrase": "wrong", "mode": "open"}, headers=_LOCAL)
    assert r.status_code == 401, "an open export must re-verify the passphrase"


# --- decision #6: republishing must not rename objects (or tree-host deltas die) ------------------

def test_an_unchanged_republish_changes_only_seq_and_signature(alice: TestClient) -> None:
    # THE property that makes hosting the unzipped tree work: publish twice with nothing edited and
    # every objects/* entry is byte-identical — a subscriber (and the publisher's next upload)
    # touches only the manifest and index. Requires the persisted name_key; a fresh one per export
    # would rename everything.
    vid = _make_vault(alice, _DOCS)
    first = _export(alice, vid, _PASS_A, "open")
    second = _export(alice, vid, _PASS_A, "open")

    assert _objects(first) == _objects(second), "unchanged content must keep byte-identical objects"

    m1, m2 = _manifest(first), _manifest(second)
    assert m2["seq"] == m1["seq"] + 1
    # The index embeds seq, so its hash moves with it; everything else is identical — including
    # name_key, which is what proves the key was persisted rather than re-minted.
    drop = ("seq", "index")
    assert {k: v for k, v in m1.items() if k not in drop} == \
           {k: v for k, v in m2.items() if k not in drop}
    i1, i2 = json.loads(_entries(first)["index.bin"]), json.loads(_entries(second)["index.bin"])
    assert i1["docs"] == i2["docs"], "per-document rows (uid/hash/obj) must survive a republish"
    assert json.loads(_entries(first)["manifest.json"])["sig"] != \
           json.loads(_entries(second)["manifest.json"])["sig"]


def test_a_born_open_vault_keeps_its_object_names_across_a_rename(alice: TestClient) -> None:
    # A vault that was NEVER sealed-exported has no Vault Key to derive from, so its random K_name
    # lives only in the encrypted body. Renaming rewrites that body — this is the #73
    # read-modify-write guarantee earning its keep: lose the field, and the next publish silently
    # renames every object.
    vid = _make_vault(alice, _DOCS, name="Field notes")
    first = _export(alice, vid, _PASS_A, "open")
    r = alice.patch(f"/api/vaults/{vid}", json={"name": "Renamed notes", "description": "v2"})
    assert r.status_code == 200
    second = _export(alice, vid, _PASS_A, "open")

    assert sorted(_objects(first)) == sorted(_objects(second)), \
        "the persisted name_key must survive a body rewrite (rename)"
    assert _manifest(second)["name"] == "Renamed notes"  # open mode publishes the topic


def test_a_sealed_then_open_flip_keeps_every_object_name(alice: TestClient) -> None:
    # Publishing must be an UNLOCK, not a rewrite: someone who mirrored the sealed tree sees an
    # in-place mode change. Route-level: the export handler must derive K_name from the STORED
    # Vault Key, exactly as the sealed pack did.
    vid = _make_vault(alice, _DOCS)
    sealed = _export(alice, vid, _PASS_A)  # mode defaults to sealed; mints + stores the Vault Key
    opened = _export(alice, vid, _PASS_A, "open")

    assert sorted(_objects(sealed)) == sorted(_objects(opened)), \
        "a sealed->open flip must keep every object name"
    assert _manifest(opened)["vault_id"] == _manifest(sealed)["vault_id"]
    assert _manifest(opened)["seq"] == _manifest(sealed)["seq"] + 1

    # The sealed key survives the flip: recipients of the old sealed file still need it.
    k = alice.post(f"/api/vaults/{vid}/key", json={"passphrase": _PASS_A}, headers=_LOCAL)
    assert k.status_code == 200 and k.json()["key"].startswith("SBVK1-")

    # And a LATER re-seal (which mints a fresh Vault Key) must not reshuffle the public tree:
    # K_name was fixed at the first open publish, so open subscribers keep their delta updates.
    _export(alice, vid, _PASS_A)  # sealed again — new Vault Key remembered
    third = _export(alice, vid, _PASS_A, "open")
    assert sorted(_objects(third)) == sorted(_objects(opened)), \
        "re-sealing must not rename the published open tree"


# --- "there is no key" must be said, not implied --------------------------------------------------

def test_the_key_route_409s_on_a_vault_only_published_open(alice: TestClient) -> None:
    vid = _make_vault(alice, [("Doc", "body")])
    _export(alice, vid, _PASS_A, "open")
    r = alice.post(f"/api/vaults/{vid}/key", json={"passphrase": _PASS_A}, headers=_LOCAL)
    assert r.status_code == 409
    assert "there is no key" in r.json()["detail"], "the refusal must say WHY there is no key"
    assert "anyone with the file can read it" in r.json()["detail"]


def test_an_open_export_mints_no_vault_key(alice: TestClient) -> None:
    # "Public" == "there is no Vault Key" — the export must not quietly mint one (a key that opens
    # nothing would be worse than none: the user would send it believing it protects something).
    vid = _make_vault(alice, [("Doc", "body")])
    blob = _export(alice, vid, _PASS_A, "open")
    manifest = _manifest(blob)
    assert manifest["mode"] == "open" and "crypto" not in manifest
    r = alice.post(f"/api/vaults/{vid}/key", json={"passphrase": _PASS_A}, headers=_LOCAL)
    assert r.status_code == 409


# --- the §2 metadata rule, seen from the route ----------------------------------------------------

def test_the_open_manifest_carries_the_name_a_sealed_one_hides(alice: TestClient) -> None:
    # Same vault, both modes: sealed keeps the topic out of the plaintext manifest (a host learns
    # size and publisher, not subject); open publishes it — the topic is public anyway.
    vid = _make_vault(alice, [("Doc", "contents")], name="Beekeeping ZEBRA77")
    sealed = _export(alice, vid, _PASS_A)
    assert b"ZEBRA77" not in sealed, "the sealed artifact must not leak the vault name"
    opened = _export(alice, vid, _PASS_A, "open")
    m = _manifest(opened)
    assert m["name"] == "Beekeeping ZEBRA77" and "description" in m and "name_key" in m
    # The plaintext really is in the open file — that is the whole point of "public".
    assert b"contents" in opened


# --- the UI's badge: never a "Public" label without the identity behind it ------------------------

def test_the_vault_list_marks_a_published_open_vault_with_the_fingerprint(alice: TestClient) -> None:
    vid_pub = _make_vault(alice, [("Doc", "public body")], name="Published")
    vid_priv = _make_vault(alice, [("Doc2", "private body")], name="Private")
    blob = _export(alice, vid_pub, _PASS_A, "open")
    _export(alice, vid_priv, _PASS_A)  # sealed share only — must NOT be marked public

    by_id = {v["id"]: v for v in alice.get("/api/vaults").json()["vaults"]}
    assert by_id[vid_pub]["published_open"] is True
    # The fingerprint shown on the card is the SAME identity the file itself carries — what a
    # subscriber pins. A label without it (or with a different one) would be decoration.
    expected = vault_format.fingerprint(_manifest(blob)["publisher"]["pubkey"])
    assert by_id[vid_pub]["publisher_fingerprint"] == expected
    assert by_id[vid_priv]["published_open"] is False
    assert "publisher_fingerprint" not in by_id[vid_priv]

    # get_vault (the card's other data source) agrees with the list.
    got = alice.get(f"/api/vaults/{vid_pub}").json()
    assert got["published_open"] is True and got["publisher_fingerprint"] == expected


# --- parity with sealed for imported vaults --------------------------------------------------------

def test_an_imported_vault_re_exports_open_exactly_as_it_does_sealed(alice: TestClient, bob: TestClient) -> None:
    # Sealed imposes no kind check on export (re-sharing an imported vault re-signs it as YOUR
    # publication), so open keeps parity: both succeed. Pinning this stops the two modes from
    # silently diverging — if re-export is ever forbidden, it must be forbidden for both.
    vid = _make_vault(alice, [("Doc", "the QUOKKA clause")])
    blob = _export(alice, vid, _PASS_A)
    key = alice.post(f"/api/vaults/{vid}/key",
                     json={"passphrase": _PASS_A}, headers=_LOCAL).json()["key"]
    imported = bob.post(f"/api/vaults/import?key={key}", content=blob).json()["id"]

    r_sealed = bob.post(f"/api/vaults/{imported}/export",
                        json={"passphrase": _PASS_B}, headers=_LOCAL)
    r_open = bob.post(f"/api/vaults/{imported}/export",
                      json={"passphrase": _PASS_B, "mode": "open"}, headers=_LOCAL)
    assert r_sealed.status_code == r_open.status_code == 200
    # Bob's republication is signed by BOB's key — provenance changes hands honestly.
    assert _manifest(r_open.content)["publisher"]["pubkey"] != _manifest(blob)["publisher"]["pubkey"]
