"""The ``.sbvault`` container: pack a vault to a portable file, and open one from a stranger.

See docs/internal/vault-format.md for the full spec and its rationale. The load-bearing idea:

    Confidentiality comes from the VAULT KEY (symmetric, shared with recipients).
    Authenticity comes from the PUBLISHER'S Ed25519 KEY (asymmetric, never shared).

So "public" is just "there is no Vault Key" — and the signature is present in BOTH modes, which is
why a friend you handed a sealed vault to still cannot forge a "v2" in your name. This module
implements the sealed (private-share) half; open/hosted vaults reuse it unchanged.

Nothing here touches the local at-rest encryption: an imported document is RE-SEALED under the
importer's own master key (AES-GCM AAD is the local doc_id — there is no such thing as importing a
ciphertext). This module only produces and consumes the transport artifact.

Everything read out of a vault file is UNTRUSTED. Every bound below is enforced, and exceeding one
is an error, never a silent truncation.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import struct
import zipfile

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import identity

FORMAT = "sbvault"
FORMAT_VERSION = 1
SEALED = "sealed"
OPEN = "open"

# --- bounds (all explicit, all verifiable; exceeding one is reported, never silent) -------------
MAX_VAULT_DOCS = 10_000  # 10k docs x ~10 chunks = 100k vectors = exactly kb._MAX_INDEXED_VECTORS
MAX_VAULT_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
MAX_INDEX_BYTES = 16 * 1024 * 1024
MAX_DOC_OBJECT_BYTES = 8 * 1024 * 1024  # ingest._MAX_TEXT (1M chars) + UTF-8/JSON escaping headroom
MAX_VEC_OBJECT_BYTES = 12 + 64 * 4096 * 4  # header + max chunks x max dim x float32
MAX_ZIP_EXPANSION = 100  # refuse an entry claiming >100x its compressed size (zip bomb)
MAX_TITLE = 300
MAX_PAGES = 1000  # mirrors ingest._MAX_SECTIONS
MAX_TEXT = 1_000_000  # mirrors ingest._MAX_TEXT
MAX_DIM = 4096  # mirrors kb._MAX_EMBED_DIM
MAX_CHUNKS = 64  # mirrors kb._MAX_CHUNKS

_MANIFEST = "manifest.json"
_INDEX = "index.bin"
_VEC_MAGIC = b"SBVEC1"
_KEY_PREFIX = "SBVK1-"
_SIG_PREFIX = b"sbvault-sig:v1\n"
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)  # fixed, so the same content produces a byte-identical file


class VaultError(Exception):
    """A vault file is malformed, untrusted, or cannot be opened with the key given."""


# --- canonical JSON: the bytes that get signed -------------------------------------------------

def canonical(obj) -> bytes:
    """Deterministic JSON. This is what a signature actually covers, so it must be unambiguous.

    ``allow_nan=False`` bans NaN/Infinity — and by convention the signed payload carries no floats
    at all: float formatting is *the* canonicalisation footgun, so we forbid the type rather than
    try to specify its rendering.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _no_duplicate_keys(pairs):
    """json.loads keeps the LAST duplicate key silently — and "last one wins" is how signature
    bypasses get built (sign one value, act on another). Reject the document instead."""
    seen: dict = {}
    for key, value in pairs:
        if key in seen:
            raise VaultError(f"duplicate key in vault JSON: {key!r}")
        seen[key] = value
    return seen


def parse_canonical(raw: bytes) -> dict:
    """Parse JSON that MUST already be canonical, so we always act on exactly what we verified.

    Re-serialising the parsed object and byte-comparing collapses "sign the bytes" and "sign the
    object" into the same thing — there is no gap between what was authenticated and what is used.
    """
    try:
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except VaultError:
        raise
    except Exception as exc:
        raise VaultError(f"vault JSON is malformed: {exc}") from None
    if not isinstance(obj, dict):
        raise VaultError("vault JSON must be an object")
    if canonical(obj) != raw:
        raise VaultError("vault JSON is not in canonical form (it may have been re-encoded)")
    return obj


# --- keys ---------------------------------------------------------------------------------------

def _derive(vault_key: bytes, vault_id: str, info: bytes) -> bytes:
    """One 32-byte subkey per purpose. salt=vault_id gives domain separation across vaults free."""
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=vault_id.encode("utf-8"),
                info=info).derive(vault_key)


def new_vault_key() -> bytes:
    return os.urandom(32)


def derive_name_key(vault_key: bytes, vault_id: str) -> bytes:
    """K_name exactly as a sealed pack derives it from the Vault Key.

    An open export of a previously-sealed vault must reuse THIS key: object names are
    HMAC(K_name, ...), so deriving the same K_name keeps every uid, hash, and object name across
    the flip — subscribers see an in-place mode change, not a rewrite. pack() and open_vault() call
    this too, so "exactly as sealed does" holds by construction, not by convention.
    """
    return _derive(vault_key, vault_id, b"sbvault/v1/objname")


def encode_vault_key(raw: bytes) -> str:
    """Render a Vault Key for a human to send to a friend: SBVK1-XXXX-XXXX-...

    The SBVK1- prefix is not decoration. A Vault Key would otherwise look EXACTLY like a Recovery
    Key, and a user could text their Recovery Key to a friend believing it was a vault key — handing
    over their entire brain. The import field rejects anything without this prefix.
    """
    assert len(raw) == 32, "vault key must be 32 bytes"
    b32 = base64.b32encode(raw).decode("ascii").rstrip("=")
    return _KEY_PREFIX + "-".join(b32[i:i + 4] for i in range(0, len(b32), 4))


def decode_vault_key(display: str) -> bytes:
    """Parse a Vault Key string. Refuses anything not tagged SBVK1- (see encode_vault_key)."""
    if not display or not display.strip().upper().startswith(_KEY_PREFIX):
        raise VaultError("that is not a vault key — a vault key starts with SBVK1-")
    body = display.strip().upper()[len(_KEY_PREFIX):].replace("-", "").replace(" ", "")
    try:
        raw = base64.b32decode(body + "=" * ((-len(body)) % 8))
    except Exception:
        raise VaultError("that vault key is not valid") from None
    if len(raw) != 32:
        raise VaultError("that vault key is not valid")
    return raw


def fingerprint(pubkey_b64: str) -> str:
    """What a human is actually asked to trust: SB-A3F2-9K1M-QQ4T-7ZB0. Never show a self-asserted
    publisher label instead of this — the label is decoration, the key is the identity."""
    digest = hashlib.sha256(base64.b64decode(pubkey_b64)).digest()[:10]
    fp = base64.b32encode(digest).decode("ascii").rstrip("=")[:16]
    return "SB-" + "-".join(fp[i:i + 4] for i in range(0, 16, 4))


_MAX_NAME_ECHO = 120  # bound an echoed publisher-chosen name


def sanitize_name(name: object, cap: int = _MAX_NAME_ECHO) -> str:
    """Neutralize a publisher-chosen vault NAME before it is echoed into a trust marker or the feed.

    A vault name is attacker-controlled text. Every non-printable character (newlines, control
    chars) and the bracket/quote characters (``[]'``) is replaced with a space, so a crafted name
    cannot break out of a bracketed sentinel or forge markdown structure (a fake block, a phishing
    ``[link](url)``) in the trusted UI; the length cap bounds it. Used by both the import-provenance
    line (C0) and the auto-update feed messages, so the exact same rule protects every surface."""
    assert cap > 0, "cap must be positive"
    return "".join(
        c if c.isprintable() and c not in "[]'" else " " for c in str(name)
    )[:cap]


# --- object naming + nonces ---------------------------------------------------------------------

def _obj_name(k_name: bytes, kind: bytes, uid: str, content_hash: str) -> str:
    """Content-addressed AND keyed.

    Content-addressed: the name changes iff the content changes, so an update fetches only what
    changed. Keyed: a host cannot hash a known public PDF and test whether your sealed vault
    contains it — a raw sha256 name would be exactly that oracle.
    """
    mac = hmac.new(k_name, kind + b"|" + uid.encode() + b"|" + content_hash.encode(), hashlib.sha256)
    return mac.digest()[:16].hex()


def _nonce(k_nonce: bytes, kind: bytes, uid: str, content_hash: str) -> bytes:
    """Deterministic GCM nonce.

    Nonce reuse is catastrophic across DIFFERENT plaintexts under one key. Here (uid, hash) uniquely
    determines the plaintext, so the same nonce can only ever recur with the SAME message — which is
    safe, and buys byte-reproducible exports (so an incremental publish uploads only real changes).
    """
    mac = hmac.new(k_nonce, kind + b"|" + uid.encode() + b"|" + content_hash.encode(), hashlib.sha256)
    return mac.digest()[:12]


def _doc_object(title: str, content: str, meta: dict) -> bytes:
    """Byte-for-byte the body kb._seal seals, minus the key — so an import is a straight kb.add.

    Deliberately WITHOUT tags: tags are the local user's organization, not the publisher's
    payload, so they never travel in an export (and an import starts untagged).
    """
    return canonical({"content": content, "meta": meta or {}, "title": title})


def _vec_object(vectors: list[list[float]]) -> bytes:
    """SBVEC1 | dim | chunks | reserved | float32 little-endian, chunk-major.

    The float packing is exactly what kb.put_embeddings stores, so an import is a memcpy rather than
    a re-encoding.
    """
    dim, chunks = len(vectors[0]), len(vectors)
    out = bytearray(_VEC_MAGIC + struct.pack("<HHH", dim, chunks, 0))
    for vec in vectors:
        out += struct.pack(f"<{dim}f", *vec)
    return bytes(out)


def _read_vec_object(raw: bytes) -> list[list[float]]:
    """Parse a vector object from an UNTRUSTED vault."""
    if len(raw) < 12 or raw[:6] != _VEC_MAGIC:
        raise VaultError("vector object is malformed")
    dim, chunks, _ = struct.unpack("<HHH", raw[6:12])
    if not 1 <= dim <= MAX_DIM or not 1 <= chunks <= MAX_CHUNKS:
        raise VaultError("vector object declares an out-of-range shape")
    if len(raw) != 12 + chunks * dim * 4:
        raise VaultError("vector object length does not match its header")
    out: list[list[float]] = []
    for i in range(chunks):  # bounded by MAX_CHUNKS
        start = 12 + i * dim * 4
        vec = list(struct.unpack(f"<{dim}f", raw[start:start + dim * 4]))
        # Finiteness must be an explicit error, not an assert: asserts vanish under `python -O`, and
        # kbindex._VecBlock.bulk_load does NOT re-check stored vectors. A single inf makes
        # `matrix @ q` produce NaN and ranks the WHOLE corpus at random — the one place a malicious
        # vault could silently break search.
        for x in vec:
            if x != x or x in (float("inf"), float("-inf")):
                raise VaultError("vector object contains a non-finite value")
        out.append(vec)
    return out


# --- meta validation (kb.page_for TRUSTS this list completely) ----------------------------------

_META_KEYS = ("filename", "source_url", "mime", "page_label", "pages")
_PAGE_LABELS = ("", "page", "slide", "sheet")


def _clean_meta(meta) -> dict:
    """Allowlist + validate provenance from an untrusted vault.

    This is not hygiene theatre: kb.page_for does bisect_right(pages, offset) and trusts `pages`
    completely, so a hostile list gives WRONG citations ("p.12" pointing at page 3) or a
    ten-million-int memory hit.
    """
    if not isinstance(meta, dict):
        return {}
    out: dict = {}
    for key in _META_KEYS:
        if key not in meta:
            continue
        value = meta[key]
        if key == "pages":
            if not isinstance(value, list) or len(value) > MAX_PAGES:
                raise VaultError("document page map is malformed or too long")
            pages = []
            last = -1
            for p in value:  # bounded by MAX_PAGES
                if not isinstance(p, int) or isinstance(p, bool) or p < 0 or p <= last:
                    raise VaultError("document page map must be non-negative and strictly increasing")
                last = p
                pages.append(p)
            out["pages"] = pages
        elif key == "page_label":
            out["page_label"] = value if value in _PAGE_LABELS else ""
        elif isinstance(value, str):
            out[key] = value[:2048]
    return out


# --- pack ---------------------------------------------------------------------------------------

def pack(
    *,
    store,
    vault_id: str,
    name: str,
    description: str,
    seq: int,
    docs: list[dict],
    vault_key: bytes | None = None,
    name_key: bytes | None = None,
    mode: str = SEALED,
    embed_model: str = "",
    label: str = "",
) -> bytes:
    """Build a .sbvault. ``docs`` = [{uid, title, content, meta, vectors?}].

    SEALED (default): objects and the index are AES-GCM encrypted under keys derived from
    ``vault_key``; the manifest carries the encryption params and, by the §2 metadata rule, no topic.

    OPEN: there is no Vault Key, so the index is raw canonical JSON and each object is its plaintext
    body — but the manifest is signed exactly as in sealed mode, and object names stay
    HMAC(K_name, ...). The caller passes ``name_key`` = the very K_name sealed mode derives from the
    Vault Key (and it is then published in the open manifest), so a sealed->open flip of the same
    vault yields byte-identical uids, content hashes, and object names: publishing is an UNLOCK, not a
    rewrite. A born-open vault instead supplies a persisted random ``name_key`` (else every republish
    would look like a full rewrite).
    """
    if mode not in (SEALED, OPEN):
        raise VaultError(f"unknown vault mode: {mode!r}")
    if len(docs) > MAX_VAULT_DOCS:
        raise VaultError(f"a vault holds at most {MAX_VAULT_DOCS} documents")
    if mode == SEALED:
        assert vault_key is not None, "sealed pack requires a vault key"
        cek = _derive(vault_key, vault_id, b"sbvault/v1/content")
        k_name = derive_name_key(vault_key, vault_id)
        k_nonce = _derive(vault_key, vault_id, b"sbvault/v1/nonce")
        aes = AESGCM(cek)
    else:
        assert name_key is not None and len(name_key) == 32, "open pack requires a 32-byte name_key"
        k_name = name_key

    entries: dict[str, bytes] = {}
    index_docs: list[dict] = []
    dims = set()
    for doc in docs:  # bounded by MAX_VAULT_DOCS
        uid, title = doc["uid"], doc["title"][:MAX_TITLE]
        body = _doc_object(title, doc["content"], doc.get("meta") or {})
        digest = hashlib.sha256(body).hexdigest()
        obj = _obj_name(k_name, b"doc", uid, digest)
        # Hashes and object names are over PLAINTEXT in both modes (only the stored body changes from
        # ciphertext to plaintext), which is what makes the sealed->open flip an unlock.
        entries[f"objects/{obj}.bin"] = (
            aes.encrypt(_nonce(k_nonce, b"doc", uid, digest), body,
                        b"sbvault:doc:v1|" + vault_id.encode() + b"|" + uid.encode())
            if mode == SEALED else body)
        row = {"uid": uid, "title": title, "hash": digest, "obj": obj, "bytes": len(body),
               "chunks": int(doc.get("chunks") or 1)}
        vectors = doc.get("vectors")
        if vectors:
            vbody = _vec_object(vectors)
            vdigest = hashlib.sha256(vbody).hexdigest()
            vobj = _obj_name(k_name, b"vec", uid, vdigest)
            entries[f"objects/{vobj}.bin"] = (
                aes.encrypt(_nonce(k_nonce, b"vec", uid, vdigest), vbody,
                            b"sbvault:vec:v1|" + vault_id.encode() + b"|" + uid.encode())
                if mode == SEALED else vbody)
            row["vec"] = {"obj": vobj, "hash": vdigest, "bytes": len(vbody)}
            dims.add(len(vectors[0]))
        index_docs.append(row)

    # The index plaintext is identical in both modes (so a flip leaves index.hash unchanged); sealed
    # encrypts it, open stores it raw — it is the one file a keyless reader must be able to read.
    index_raw = canonical({"format_version": FORMAT_VERSION, "vault_id": vault_id, "seq": seq,
                           "name": name, "description": description, "docs": index_docs})
    entries[_INDEX] = (
        aes.encrypt(_nonce(k_nonce, b"index", vault_id, hashlib.sha256(index_raw).hexdigest()),
                    index_raw, b"sbvault:index:v1|" + vault_id.encode())
        if mode == SEALED else index_raw)

    payload = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "requires": [],
        "vault_id": vault_id,
        "seq": seq,
        "mode": mode,
        "publisher": {
            "alg": "ed25519",
            "pubkey": identity.public_key_b64(store, identity.VAULT_PUBLISHER_SECRET),
            "label": label[:100],
        },
        "doc_count": len(index_docs),
        "index": {"hash": hashlib.sha256(index_raw).hexdigest(), "bytes": len(index_raw)},
        "embeddings": ({"model": embed_model, "dim": sorted(dims)[0],
                        "chunking": {"scheme": "sb-chunk-v1", "chunk_chars": 4000,
                                     "max_chunks": MAX_CHUNKS, "title_prefix": True}}
                       if dims and embed_model else None),
    }
    if mode == SEALED:
        # A sealed manifest carries NO name/description: a host storing your private vault should
        # learn its size and your public key, not its topic. The crypto block holds only wrap params.
        payload["crypto"] = {"alg": "AES-256-GCM", "kdf": "hkdf-sha256", "key_epoch": 1,
                             "key_wraps": [{"type": "direct"}]}
    else:
        # Open == "there is no Vault Key": the topic is public anyway, and K_name is published so
        # anyone can recompute (and thus verify) every object name.
        payload["name"] = name
        payload["description"] = description
        payload["name_key"] = base64.b64encode(name_key).decode("ascii")

    manifest_raw = canonical(payload)
    sig = identity.sign(store, _SIG_PREFIX + manifest_raw, identity.VAULT_PUBLISHER_SECRET)
    entries[_MANIFEST] = canonical({"sbvault": payload, "sig": {"alg": "ed25519", "value": sig}})

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # Deterministic order + fixed timestamps: identical content -> byte-identical file.
        for entry in [_MANIFEST, _INDEX] + sorted(k for k in entries if k.startswith("objects/")):
            info = zipfile.ZipInfo(entry, date_time=_ZIP_DATE)
            # JSON deflates well; ciphertext and float32 vectors don't compress at all, so storing
            # them is strictly cheaper than pretending otherwise.
            info.compress_type = zipfile.ZIP_DEFLATED if entry == _MANIFEST else zipfile.ZIP_STORED
            zf.writestr(info, entries[entry])
    return buf.getvalue()


# --- unpack -------------------------------------------------------------------------------------

def _entry(zf: zipfile.ZipFile, name: str, limit: int) -> bytes:
    try:
        info = zf.getinfo(name)
    except KeyError:
        raise VaultError(f"vault is missing an object it references: {name}") from None
    if info.file_size > limit:
        raise VaultError(f"{name} is larger than allowed")
    if info.compress_size and info.file_size > info.compress_size * MAX_ZIP_EXPANSION:
        raise VaultError(f"{name} expands suspiciously (possible zip bomb)")
    try:
        return zf.read(name)
    except Exception:
        # zipfile CRC-checks DURING read: a corrupted entry raises BadZipFile here, mid-extraction.
        # Hostile/damaged input must be a clean refusal (400), never an unhandled 500.
        raise VaultError(f"vault entry {name} is corrupted") from None


def _open_container(data: bytes) -> zipfile.ZipFile:
    """Open a vault archive and enforce its container shape (size, entry-name allowlist)."""
    if len(data) > MAX_VAULT_BYTES:
        raise VaultError("that vault file is too large")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception:
        raise VaultError("that file is not a vault (it is not a valid archive)") from None
    names = set(zf.namelist())
    if _MANIFEST not in names or _INDEX not in names:
        raise VaultError("that file is not a vault (no manifest)")
    for entry in names:  # bounded by the archive
        if entry not in (_MANIFEST, _INDEX) and not (
            entry.startswith("objects/") and entry.endswith(".bin") and len(entry) == 8 + 32 + 4
        ):
            raise VaultError(f"vault contains an unexpected entry: {entry!r}")
    return zf


def manifest_entry(data: bytes) -> bytes:
    """The archive's raw ``manifest.json`` bytes — for verifying a pin against the EXACT file.

    An update check must verify the signature over the very bytes the host served
    (``manifest_signed_by``), so the caller needs the entry itself, not a parsed object."""
    return _entry(_open_container(data), _MANIFEST, MAX_MANIFEST_BYTES)


def manifest_signed_by(raw: bytes, pubkey_b64: str) -> bool:
    """True iff ``raw`` (a manifest.json's bytes) is signed by ``pubkey_b64``.

    §5 step 3's primitive: an update is verified against the PINNED publisher key — never against
    the pubkey the downloaded manifest itself claims, which would make the pin decorative. A
    malformed envelope is simply "not signed by this key".
    """
    assert isinstance(pubkey_b64, str) and pubkey_b64, "pinned pubkey required"
    try:
        envelope = parse_canonical(raw)
    except VaultError:
        return False
    payload, sig = envelope.get("sbvault"), envelope.get("sig") or {}
    if not isinstance(payload, dict) or not isinstance(sig.get("value"), str):
        return False
    return identity.verify(pubkey_b64, _SIG_PREFIX + canonical(payload), sig["value"])


def read_manifest(data: bytes) -> dict:
    """Verify the container shape and the publisher SIGNATURE. No key needed — this is what a
    recipient can check before deciding whether to trust the publisher at all."""
    return read_manifest_bytes(manifest_entry(data))


def read_manifest_bytes(raw: bytes) -> dict:
    """Verify a standalone ``manifest.json`` (a tree host serves this file directly) and return
    its signed payload.

    The signature is checked against the manifest's OWN pubkey in BOTH modes — this proves the
    file is intact and really was signed by the key it names, which is all a FIRST contact can
    check (TOFU). An UPDATE must additionally verify against the pinned key via
    ``manifest_signed_by`` — trusting the embedded pubkey there would make the pin decorative.
    The ``mode`` field (itself signed) then selects which mode-specific fields must be present
    and, just as strictly, which must be ABSENT."""
    if len(raw) > MAX_MANIFEST_BYTES:
        raise VaultError("vault manifest is larger than allowed")
    envelope = parse_canonical(raw)
    payload, sig = envelope.get("sbvault"), envelope.get("sig") or {}
    if not isinstance(payload, dict) or not isinstance(sig.get("value"), str):
        raise VaultError("vault manifest is malformed")
    if payload.get("format") != FORMAT:
        raise VaultError("that file is not a vault")
    if not isinstance(payload.get("format_version"), int) or payload["format_version"] > FORMAT_VERSION:
        # Same stance as db.run_migrations' forward-compat guard: refuse rather than risk it.
        raise VaultError("this vault was made by a newer version of SmartBrain — please update")
    if payload.get("requires"):
        raise VaultError("this vault needs features this version doesn't support — please update")
    # ``publisher`` may be any signed-but-hostile value: a truthy NON-dict (string/int/list) would
    # make the ``.get`` below an AttributeError -> 500, breaking this module's own contract that a
    # signed-but-malformed file is a clean VaultError. Reject the non-dict case, then read pubkey.
    publisher = payload.get("publisher")
    if publisher is not None and not isinstance(publisher, dict):
        raise VaultError("vault manifest is malformed (publisher)")
    pubkey = (publisher or {}).get("pubkey")
    if not isinstance(pubkey, str) or not identity.verify(pubkey, _SIG_PREFIX + canonical(payload), sig["value"]):
        raise VaultError("this vault's signature is invalid — it may have been tampered with")
    # A valid signature proves WHO wrote the manifest, not that it is well-formed: a hostile
    # publisher can sign anything. Guard every field open_vault dereferences, so a signed-but-
    # malformed file is a clean refusal, never a KeyError/TypeError escaping as a 500.
    vault_id = payload.get("vault_id")
    if not isinstance(vault_id, str) or not vault_id or len(vault_id) > 100:
        raise VaultError("vault manifest is malformed (vault_id)")
    seq = payload.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise VaultError("vault manifest is malformed (seq)")
    doc_count = payload.get("doc_count")
    if not isinstance(doc_count, int) or isinstance(doc_count, bool) or doc_count < 0:
        raise VaultError("vault manifest is malformed (doc_count)")
    index_meta = payload.get("index")
    if not isinstance(index_meta, dict) or not isinstance(index_meta.get("hash"), str) \
            or len(index_meta["hash"]) != 64:
        raise VaultError("vault manifest is malformed (index)")
    mode = payload.get("mode")
    if mode == SEALED:
        # Sealed carries the wrap params and, by the §2 metadata rule, no topic: a host storing your
        # private vault must learn its size and your key, never its name.
        if not isinstance(payload.get("crypto"), dict):
            raise VaultError("this sealed vault is missing its encryption parameters")
        if any(k in payload for k in ("name", "description", "name_key")):
            raise VaultError("a sealed vault must not carry its name in the plaintext manifest")
    elif mode == OPEN:
        # Open == no Vault Key: the topic is public and K_name is published so anyone can recompute
        # every object name. There is nothing to encrypt, so a crypto block here is a contradiction —
        # refuse rather than guess which half of a mode-confused file to believe.
        if "crypto" in payload:
            raise VaultError("an open vault must not carry encryption parameters")
        name_key = payload.get("name_key")
        if not isinstance(name_key, str):
            raise VaultError("this open vault is missing its object-naming key")
        try:
            raw_name_key = base64.b64decode(name_key, validate=True)
        except Exception:
            raise VaultError("this open vault's object-naming key is not valid base64") from None
        if len(raw_name_key) != 32:
            raise VaultError("this open vault's object-naming key is the wrong length")
    else:
        raise VaultError("this vault uses a mode this version doesn't understand")
    return payload


def read_index(payload: dict, index_raw: bytes, k_name: bytes) -> list[dict]:
    """Validate a vault's PLAINTEXT index against its verified manifest; return its doc rows.

    Everything the signature transitively commits to at the index level is checked here: the
    signed index hash, canonical form, the vault_id/seq echo, doc_count, every row's shape, duplicate
    uids, and every object NAME recomputed from ``k_name`` (docs and vectors). After this, a row's
    ``obj`` provably belongs to its ``(uid, hash)`` — which is what lets a tree-host update fetch
    only the objects whose names it cannot derive from the hashes it already pinned.
    """
    assert isinstance(payload, dict), "verified manifest payload required"
    assert len(k_name) == 32, "object-naming key must be 32 bytes"
    if hashlib.sha256(index_raw).hexdigest() != payload["index"]["hash"]:
        raise VaultError("vault index does not match its signed hash")
    index = parse_canonical(index_raw)
    if index.get("vault_id") != payload["vault_id"] or index.get("seq") != payload["seq"]:
        raise VaultError("vault index disagrees with its manifest")

    rows = index.get("docs")
    if not isinstance(rows, list) or len(rows) > MAX_VAULT_DOCS or len(rows) != payload["doc_count"]:
        raise VaultError("vault index is malformed")

    seen_uids: set[str] = set()
    for row in rows:  # bounded by MAX_VAULT_DOCS
        # A signed index whose ``docs`` holds a non-dict element (e.g. ``["x"]`` with a matching
        # doc_count) would make the ``.get`` calls below an AttributeError -> 500. The row's SHAPE is
        # as untrusted as its contents, so check it first: a malformed row is a clean VaultError.
        if not isinstance(row, dict):
            raise VaultError("vault index entry is malformed")
        uid, digest, obj = row.get("uid"), row.get("hash"), row.get("obj")
        if not (isinstance(uid, str) and isinstance(digest, str) and isinstance(obj, str)):
            raise VaultError("vault index entry is malformed")
        # A uid IS the update key (§3), so two rows sharing one would double-import and make a later
        # update ambiguous. Refuse rather than silently pick one — this also hardens sealed mode.
        if uid in seen_uids:
            raise VaultError("vault index names the same document twice")
        seen_uids.add(uid)
        # Recompute the object name from K_name and refuse a mismatch. In open mode the body is
        # plaintext, so this — not GCM — is what stops a hostile file from parking a well-formed but
        # wrong object under a legitimate name; in sealed mode it is a cheap extra guard.
        if _obj_name(k_name, b"doc", uid, digest) != obj:
            raise VaultError("a vault object is misnamed")
        vec = row.get("vec")
        if isinstance(vec, dict) and isinstance(vec.get("obj"), str):
            vhash = vec.get("hash")
            if not isinstance(vhash, str) or _obj_name(k_name, b"vec", uid, vhash) != vec["obj"]:
                raise VaultError("a vault's vectors are misnamed")
    return rows


def read_doc_object(body: bytes, row: dict) -> dict:
    """Validate one document object's PLAINTEXT bytes against its (index-verified) row.

    The hash check chains the body to the signature (manifest sig -> index hash -> row hash), and
    the field guards make a signed-but-malformed document a clean refusal. The SIGNED hash rides
    along in the result: an importer pins {uid, hash} per member, and that pin must be the exact
    value future indexes carry for this uid — recomputing it locally after the title/meta
    normalisation below could silently disagree with what the publisher signed.
    """
    uid, digest = row["uid"], row["hash"]
    if hashlib.sha256(body).hexdigest() != digest:
        raise VaultError("a vault document does not match its signed hash")
    doc_obj = parse_canonical(body)
    title, content = doc_obj.get("title"), doc_obj.get("content")
    if not isinstance(title, str) or not isinstance(content, str) or len(content) > MAX_TEXT:
        raise VaultError("a vault document is malformed or too large")
    return {"uid": uid, "title": title[:MAX_TITLE] or "Untitled",
            "content": content, "meta": _clean_meta(doc_obj.get("meta")), "hash": digest}


def read_vec_body(vbody: bytes, vec: dict) -> list[list[float]]:
    """Validate one vector object's PLAINTEXT bytes against its (index-verified) vec block."""
    if hashlib.sha256(vbody).hexdigest() != vec["hash"]:
        raise VaultError("a vault's vectors do not match their signed hash")
    return _read_vec_object(vbody)


def doc_hash(title: str, content: str, meta: dict) -> str:
    """The content hash a publisher signs for a document, recomputed from a LOCAL copy.

    An update asks "does the user's copy still match what the publisher shipped?" by comparing
    this against the pinned member hash — a mismatch means the user edited it, and the update must
    keep theirs (plan decision #1). Same bytes ``pack`` hashes, by construction.
    """
    return hashlib.sha256(_doc_object(title, content, meta or {})).hexdigest()


def open_vault(data: bytes, vault_key: bytes | None = None) -> tuple[dict, list[dict]]:
    """Verify and validate a vault, decrypting it in sealed mode. Returns (manifest payload, docs).

    Every document is checked against a hash the signature transitively commits to, so a host that
    swapped, renamed, injected, or removed an object cannot go unnoticed — in BOTH modes. Open mode
    only drops the AES-GCM layer: the hash chain (manifest sig -> index hash -> per-doc hash) plus the
    object-name recomputation still catch tampering with no key at all. ``vault_key`` is therefore
    required for sealed and ignored for open.
    """
    payload = read_manifest(data)
    mode = payload["mode"]
    vault_id = payload["vault_id"]
    zf = zipfile.ZipFile(io.BytesIO(data))

    if mode == SEALED:
        if vault_key is None:
            raise VaultError("this vault is sealed — it needs a key to open")
        cek = _derive(vault_key, vault_id, b"sbvault/v1/content")
        aes = AESGCM(cek)
        k_name = derive_name_key(vault_key, vault_id)
        k_nonce = _derive(vault_key, vault_id, b"sbvault/v1/nonce")
    else:  # OPEN — no key; K_name is published in the manifest (base64-validated by read_manifest).
        aes = k_nonce = None
        k_name = base64.b64decode(payload["name_key"])

    index_ct = _entry(zf, _INDEX, MAX_INDEX_BYTES)
    if mode == SEALED:
        try:
            index_raw = aes.decrypt(
                _nonce(k_nonce, b"index", vault_id, payload["index"]["hash"]), index_ct,
                b"sbvault:index:v1|" + vault_id.encode(),
            )
        except Exception:
            raise VaultError("that key doesn't open this vault") from None
    else:
        index_raw = index_ct  # raw canonical JSON; a mode-confused ciphertext fails read_index below
    rows = read_index(payload, index_raw, k_name)

    # Surface the vault's real name/description. In sealed mode they live ONLY in the encrypted index
    # (the manifest carries no topic); in open mode they ride the plaintext manifest. Either source is
    # signed, so both are as trusted as the documents. Namespaced under "_sealed" so the importer
    # reads them the same way regardless of mode.
    src = payload if mode == OPEN else parse_canonical(index_raw)
    name, description = src.get("name"), src.get("description")
    payload["_sealed"] = {
        "name": name[:MAX_TITLE] if isinstance(name, str) else "",
        "description": description[:MAX_TITLE] if isinstance(description, str) else "",
    }

    docs: list[dict] = []
    referenced: set[str] = set()
    for row in rows:  # bounded by MAX_VAULT_DOCS (row shapes + object names verified by read_index)
        uid, digest, obj = row["uid"], row["hash"], row["obj"]
        referenced.add(f"objects/{obj}.bin")
        raw_obj = _entry(zf, f"objects/{obj}.bin", MAX_DOC_OBJECT_BYTES)
        if mode == SEALED:
            try:
                body = aes.decrypt(
                    _nonce(k_nonce, b"doc", uid, digest), raw_obj,
                    b"sbvault:doc:v1|" + vault_id.encode() + b"|" + uid.encode(),
                )
            except Exception:  # tampered ciphertext / wrong key -> a clean 400, never a raw InvalidTag
                raise VaultError("a vault document failed its integrity check — it may have been tampered with") from None
        else:
            body = raw_obj
        out = read_doc_object(body, row)
        vec = row.get("vec")
        if isinstance(vec, dict) and isinstance(vec.get("obj"), str):
            vobj, vhash = vec["obj"], vec["hash"]
            referenced.add(f"objects/{vobj}.bin")
            raw_vec = _entry(zf, f"objects/{vobj}.bin", MAX_VEC_OBJECT_BYTES)
            if mode == SEALED:
                try:
                    vbody = aes.decrypt(
                        _nonce(k_nonce, b"vec", uid, vhash), raw_vec,
                        b"sbvault:vec:v1|" + vault_id.encode() + b"|" + uid.encode(),
                    )
                except Exception:
                    raise VaultError("a vault's vectors failed their integrity check") from None
            else:
                vbody = raw_vec
            out["vectors"] = read_vec_body(vbody, vec)
        docs.append(out)

    # Every objects/* entry must be referenced by the index (§1): an unreferenced object is either a
    # smuggling channel or a sign the file was rebuilt by something that should not have been.
    stray = {n for n in zf.namelist() if n.startswith("objects/")} - referenced
    if stray:
        raise VaultError("vault contains an object its index does not reference")
    return payload, docs
