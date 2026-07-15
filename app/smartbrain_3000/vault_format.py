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
    """Byte-for-byte the body kb._seal seals, minus the key — so an import is a straight kb.add."""
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
    vault_key: bytes,
    embed_model: str = "",
    label: str = "",
) -> bytes:
    """Build a SEALED .sbvault. ``docs`` = [{uid, title, content, meta, vectors?}].

    Objects and the index are encrypted under keys derived from the Vault Key; the manifest is
    plaintext (it is what a reader parses before it has any key) and SIGNED by the publisher.
    """
    if len(docs) > MAX_VAULT_DOCS:
        raise VaultError(f"a vault holds at most {MAX_VAULT_DOCS} documents")
    cek = _derive(vault_key, vault_id, b"sbvault/v1/content")
    k_name = _derive(vault_key, vault_id, b"sbvault/v1/objname")
    k_nonce = _derive(vault_key, vault_id, b"sbvault/v1/nonce")
    aes = AESGCM(cek)

    entries: dict[str, bytes] = {}
    index_docs: list[dict] = []
    dims = set()
    for doc in docs:  # bounded by MAX_VAULT_DOCS
        uid, title = doc["uid"], doc["title"][:MAX_TITLE]
        body = _doc_object(title, doc["content"], doc.get("meta") or {})
        digest = hashlib.sha256(body).hexdigest()
        obj = _obj_name(k_name, b"doc", uid, digest)
        entries[f"objects/{obj}.bin"] = aes.encrypt(
            _nonce(k_nonce, b"doc", uid, digest), body,
            b"sbvault:doc:v1|" + vault_id.encode() + b"|" + uid.encode(),
        )
        row = {"uid": uid, "title": title, "hash": digest, "obj": obj, "bytes": len(body),
               "chunks": int(doc.get("chunks") or 1)}
        vectors = doc.get("vectors")
        if vectors:
            vbody = _vec_object(vectors)
            vdigest = hashlib.sha256(vbody).hexdigest()
            vobj = _obj_name(k_name, b"vec", uid, vdigest)
            entries[f"objects/{vobj}.bin"] = aes.encrypt(
                _nonce(k_nonce, b"vec", uid, vdigest), vbody,
                b"sbvault:vec:v1|" + vault_id.encode() + b"|" + uid.encode(),
            )
            row["vec"] = {"obj": vobj, "hash": vdigest, "bytes": len(vbody)}
            dims.add(len(vectors[0]))
        index_docs.append(row)

    index_raw = canonical({"format_version": FORMAT_VERSION, "vault_id": vault_id, "seq": seq,
                           "name": name, "description": description, "docs": index_docs})
    entries[_INDEX] = aes.encrypt(
        _nonce(k_nonce, b"index", vault_id, hashlib.sha256(index_raw).hexdigest()), index_raw,
        b"sbvault:index:v1|" + vault_id.encode(),
    )

    payload = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "requires": [],
        "vault_id": vault_id,
        "seq": seq,
        "mode": SEALED,
        "publisher": {
            "alg": "ed25519",
            "pubkey": identity.public_key_b64(store, identity.VAULT_PUBLISHER_SECRET),
            "label": label[:100],
        },
        "doc_count": len(index_docs),
        "index": {"hash": hashlib.sha256(index_raw).hexdigest(), "bytes": len(index_raw)},
        # A sealed manifest carries NO name/description: a host storing your private vault should
        # learn its size and your public key, not its topic.
        "crypto": {"alg": "AES-256-GCM", "kdf": "hkdf-sha256", "key_epoch": 1,
                   "key_wraps": [{"type": "direct"}]},
        "embeddings": ({"model": embed_model, "dim": sorted(dims)[0],
                        "chunking": {"scheme": "sb-chunk-v1", "chunk_chars": 4000,
                                     "max_chunks": MAX_CHUNKS, "title_prefix": True}}
                       if dims and embed_model else None),
    }
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


def read_manifest(data: bytes) -> dict:
    """Verify the container shape and the publisher SIGNATURE. No key needed — this is what a
    recipient can check before deciding whether to trust the publisher at all."""
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

    envelope = parse_canonical(_entry(zf, _MANIFEST, MAX_MANIFEST_BYTES))
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
    pubkey = (payload.get("publisher") or {}).get("pubkey")
    if not isinstance(pubkey, str) or not identity.verify(pubkey, _SIG_PREFIX + canonical(payload), sig["value"]):
        raise VaultError("this vault's signature is invalid — it may have been tampered with")
    if payload.get("mode") != SEALED:
        raise VaultError("only sealed vaults can be opened by this version")
    return payload


def open_vault(data: bytes, vault_key: bytes) -> tuple[dict, list[dict]]:
    """Verify, decrypt, and validate a vault. Returns (manifest payload, documents).

    Every document is checked against a hash that the signature transitively commits to, so a host
    that swapped an object cannot go unnoticed.
    """
    payload = read_manifest(data)
    vault_id = payload["vault_id"]
    cek = _derive(vault_key, vault_id, b"sbvault/v1/content")
    aes = AESGCM(cek)
    zf = zipfile.ZipFile(io.BytesIO(data))

    index_ct = _entry(zf, _INDEX, MAX_INDEX_BYTES)
    k_nonce = _derive(vault_key, vault_id, b"sbvault/v1/nonce")
    try:
        index_raw = aes.decrypt(
            _nonce(k_nonce, b"index", vault_id, payload["index"]["hash"]), index_ct,
            b"sbvault:index:v1|" + vault_id.encode(),
        )
    except Exception:
        raise VaultError("that key doesn't open this vault") from None
    if hashlib.sha256(index_raw).hexdigest() != payload["index"]["hash"]:
        raise VaultError("vault index does not match its signed hash")
    index = parse_canonical(index_raw)
    if index.get("vault_id") != vault_id or index.get("seq") != payload["seq"]:
        raise VaultError("vault index disagrees with its manifest")

    rows = index.get("docs")
    if not isinstance(rows, list) or len(rows) > MAX_VAULT_DOCS or len(rows) != payload["doc_count"]:
        raise VaultError("vault index is malformed")

    # Surface the vault's real name/description — they live ONLY in the encrypted index (the sealed
    # manifest deliberately carries no topic), and the index hash is signed, so these are as trusted
    # as the documents. Namespaced under "_sealed" so they can't be confused with plaintext fields.
    name = index.get("name")
    description = index.get("description")
    payload["_sealed"] = {
        "name": name[:MAX_TITLE] if isinstance(name, str) else "",
        "description": description[:MAX_TITLE] if isinstance(description, str) else "",
    }

    docs: list[dict] = []
    for row in rows:  # bounded by MAX_VAULT_DOCS
        uid, digest, obj = row.get("uid"), row.get("hash"), row.get("obj")
        if not (isinstance(uid, str) and isinstance(digest, str) and isinstance(obj, str)):
            raise VaultError("vault index entry is malformed")
        try:
            body = aes.decrypt(
                _nonce(k_nonce, b"doc", uid, digest),
                _entry(zf, f"objects/{obj}.bin", MAX_DOC_OBJECT_BYTES),
                b"sbvault:doc:v1|" + vault_id.encode() + b"|" + uid.encode(),
            )
        except VaultError:
            raise
        except Exception:  # tampered ciphertext / wrong key -> a clean 400, never a raw InvalidTag
            raise VaultError("a vault document failed its integrity check — it may have been tampered with") from None
        if hashlib.sha256(body).hexdigest() != digest:
            raise VaultError("a vault document does not match its signed hash")
        doc_obj = parse_canonical(body)
        title, content = doc_obj.get("title"), doc_obj.get("content")
        if not isinstance(title, str) or not isinstance(content, str) or len(content) > MAX_TEXT:
            raise VaultError("a vault document is malformed or too large")
        out = {"uid": uid, "title": title[:MAX_TITLE] or "Untitled",
               "content": content, "meta": _clean_meta(doc_obj.get("meta"))}
        vec = row.get("vec")
        if isinstance(vec, dict) and isinstance(vec.get("obj"), str):
            try:
                vbody = aes.decrypt(
                    _nonce(k_nonce, b"vec", uid, vec["hash"]),
                    _entry(zf, f"objects/{vec['obj']}.bin", MAX_VEC_OBJECT_BYTES),
                    b"sbvault:vec:v1|" + vault_id.encode() + b"|" + uid.encode(),
                )
            except VaultError:
                raise
            except Exception:
                raise VaultError("a vault's vectors failed their integrity check") from None
            if hashlib.sha256(vbody).hexdigest() != vec["hash"]:
                raise VaultError("a vault's vectors do not match their signed hash")
            out["vectors"] = _read_vec_object(vbody)
        docs.append(out)
    return payload, docs
