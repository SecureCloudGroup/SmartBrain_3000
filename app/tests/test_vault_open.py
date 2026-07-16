"""Open (public/hosted) vaults: the SAME container as sealed, minus the content encryption.

The load-bearing property, proved by ``test_sealed_to_open_flip_is_byte_identical_where_it_must_be``:
packing a vault sealed and open with the SAME K_name yields byte-identical uids, content hashes, and
``objects/*`` entry NAMES — only the object bodies (ciphertext -> plaintext) and the manifest
(crypto block dropped, name/name_key added) differ. That is what makes "publish an existing vault" an
unlock, not a rewrite. The rest guard the properties a malicious or corrupted OPEN vault would attack
with no key in play — so the hash chain and the object-name recomputation carry the whole load.

Stage A is pure library work (no routes/UI), so these drive vault_format directly.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile

import duckdb
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import identity, vault_format
from smartbrain_3000.secrets import SecretStore, gen_master_key


def _store() -> SecretStore:
    """A fresh unlocked SecretStore — its publisher Ed25519 key signs the vaults it packs."""
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    return SecretStore(conn, gen_master_key())


def _name_key(vault_id: str) -> bytes:
    """K_name derived exactly as sealed mode derives it — so an open pack reusing it flips clean."""
    return vault_format._derive(vault_format.new_vault_key(), vault_id, b"sbvault/v1/objname")


def _pack_open(store, *, vault_id="v-open", name="Expert pack", description="d", seq=1,
               docs=None, name_key=None, embed_model="") -> bytes:
    return vault_format.pack(
        store=store, vault_id=vault_id, name=name, description=description, seq=seq,
        docs=docs if docs is not None else _DOCS, name_key=name_key or _name_key(vault_id),
        mode=vault_format.OPEN, embed_model=embed_model)


_DOCS = [
    {"uid": "u1", "title": "Regulations", "content": "the QUOKKA clause governs all filings",
     "meta": {}, "chunks": 1},
    {"uid": "u2", "title": "Guidance", "content": "for a WOMBAT exemption, file form 12B",
     "meta": {}, "chunks": 1},
]


# --- zip helpers (white-box: read, surgically edit, and re-sign a vault as the publisher) --------

def _entries(blob: bytes) -> dict[str, bytes]:
    zf = zipfile.ZipFile(io.BytesIO(blob))
    return {n: zf.read(n) for n in zf.namelist()}


def _manifest(blob: bytes) -> dict:
    return json.loads(_entries(blob)["manifest.json"])["sbvault"]


def _index(blob: bytes) -> dict:
    return json.loads(_entries(blob)["index.bin"])


def _obj_names(blob: bytes) -> list[str]:
    return sorted(n for n in _entries(blob) if n.startswith("objects/"))


def _sign(store, payload: dict) -> bytes:
    """Rebuild manifest.json signed by the store's real publisher key (pubkey already matches)."""
    raw = vault_format.canonical(payload)
    sig = identity.sign(store, vault_format._SIG_PREFIX + raw, identity.VAULT_PUBLISHER_SECRET)
    return vault_format.canonical({"sbvault": payload, "sig": {"alg": "ed25519", "value": sig}})


def _repack(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in (["manifest.json", "index.bin"]
                     + sorted(n for n in entries if n.startswith("objects/"))):
            zf.writestr(name, entries[name])
    return buf.getvalue()


# --- THE product promise: no key needed to import a public vault --------------------------------

def test_open_export_round_trips_without_a_key() -> None:
    # Mirrors the sealed two-user share, distilled to what open actually changes: a reader who shares
    # NO secret with the publisher — no passphrase, no vault key, not even a store — still opens it.
    store = _store()
    blob = _pack_open(store)
    payload, docs = vault_format.open_vault(blob)  # note: no key argument at all
    assert payload["mode"] == "open"
    assert payload["_sealed"]["name"] == "Expert pack"  # the name rides the manifest, not the index
    assert {d["uid"]: d["content"] for d in docs} == {
        "u1": _DOCS[0]["content"], "u2": _DOCS[1]["content"]}
    # The publisher is still cryptographically identified even with nothing encrypted.
    assert vault_format.fingerprint(payload["publisher"]["pubkey"]).startswith("SB-")
    # ...and the body really is plaintext in the file (that is the whole point of "public").
    assert b"QUOKKA" in blob


# --- THE core invariant: sealed -> open is an unlock, not a rewrite ------------------------------

def test_sealed_to_open_flip_is_byte_identical_where_it_must_be() -> None:
    store = _store()
    vault_id = "v-flip"
    vault_key = vault_format.new_vault_key()
    # The caller derives K_name the way sealed does, then hands it to the open pack.
    name_key = vault_format._derive(vault_key, vault_id, b"sbvault/v1/objname")
    docs = [
        {"uid": "u1", "title": "Doc one", "content": "alpha body", "meta": {}, "chunks": 1,
         "vectors": [[1.0, 0.0], [0.0, 1.0]]},  # a vec object exercises vec-name identity too
        {"uid": "u2", "title": "Doc two", "content": "beta body", "meta": {}, "chunks": 1},
    ]
    common = dict(store=store, vault_id=vault_id, name="Topic", description="desc", seq=7,
                  docs=docs, embed_model="test-model")
    sealed = vault_format.pack(**common, vault_key=vault_key)  # mode defaults to sealed
    opened = vault_format.pack(**common, name_key=name_key, mode=vault_format.OPEN)

    sm, om = _manifest(sealed), _manifest(opened)
    # Object entry NAMES, uids, content hashes, and even the index hash survive the flip untouched.
    assert _obj_names(sealed) == _obj_names(opened)
    assert sm["index"]["hash"] == om["index"]["hash"]
    assert sm["vault_id"] == om["vault_id"] and sm["doc_count"] == om["doc_count"]
    # The manifests differ ONLY in the mode flag and the crypto <-> name/name_key swap.
    assert sm["mode"] == "sealed" and om["mode"] == "open"
    assert "crypto" in sm and "crypto" not in om
    assert {"name", "description", "name_key"} <= om.keys()
    assert not any(k in sm for k in ("name", "description", "name_key"))
    assert {k: v for k, v in sm.items() if k not in ("mode", "crypto")} == \
           {k: v for k, v in om.items() if k not in ("mode", "name", "description", "name_key")}
    # Only the stored index BODY changes: ciphertext -> the same plaintext it was signed over.
    assert _entries(sealed)["index.bin"] != _entries(opened)["index.bin"]

    # And the flipped file opens with no key and yields the same documents.
    _payload, plain = vault_format.open_vault(opened)
    assert {d["uid"]: d["content"] for d in plain} == {"u1": "alpha body", "u2": "beta body"}


# --- an untrusted OPEN file must not be able to hurt the importer either --------------------------

def test_a_tampered_open_object_is_refused() -> None:
    # No encryption to catch this — but the hash chain (manifest sig -> index hash -> doc hash) does:
    # a flipped plaintext body no longer matches the digest the signed index committed to.
    store = _store()
    blob = _pack_open(store, docs=[_DOCS[0]])
    name = _obj_names(blob)[0]
    entries = _entries(blob)
    entries[name] = bytes([entries[name][0] ^ 0xFF]) + entries[name][1:]
    with pytest.raises(vault_format.VaultError, match="does not match its signed hash"):
        vault_format.open_vault(_repack(entries))


def test_a_swapped_object_is_refused_by_the_name_check() -> None:
    # A hostile publisher re-signs a self-consistent index (hash matches) that points a document at a
    # well-formed but WRONG object parked under a name that is not its content-addressed HMAC. Only
    # the object-name recomputation stops this — there is no GCM tag to fail.
    store = _store()
    vault_id = "v-swap"
    name_key = _name_key(vault_id)
    blob = _pack_open(store, vault_id=vault_id, docs=[_DOCS[0]], name_key=name_key)

    rogue = vault_format._doc_object("Rogue", "attacker body", {})
    rogue_hash = hashlib.sha256(rogue).hexdigest()
    stray = "0" * 32  # a valid-looking name that is NOT HMAC(K_name, doc|u1|rogue_hash)
    assert vault_format._obj_name(name_key, b"doc", "u1", rogue_hash) != stray

    index = _index(blob)
    index["docs"] = [{"uid": "u1", "title": "Doc", "hash": rogue_hash, "obj": stray,
                      "bytes": len(rogue), "chunks": 1}]
    index_raw = vault_format.canonical(index)
    payload = _manifest(blob)
    payload["index"] = {"hash": hashlib.sha256(index_raw).hexdigest(), "bytes": len(index_raw)}

    entries = {k: v for k, v in _entries(blob).items() if not k.startswith("objects/")}
    entries[f"objects/{stray}.bin"] = rogue
    entries["index.bin"] = index_raw
    entries["manifest.json"] = _sign(store, payload)
    with pytest.raises(vault_format.VaultError, match="misnamed"):
        vault_format.open_vault(_repack(entries))


# --- mode confusion: each half of a mislabelled file is refused cleanly ---------------------------

def test_a_sealed_manifest_carrying_a_name_is_refused() -> None:
    # The §2 metadata-leak rule, enforced at read time: a sealed manifest must never carry its topic.
    store = _store()
    vault_id = "v-seal"
    docs = [{"uid": "u1", "title": "Doc", "content": "body", "meta": {}, "chunks": 1}]
    blob = vault_format.pack(store=store, vault_id=vault_id, name="N", description="", seq=1,
                             docs=docs, vault_key=vault_format.new_vault_key())
    payload = _manifest(blob)
    payload["name"] = "Divorce filings"
    payload["name_key"] = base64.b64encode(b"x" * 32).decode()
    entries = _entries(blob)
    entries["manifest.json"] = _sign(store, payload)
    with pytest.raises(vault_format.VaultError, match="must not carry its name"):
        vault_format.read_manifest(_repack(entries))


def test_an_open_manifest_carrying_crypto_is_refused() -> None:
    # Open == "there is no Vault Key", so a crypto block is a contradiction — refuse, don't guess.
    store = _store()
    blob = _pack_open(store)
    payload = _manifest(blob)
    payload["crypto"] = {"alg": "AES-256-GCM"}
    entries = _entries(blob)
    entries["manifest.json"] = _sign(store, payload)
    with pytest.raises(vault_format.VaultError, match="must not carry encryption"):
        vault_format.read_manifest(_repack(entries))


def test_an_open_labelled_file_whose_index_is_ciphertext_is_refused() -> None:
    # A sealed vault relabelled open (real ciphertext index, self-consistent hash, re-signed): open
    # mode reads the index raw, so the ciphertext fails to parse as canonical JSON.
    store = _store()
    vault_id = "v-conf"
    docs = [{"uid": "u1", "title": "Doc", "content": "body", "meta": {}, "chunks": 1}]
    blob = vault_format.pack(store=store, vault_id=vault_id, name="N", description="", seq=1,
                             docs=docs, vault_key=vault_format.new_vault_key())
    entries = _entries(blob)
    ciphertext = entries["index.bin"]  # genuine AES-GCM ciphertext
    payload = _manifest(blob)
    payload["mode"] = "open"
    payload.pop("crypto")
    payload["name_key"] = base64.b64encode(b"n" * 32).decode()
    payload["index"] = {"hash": hashlib.sha256(ciphertext).hexdigest(), "bytes": len(ciphertext)}
    entries["manifest.json"] = _sign(store, payload)
    with pytest.raises(vault_format.VaultError):  # index is not JSON at all
        vault_format.open_vault(_repack(entries))


# --- §0 regression: the signature is checked in open mode too ------------------------------------

def test_a_forged_open_manifest_is_refused() -> None:
    # Re-sign an open manifest with a DIFFERENT Ed25519 key while leaving the claimed publisher pubkey
    # unchanged: the signature no longer verifies against the key the manifest names. If open mode
    # skipped the signature check, a stranger could publish a "v2" of your public vault.
    store = _store()
    blob = _pack_open(store)
    payload = _manifest(blob)
    rogue = Ed25519PrivateKey.generate()
    sig = base64.b64encode(rogue.sign(vault_format._SIG_PREFIX + vault_format.canonical(payload)))
    entries = _entries(blob)
    entries["manifest.json"] = vault_format.canonical(
        {"sbvault": payload, "sig": {"alg": "ed25519", "value": sig.decode()}})
    with pytest.raises(vault_format.VaultError, match="signature"):
        vault_format.read_manifest(_repack(entries))


# --- new hardening (both modes): a duplicate uid would double-import -------------------------------

def test_a_duplicate_uid_in_the_index_is_refused() -> None:
    # uid stability IS the update mechanism (§3); two rows sharing one would import twice and make a
    # future update ambiguous. The check lives in the shared path, so it hardens sealed mode as well.
    store = _store()
    vault_id = "v-dup"
    name_key = _name_key(vault_id)
    blob = _pack_open(store, vault_id=vault_id, docs=[_DOCS[0]], name_key=name_key)
    index = _index(blob)
    index["docs"] = index["docs"] + [dict(index["docs"][0])]  # the same uid twice, same valid object
    index_raw = vault_format.canonical(index)
    payload = _manifest(blob)
    payload["index"] = {"hash": hashlib.sha256(index_raw).hexdigest(), "bytes": len(index_raw)}
    payload["doc_count"] = 2
    entries = _entries(blob)
    entries["index.bin"] = index_raw
    entries["manifest.json"] = _sign(store, payload)
    with pytest.raises(vault_format.VaultError, match="same document twice"):
        vault_format.open_vault(_repack(entries))


def test_a_signed_index_with_a_non_dict_row_is_a_clean_refusal() -> None:
    # A hostile publisher signs an index whose ``docs`` holds a bare string where a row object
    # belongs (doc_count made to match, so the length check can't mask it). read_index does
    # row.get(...) — without a row-shape guard that is an AttributeError -> HTTP 500; it must be the
    # module's clean VaultError instead. Found by adversarial review; newly reachable via remote input.
    store = _store()
    blob = _pack_open(store, docs=[_DOCS[0]])
    index = _index(blob)
    index["docs"] = ["not-a-dict-row"]
    index_raw = vault_format.canonical(index)
    payload = _manifest(blob)
    payload["index"] = {"hash": hashlib.sha256(index_raw).hexdigest(), "bytes": len(index_raw)}
    payload["doc_count"] = 1
    entries = {k: v for k, v in _entries(blob).items() if not k.startswith("objects/")}
    entries["index.bin"] = index_raw
    entries["manifest.json"] = _sign(store, payload)
    with pytest.raises(vault_format.VaultError, match="index entry is malformed"):
        vault_format.open_vault(_repack(entries))


# --- open exports are byte-reproducible too (incremental publish uploads only real changes) -------

def test_open_export_is_byte_reproducible() -> None:
    store = _store()
    name_key = vault_format.new_vault_key()  # a persisted random K_name, as a born-open vault would use
    kwargs = dict(store=store, vault_id="v1", name="V", description="", seq=1,
                  docs=[{"uid": "u1", "title": "T", "content": "body", "meta": {}, "chunks": 1}],
                  name_key=name_key, mode=vault_format.OPEN)
    assert vault_format.pack(**kwargs) == vault_format.pack(**kwargs)


@pytest.mark.parametrize("mutate", [
    lambda p: p.pop("vault_id"),                 # missing entirely -> was a KeyError
    lambda p: p.update(vault_id=7),              # wrong type -> was an AttributeError (.encode())
    lambda p: p.update(vault_id="x" * 101),      # unbounded id
    lambda p: p.pop("seq"),
    lambda p: p.update(seq="1"),
    lambda p: p.update(seq=True),                # bool sneaks past a bare isinstance(int) check
    lambda p: p.update(seq=-1),
    lambda p: p.pop("doc_count"),
    lambda p: p.update(doc_count=-2),
    lambda p: p.pop("index"),
    lambda p: p.update(index=["h"]),             # wrong type -> was a TypeError
    lambda p: p.update(index={"hash": 3}),
    lambda p: p.update(index={"hash": "ab"}),    # not a sha256 hexdigest
    lambda p: p.update(publisher="a-string"),    # truthy non-dict -> was (str).get() AttributeError
    lambda p: p.update(publisher=7),
    lambda p: p.update(publisher=["k"]),
])
def test_a_signed_but_malformed_manifest_is_a_clean_refusal(mutate) -> None:
    # A valid signature only proves WHO wrote the manifest — a hostile publisher signs whatever
    # they like. Every field open_vault dereferences must be guarded, or a signed-but-malformed
    # file escapes as a KeyError/TypeError (an HTTP 500) instead of the module's clean VaultError.
    # Found by adversarial review; the gap predated open mode (sealed had it too).
    store = _store()
    entries = _entries(_pack_open(store, docs=[_DOCS[0]]))
    # take the signed payload, mutate one field, re-sign as the publisher, repack
    payload = json.loads(entries["manifest.json"].decode())["sbvault"]
    mutate(payload)
    entries["manifest.json"] = _sign(store, payload)
    with pytest.raises(vault_format.VaultError, match="malformed"):
        vault_format.open_vault(_repack(entries))
