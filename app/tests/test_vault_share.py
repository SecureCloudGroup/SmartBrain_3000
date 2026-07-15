"""Sharing a vault: export it, send it to someone else, they import it and can search it.

The test that matters is `test_two_separate_users_can_share_a_vault`: TWO app instances with TWO
DIFFERENT passphrases (so genuinely different master keys). That is the whole product promise, and
nothing short of it proves the artifact is portable — documents are sealed under the OWNER's master
key, so a vault that "works" within one instance proves nothing at all.

The rest guard the properties a malicious or corrupted vault would attack.
"""

from __future__ import annotations

import base64
import json
import zipfile
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from smartbrain_3000 import gateway, vault_format

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


def _export(client: TestClient, vid: str, passphrase: str) -> tuple[bytes, str]:
    r = client.post(f"/api/vaults/{vid}/export", json={"passphrase": passphrase}, headers=_LOCAL)
    assert r.status_code == 200, r.text
    k = client.post(f"/api/vaults/{vid}/key", json={"passphrase": passphrase}, headers=_LOCAL)
    return r.content, k.json()["key"]


# --- THE product promise ------------------------------------------------------------------------

def test_two_separate_users_can_share_a_vault(alice: TestClient, bob: TestClient) -> None:
    # Alice and Bob have DIFFERENT passphrases, so genuinely different master keys. A document is
    # sealed under its owner's key, so this is the only test that proves the artifact is portable.
    vid = _make_vault(alice, [
        ("Regulations", "the QUOKKA clause governs all filings"),
        ("Guidance", "for a WOMBAT exemption, file form 12B"),
    ])
    blob, key = _export(alice, vid, _PASS_A)
    assert key.startswith("SBVK1-")

    r = bob.post(f"/api/vaults/import?key={key}", content=blob)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"] == 2 and body["publisher"].startswith("SB-")

    # Bob can now actually SEARCH what Alice sent him — the point of the whole exercise.
    hits = bob.get("/api/kb/search", params={"q": "quokka", "mode": "lexical"}).json()["results"]
    assert [h["title"] for h in hits] == ["Regulations"]

    # ...and it landed in a vault marked as imported, scoped and searchable on its own — carrying the
    # REAL name Alice gave it (recovered from the encrypted index, not a plaintext label).
    vaults = bob.get("/api/vaults").json()["vaults"]
    assert len(vaults) == 1 and vaults[0]["kind"] == "imported" and vaults[0]["doc_count"] == 2
    assert vaults[0]["name"] == "Expert pack"
    scoped = bob.get("/api/kb/search", params={"q": "wombat", "mode": "lexical",
                                               "vault": vaults[0]["id"]}).json()["results"]
    assert [h["title"] for h in scoped] == ["Guidance"]


def test_the_vault_name_never_appears_in_plaintext(alice: TestClient) -> None:
    # The name is the topic ("Divorce filings" says plenty). It must live ONLY inside the encrypted
    # index: anyone holding the FILE but not the key — a cloud host, a mail relay — learns the vault's
    # size and publisher, never what it's about.
    vid = _make_vault(alice, [("Doc", "contents here")], name="Divorce filings ZEBRA77")
    blob, key = _export(alice, vid, _PASS_A)
    assert b"ZEBRA77" not in blob, "vault name leaked into the artifact's plaintext"
    manifest = vault_format.read_manifest(blob)
    assert manifest["publisher"]["label"] == ""
    # ...and the importer still recovers the real name, because it rides the encrypted index.
    opened, _docs = vault_format.open_vault(blob, vault_format.decode_vault_key(key))
    assert opened["_sealed"]["name"] == "Divorce filings ZEBRA77"


def test_bobs_copy_is_re_sealed_not_a_copied_ciphertext(alice: TestClient, bob: TestClient) -> None:
    # Import must RE-SEAL under Bob's own master key. The GCM tag is bound to the doc_id, so there is
    # no such thing as importing a ciphertext — Bob's copy must get a FRESH local id and be sealed
    # under a key Bob actually holds. (Minting locally is also what makes it impossible for a
    # malicious vault to name a document with an id that collides with one Bob already has.)
    alice_doc = alice.post("/api/kb", json={"title": "Doc", "content": "the QUOKKA clause"}).json()["id"]
    vid = alice.post("/api/vaults", json={"name": "V"}).json()["id"]
    alice.post(f"/api/vaults/{vid}/documents", json={"doc_ids": [alice_doc]})
    blob, key = _export(alice, vid, _PASS_A)

    # The transported file itself is sealed: the plaintext is not sitting in it.
    assert b"QUOKKA" not in blob, "a sealed vault must not carry plaintext"

    bob.post(f"/api/vaults/import?key={key}", content=blob)
    bob_doc = bob.get("/api/kb").json()["documents"][0]["id"]
    assert bob_doc != alice_doc, "the importer must mint a fresh local id, not reuse the publisher's"

    # And Bob can read it back — i.e. it really is sealed under a key Bob has.
    got = bob.get(f"/api/kb/{bob_doc}").json()
    assert got["content"] == "the QUOKKA clause"


def test_the_recipient_keeps_their_own_copy_when_content_already_exists(alice, bob) -> None:
    # Never overwrite something the user authored with a stranger's copy of the same text.
    bob.post("/api/kb", json={"title": "My own notes", "content": "the QUOKKA clause"})
    vid = _make_vault(alice, [("Alice's version", "the QUOKKA clause")])
    blob, key = _export(alice, vid, _PASS_A)

    body = bob.post(f"/api/vaults/import?key={key}", content=blob).json()
    assert body["added"] == 0 and body["duplicates"] == 1
    titles = [d["title"] for d in bob.get("/api/kb").json()["documents"]]
    assert titles == ["My own notes"], "the user's own title must survive the import"


def test_shipped_vectors_make_an_imported_vault_instantly_searchable(alice, bob, monkeypatch) -> None:
    # The difference between a product and a download: the recipient shouldn't have to re-embed.
    monkeypatch.setattr(gateway, "embed", lambda *a, **k: [1.0, 0.0, 0.0])
    vid = _make_vault(alice, [("Doc", "alpha content")])
    alice.post("/api/kb/reindex")  # give Alice's document real vectors to ship
    blob, key = _export(alice, vid, _PASS_A)

    body = bob.post(f"/api/vaults/import?key={key}", content=blob).json()
    assert body["vectors_used"] is True
    assert bob.get("/api/kb/index-status").json()["pending"] == 0, "no re-embedding needed"
    hits = bob.get("/api/kb/search", params={"q": "alpha", "mode": "semantic"}).json()
    assert hits["degraded"] is False and hits["results"]


# --- an untrusted file must not be able to hurt the importer -------------------------------------

def test_a_tampered_vault_is_refused(alice: TestClient, bob: TestClient) -> None:
    # Flip a byte in a document object: its hash no longer matches the SIGNED index.
    vid = _make_vault(alice, [("Doc", "the QUOKKA clause")])
    blob, key = _export(alice, vid, _PASS_A)

    import io
    src = zipfile.ZipFile(io.BytesIO(blob))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename.startswith("objects/"):
                data = bytes([data[0] ^ 0xFF]) + data[1:]  # corrupt the ciphertext
            zf.writestr(info.filename, data)

    r = bob.post(f"/api/vaults/import?key={key}", content=out.getvalue())
    assert r.status_code == 400
    assert bob.get("/api/kb").json()["documents"] == [], "nothing may be imported from a bad vault"


def test_a_forged_manifest_is_refused(alice: TestClient, bob: TestClient) -> None:
    # Rewrite the manifest payload (e.g. bump doc_count) without the publisher's private key.
    vid = _make_vault(alice, [("Doc", "body")])
    blob, key = _export(alice, vid, _PASS_A)

    import io
    src = zipfile.ZipFile(io.BytesIO(blob))
    envelope = json.loads(src.read("manifest.json"))
    envelope["sbvault"]["doc_count"] = 99  # signature now covers different bytes
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for info in src.infolist():
            data = vault_format.canonical(envelope) if info.filename == "manifest.json" else src.read(info.filename)
            zf.writestr(info.filename, data)

    r = bob.post(f"/api/vaults/import?key={key}", content=out.getvalue())
    assert r.status_code == 400 and "signature" in r.json()["detail"].lower()


def test_the_wrong_key_says_so_plainly(alice: TestClient, bob: TestClient) -> None:
    vid = _make_vault(alice, [("Doc", "body")])
    blob, _ = _export(alice, vid, _PASS_A)
    other = vault_format.encode_vault_key(vault_format.new_vault_key())
    r = bob.post(f"/api/vaults/import?key={other}", content=blob)
    assert r.status_code == 400 and "doesn't open" in r.json()["detail"]


def test_a_recovery_key_cannot_be_mistaken_for_a_vault_key() -> None:
    # THE dangerous confusion: a user could text their RECOVERY key to a friend, believing it is a
    # vault key — handing over their entire brain. The SBVK1- tag makes that impossible to do silently.
    from smartbrain_3000 import keyvault

    recovery, _raw = keyvault.gen_recovery_key()
    with pytest.raises(vault_format.VaultError, match="not a vault key"):
        vault_format.decode_vault_key(recovery)


def test_export_is_desktop_local_and_needs_the_passphrase(alice: TestClient) -> None:
    # An export is plaintext-equivalent egress, so it gets the same gate as /api/backup.
    vid = _make_vault(alice, [("Doc", "body")])
    assert alice.post(f"/api/vaults/{vid}/export", json={"passphrase": _PASS_A}).status_code == 403
    r = alice.post(f"/api/vaults/{vid}/export", json={"passphrase": "wrong"}, headers=_LOCAL)
    assert r.status_code == 401


# --- format-level guards -------------------------------------------------------------------------

def test_export_is_byte_reproducible() -> None:
    # Deterministic nonces + fixed zip timestamps: the same content produces the same file, so an
    # incremental publish can upload only what actually changed.
    from smartbrain_3000.secrets import SecretStore, gen_master_key
    import duckdb
    from smartbrain_3000 import db as dbmod

    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    store = SecretStore(conn, gen_master_key())
    key = vault_format.new_vault_key()
    docs = [{"uid": "u1", "title": "T", "content": "body", "meta": {}, "chunks": 1}]
    kwargs = dict(store=store, vault_id="v1", name="V", description="", seq=1, docs=docs, vault_key=key)
    assert vault_format.pack(**kwargs) == vault_format.pack(**kwargs)


def test_a_hostile_page_map_is_rejected() -> None:
    # kb.page_for does bisect_right(pages, offset) and TRUSTS the list: a non-increasing or huge one
    # gives wrong citations ("p.12" pointing at page 3) or a memory hit.
    with pytest.raises(vault_format.VaultError, match="strictly increasing"):
        vault_format._clean_meta({"pages": [0, 50, 20]})
    with pytest.raises(vault_format.VaultError, match="too long"):
        vault_format._clean_meta({"pages": list(range(vault_format.MAX_PAGES + 1))})


def test_a_non_finite_vector_is_rejected() -> None:
    # One inf makes `matrix @ q` produce NaN and ranks the WHOLE corpus at random — the one place a
    # malicious vault could silently break search. It must be an error, not an assert (asserts vanish
    # under python -O).
    import struct

    raw = vault_format._VEC_MAGIC + struct.pack("<HHH", 2, 1, 0) + struct.pack("<2f", float("inf"), 0.0)
    with pytest.raises(vault_format.VaultError, match="non-finite"):
        vault_format._read_vec_object(raw)


def test_duplicate_json_keys_are_rejected() -> None:
    # json.loads keeps the LAST duplicate silently — "last one wins" is how signature bypasses work.
    with pytest.raises(vault_format.VaultError, match="duplicate key"):
        vault_format.parse_canonical(b'{"a":1,"a":2}')


def test_non_canonical_json_is_rejected() -> None:
    # We must act on exactly the bytes we verified, so the manifest's bytes must already be canonical.
    with pytest.raises(vault_format.VaultError, match="canonical"):
        vault_format.parse_canonical(b'{"b":1, "a":2}')  # spacing + key order


def test_vault_key_roundtrips() -> None:
    raw = vault_format.new_vault_key()
    assert vault_format.decode_vault_key(vault_format.encode_vault_key(raw)) == raw
    assert base64.b64encode(raw)  # sanity: it is 32 real bytes
