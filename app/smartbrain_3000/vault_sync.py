"""Keep a subscription up to date: CHECK a pinned vault's host for a newer version, APPLY it.

First contact (subscribe) was the trust decision; everything here is enforcement. Every byte a
host serves is untrusted, and the PIN — the publisher key, vault_id, and seq floor recorded in the
vault's encrypted body — is the only authority. The verification order is vault-format §5, exactly:

    1. format/version supported (read_manifest_bytes refuses newer versions and unknown requires);
    2. the manifest's vault_id matches the pin — else "this is a different vault";
    3. the signature verifies against the PINNED pubkey — never against the pubkey in the manifest
       we just downloaded, which would make the pin decorative;
    4. seq > pinned seq — else "no update" (equal) or a rollback refusal (lower).

A key change is REPORTED (KeyChanged, carrying the offered key), never applied: the caller blocks
the subscription and interrupts the user — the one interruption the design allows itself.

Updates are all-or-nothing (§5): everything is fetched and verified BEFORE the first database
write, and the write phase itself runs in one DuckDB transaction (KnowledgeBase and VaultStore
share the request thread's cursor, so a single BEGIN covers both stores) with the in-memory search
index dropped either way. A subscriber is never left half on seq 4 and half on seq 5.

Two host shapes (§1): a URL ending ``/manifest.json`` is a TREE host — check fetches only the
manifest, and apply fetches only the objects whose names aren't derivable from the hashes already
pinned in the member map. Anything else is a ZIP host: the whole file is refetched (the honest v1
cost, surfaced in the UI). This module owns transport policy; the container/verification
primitives stay in vault_format, which remains transport-free.
"""

from __future__ import annotations

import base64
import logging
import time
from datetime import datetime, timezone

from . import kb as kbmod
from . import netguard, vault_format
from .vaults import IMPORT, OWNER

log = logging.getLogger(__name__)

_TREE_SUFFIX = "/manifest.json"

# A tree update fetches objects one at a time, each with its own 8s timeout: the byte cap alone
# lets a host serving thousands of small-but-slow objects wedge an /update (and, under Stage E, the
# scheduler tick) for hours. This overall wall-clock budget on the fetch phase caps that — checked
# between fetches, so the update is refused BEFORE the write transaction and nothing lands.
_MAX_UPDATE_SECONDS = 120
_monotonic = time.monotonic  # module attribute so a test can drive the deadline deterministically


class SyncError(Exception):
    """An update was refused: the host serves something the pin does not allow."""


class KeyChanged(Exception):
    """The manifest is self-consistently signed — by a key that is NOT the pinned one.

    Carries the offered key so the caller can block the subscription and show BOTH fingerprints.
    Raised only after the manifest verified against its own claimed key: a manifest that matches
    neither key is tampering (a plain VaultError), not a key change.
    """

    def __init__(self, offered_pubkey: str) -> None:
        assert offered_pubkey, "offered pubkey required"
        super().__init__("the publisher's key changed")
        self.offered_pubkey = offered_pubkey


def is_tree_url(url: str) -> bool:
    """A URL ending /manifest.json is a tree host (§1): manifest-only check, per-object fetch."""
    assert url, "url required"
    return url.endswith(_TREE_SUFFIX)


def check(pin: dict) -> dict:
    """Ask the pinned URL whether a newer version exists. Verifies, in §5 order; applies NOTHING.

    Returns {manifest, blob, tree, remote_seq, pinned_seq, behind, rollback}. ``blob`` carries the
    already-fetched zip on a zip host so an apply immediately after doesn't download it twice.
    Raises KeyChanged on a key substitution, SyncError/VaultError on refusals, FetchError from the
    guard — the caller maps each to its HTTP shape.
    """
    url, pinned_key = pin.get("url"), pin.get("publisher_pubkey")
    if not url or not pinned_key or not pin.get("vault_id"):
        raise SyncError("this vault's subscription pin is incomplete — remove and re-subscribe")
    tree = is_tree_url(url)
    if tree:
        raw = netguard.safe_fetch_vault_manifest(url)
        blob = None
    else:
        blob = netguard.safe_fetch_vault(url)
        raw = vault_format.manifest_entry(blob)

    # (1) format/version/requires + envelope integrity (self-consistency) + field guards.
    payload = vault_format.read_manifest_bytes(raw)
    # (2) the same vault? A host serving a different vault_id gets "different vault" — NOT the
    # key-change flow: offering trust-publisher for a vault the pin never named would teach the
    # user to bless arbitrary substitutions.
    if payload["vault_id"] != pin["vault_id"]:
        raise SyncError("that URL now serves a different vault — refusing "
                        "(the subscription is pinned to another vault identity)")
    # (3) THE PIN. Verified over the exact bytes the host served, against the key pinned at
    # subscribe time — the manifest's own pubkey field is never consulted for this.
    if not vault_format.manifest_signed_by(raw, pinned_key):
        raise KeyChanged(payload["publisher"]["pubkey"])
    if payload["mode"] != vault_format.OPEN:
        # The publisher re-sealed the vault: a URL subscription has no key, so updates stop here.
        raise SyncError("the publisher re-sealed this vault — a URL subscription can only follow "
                        "it while it is published open")
    # (4) seq: strictly greater is an update; equal is up to date; lower is a rollback (or a
    # frozen host replaying an old file) and must be refused, never silently re-applied.
    remote_seq, pinned_seq = payload["seq"], int(pin.get("seq") or 0)
    return {
        "manifest": payload,
        "blob": blob,
        "tree": tree,
        "remote_seq": remote_seq,
        "pinned_seq": pinned_seq,
        "behind": remote_seq > pinned_seq,
        "rollback": remote_seq < pinned_seq,
    }


def fetch_open_vault(url: str) -> tuple[dict, list[dict]]:
    """Materialize a hosted OPEN vault for a first subscribe: (manifest payload, docs).

    Zip host: fetch the file, open_vault verifies everything. Tree host: fetch the manifest, then
    the index, then EVERY object — each chained to the signature exactly as open_vault chains them
    (read_manifest_bytes -> read_index -> read_doc_object/read_vec_body). At subscribe time there
    is no member map yet, so "fetch only what changed" degenerates to "fetch everything".
    """
    assert url, "url required"
    if not is_tree_url(url):
        blob = netguard.safe_fetch_vault(url)
        # v1 URL-subscribe is open-only (the plan's explicit deferral): a sealed vault's key must
        # never ride a URL, so file + separately-sent key stays the sealed channel.
        if vault_format.read_manifest(blob)["mode"] != vault_format.OPEN:
            raise vault_format.VaultError(
                "that vault is sealed — import its file with the key instead")
        return vault_format.open_vault(blob)
    payload = vault_format.read_manifest_bytes(netguard.safe_fetch_vault_manifest(url))
    if payload["mode"] != vault_format.OPEN:
        raise vault_format.VaultError(
            "that vault is sealed — import its file with the key instead")
    rows, remote = _tree_index(url, payload)
    assert len(remote) == len(rows), "index rows must be unique by uid"
    docs, total = [], 0
    for row in rows:  # bounded by MAX_VAULT_DOCS
        doc, fetched = _tree_fetch_doc(url, row, want_vectors=True)
        total = _bounded_total(total, fetched)
        docs.append(doc)
    payload["_sealed"] = {"name": str(payload.get("name") or "")[:vault_format.MAX_TITLE],
                         "description": str(payload.get("description") or "")[:vault_format.MAX_TITLE]}
    return payload, docs


# --- the tree delta path -------------------------------------------------------------------------

def _bounded_total(total: int, fetched: int) -> int:
    """Accumulate fetched bytes, refusing past the whole-vault cap.

    Per-object caps bound each fetch, but 10k maximal objects would sum far past what a single
    vault file may be — the TREE path must not admit more than the ZIP path's 512 MiB.
    """
    total += fetched
    if total > vault_format.MAX_VAULT_BYTES:
        raise SyncError("this vault's objects add up to more than a vault may hold")
    return total


def _tree_index(url: str, payload: dict) -> tuple[list[dict], dict[str, dict]]:
    """Fetch + verify a tree host's index against the (already pin-verified) manifest."""
    base = url[: -len("manifest.json")]
    index_raw = netguard.safe_fetch_vault_object(base + "index.bin", vault_format.MAX_INDEX_BYTES)
    # K_name comes from the SIGNED manifest (base64 + length validated by read_manifest_bytes), so
    # recomputed object names inherit the pin's authority.
    k_name = base64.b64decode(payload["name_key"])
    rows = vault_format.read_index(payload, index_raw, k_name)
    return rows, {row["uid"]: row for row in rows}


def _tree_fetch_doc(url: str, row: dict, *, want_vectors: bool) -> tuple[dict, int]:
    """Fetch one document (and, when asked, its vectors) from a tree host; verify both.

    Every byte is checked against the signed index before it is returned: the object name was
    already recomputed by read_index, and read_doc_object/read_vec_body enforce the content hash
    the signature commits to — a host that swaps a body under a legitimate name cannot go
    unnoticed. Returns (doc, bytes fetched) so the caller can bound the TOTAL download.
    """
    base = url[: -len("manifest.json")]
    body = netguard.safe_fetch_vault_object(
        base + f"objects/{row['obj']}.bin", vault_format.MAX_DOC_OBJECT_BYTES)
    doc = vault_format.read_doc_object(body, row)
    fetched = len(body)
    vec = row.get("vec")
    if want_vectors and isinstance(vec, dict) and isinstance(vec.get("obj"), str):
        vbody = netguard.safe_fetch_vault_object(
            base + f"objects/{vec['obj']}.bin", vault_format.MAX_VEC_OBJECT_BYTES)
        doc["vectors"] = vault_format.read_vec_body(vbody, vec)
        fetched += len(vbody)
    return doc, fetched


# --- apply ----------------------------------------------------------------------------------------

def apply(vaults, knowledge, local_id: str, pin: dict, chk: dict, embed_model: str) -> dict:
    """Apply a checked update to local vault ``local_id``; return {added, updated, deleted,
    kept_yours}. All-or-nothing: fetch + verify EVERYTHING first, then write in one transaction.
    """
    assert chk.get("behind"), "apply requires a checked, newer manifest"
    manifest = chk["manifest"]
    member_map = vaults.member_map(local_id)

    if chk["tree"]:
        new_docs, changed, remote_uids = _plan_tree(pin, manifest, member_map, embed_model)
    else:
        # open_vault re-verifies the container end to end (same bytes check() verified against the
        # pin, since it is the same blob). The full doc list is in hand — the diff just ignores
        # the unchanged ones. This is the zip host's honest cost (§1): no per-object fetch exists.
        payload, docs = vault_format.open_vault(chk["blob"])
        assert payload["vault_id"] == manifest["vault_id"] and payload["seq"] == manifest["seq"], \
            "archive must match the manifest the pin verified"
        new_docs, changed, remote_uids = _plan_docs(docs, member_map)

    return _write(vaults, knowledge, local_id, manifest, member_map,
                  new_docs, changed, remote_uids, embed_model)


def apply_from_docs(vaults, knowledge, local_id: str, manifest: dict, docs: list[dict],
                    embed_model: str) -> dict:
    """Apply an update whose documents are already fetched AND verified (a re-imported FILE whose
    vault_id matches an existing pin — §7: "imported from this vault: it's an update, not an
    import"). The caller has already run §5 steps 1-4 against the pin."""
    member_map = vaults.member_map(local_id)
    new_docs, changed, remote_uids = _plan_docs(docs, member_map)
    return _write(vaults, knowledge, local_id, manifest, member_map,
                  new_docs, changed, remote_uids, embed_model)


def _plan_docs(docs: list[dict], member_map: dict) -> tuple[list[dict], list[tuple], set[str]]:
    """Diff fully-materialized docs (zip/file path) against the pinned member map."""
    new_docs, changed = [], []
    for doc in docs:  # bounded by MAX_VAULT_DOCS
        known = member_map.get(doc["uid"])
        if known is None:
            new_docs.append(doc)
        elif doc["hash"] != known["hash"]:
            changed.append((doc["uid"], doc))
    return new_docs, changed, {doc["uid"] for doc in docs}


def _plan_tree(pin: dict, manifest: dict, member_map: dict, embed_model: str
               ) -> tuple[list[dict], list[tuple], set[str]]:
    """Diff a tree host's index against the member map, fetching ONLY what the diff needs.

    An unchanged row's object name is derivable from the pinned {uid, hash} (read_index proved
    obj == HMAC(K_name, uid|hash)), so equal hashes mean the object on the host is byte-identical
    to what already landed — it is never downloaded. Owner-origin rows are the user's (the write
    phase will skip them whatever upstream did), so their bodies are never downloaded either.
    Vectors ride only when the publisher embedded with OUR model; on a tree host a subscriber on a
    different model never downloads them at all (§6).
    """
    rows, remote = _tree_index(pin["url"], manifest)
    want_vectors = (manifest.get("embeddings") or {}).get("model") == embed_model
    new_docs, changed = [], []
    count = total = 0
    deadline = _monotonic() + _MAX_UPDATE_SECONDS
    for uid, row in remote.items():  # bounded by MAX_VAULT_DOCS
        known = member_map.get(uid)
        if known is not None and known["hash"] == row["hash"]:
            continue  # unchanged — the whole point of the tree host
        if known is not None and known["origin"] != IMPORT:
            changed.append((uid, None))  # the user's copy: skipped later, so never fetched
            continue
        # An overall budget on top of each fetch's own 8s timeout: a host drip-feeding thousands of
        # slow objects would otherwise stay under the byte cap yet run for hours. Checked BEFORE the
        # fetch, so tripping it means nothing is written (planning precedes the write transaction).
        if _monotonic() > deadline:
            raise SyncError("this update took too long — the host may be unreachable; try again")
        doc, fetched = _tree_fetch_doc(pin["url"], row, want_vectors=want_vectors)
        count += 1
        total = _bounded_total(total, fetched)
        if known is None:
            new_docs.append(doc)
        else:
            changed.append((uid, doc))
    log.info("tree update for vault %s: %d of %d objects fetched",
             manifest["vault_id"], count, len(rows))
    return new_docs, changed, set(remote)


def _landed_hash(doc: dict) -> str:
    """The hash of a LOCAL doc dict {title, content, meta} — recorded beside the publisher's SIGNED
    hash as the owner-edit baseline. read_doc_object and kb.get produce the same normalized fields,
    so this equals what _apply_changes recomputes from kb.get on the next update: the value that
    lets normalization-on-landing stop masquerading as a user edit."""
    return vault_format.doc_hash(doc["title"], doc["content"], doc.get("meta"))


def _maybe_vectors(knowledge, doc_id: str, doc: dict, vectors_ok: bool, embed_model: str) -> None:
    """Adopt shipped vectors only under the exact same gate the import path applies: same model,
    same chunk count (vectors chunked differently give WRONG page citations, not worse ranking)."""
    vectors = doc.get("vectors")
    if (vectors and vectors_ok
            and len(vectors) == len(kbmod.chunk_text(doc["title"], doc["content"]))):
        knowledge.put_embeddings(doc_id, vectors, embed_model)


def _land_new(vaults, knowledge, local_id: str, new_docs: list[dict], vectors_ok: bool,
              embed_model: str) -> tuple[int, int]:
    """New uids -> land them; returns (added, kept).

    Dedupe keeps the USER's document (never overwrite something they authored with a stranger's
    copy) and records the membership owner-origin — the same never-clobber rule the import path
    applies.
    """
    added = kept = 0
    for doc in new_docs:  # bounded by MAX_VAULT_DOCS
        existing = knowledge.find_duplicate(doc["content"])
        if existing is not None:
            # Dedupe kept the USER's copy — the landed baseline is THAT doc, not the publisher's.
            vaults.add_documents(local_id, [existing], origin=OWNER)
            vaults.note_member_source(local_id, existing, doc["uid"], doc["hash"],
                                      _landed_hash(knowledge.get(existing)))
            kept += 1
            continue
        doc_id = knowledge.add(doc["title"], doc["content"], doc["meta"])
        vaults.add_documents(local_id, [doc_id], origin=IMPORT)
        vaults.note_member_source(local_id, doc_id, doc["uid"], doc["hash"], _landed_hash(doc))
        _maybe_vectors(knowledge, doc_id, doc, vectors_ok, embed_model)
        added += 1
    return added, kept


def _apply_changes(vaults, knowledge, local_id: str, member_map: dict, changed: list[tuple],
                   vectors_ok: bool, embed_model: str) -> tuple[int, int]:
    """Changed uids -> kb.replace IN PLACE (the doc_id survives); returns (updated, kept).

    The owner-edit guard (plan decision #1): before replacing, the LOCAL copy must still hash to
    what we LANDED (member.landed_hash), not to the publisher's signed hash. Those two differ for
    any doc that normalized on the way in (a >2048-char source_url, an empty title, an extra meta
    key) — comparing against the signed hash would misread such a doc as user-edited and detach it,
    silently suppressing every future update for it. A genuine mismatch means the user really edited
    it — it is THEIRS now, so it is skipped and its origin flips to owner, which makes every future
    update skip it too.
    """
    updated = kept = 0
    for uid, doc in changed:  # bounded by MAX_VAULT_DOCS
        member = member_map[uid]
        if member["origin"] != IMPORT:
            kept += 1  # the user's own copy — never the publisher's to touch (§7)
            continue
        assert doc is not None, "an import-origin change must have been fetched"
        local = knowledge.get(member["doc_id"])
        if local is None:
            continue  # the membership outlived its document — nothing to replace
        current = vault_format.doc_hash(local["title"], local["content"], local.get("meta"))
        landed = member.get("landed_hash")
        # Back-compat: members pinned before landed_hash existed (#77, already on main) have none.
        # Recomputing against the signed hash would false-detach every normalized doc, so give a
        # legacy member the benefit of the doubt: treat its current local doc AS the baseline
        # (adopt it), and note_member_source below records a real landed_hash going forward.
        if landed is None:
            landed = current
        if current != landed:
            vaults.detach(local_id, member["doc_id"])
            kept += 1
            continue
        knowledge.replace(member["doc_id"], doc["title"], doc["content"], doc["meta"])
        vaults.note_member_source(local_id, member["doc_id"], uid, doc["hash"], _landed_hash(doc))
        _maybe_vectors(knowledge, member["doc_id"], doc, vectors_ok, embed_model)
        updated += 1
    return updated, kept


def _apply_deletions(vaults, knowledge, local_id: str, member_map: dict,
                     remote_uids: set[str]) -> tuple[int, int]:
    """Uids gone upstream -> delete vault-owned copies only; returns (deleted, kept).

    Deletions are implicit (§3): present before, absent at this seq. Stale uids are grouped by
    the local doc they map to — dedupe means one doc can carry several uids, and it may only be
    deleted when NO surviving uid still ships it. An owner-origin doc is never deleted; its stale
    upstream source is pruned so later updates stop re-diffing it.
    """
    deleted = kept = 0
    stale_by_doc: dict[str, list[str]] = {}
    alive: set[str] = set()  # docs some SURVIVING uid still ships
    for uid, member in member_map.items():  # bounded by the member map
        if uid in remote_uids:
            alive.add(member["doc_id"])
        else:
            stale_by_doc.setdefault(member["doc_id"], []).append(uid)
    for doc_id, uids in stale_by_doc.items():  # bounded by the member map
        for uid in uids:
            vaults.forget_member_source(local_id, doc_id, uid)
        if doc_id in alive:
            continue  # another upstream uid still ships this document
        if member_map[uids[0]]["origin"] != IMPORT:
            kept += 1  # the user's document merely also sat in this vault — never deleted
            continue
        knowledge.delete(doc_id)
        vaults.forget_document(doc_id)
        deleted += 1
    return deleted, kept


def _write(vaults, knowledge, local_id: str, manifest: dict, member_map: dict,
           new_docs: list[dict], changed: list[tuple], remote_uids: set[str],
           embed_model: str) -> dict:
    """The write phase: one transaction, or nothing.

    Everything entering here is verified; the three cases (_land_new / _apply_changes /
    _apply_deletions) are local policy (§7 + plan decision #1).

    Explicit transactions are not the house pattern (stores autocommit), but an in-place update
    cannot be compensated the way subscribe's rollback deletes freshly-minted rows — restoring
    overwritten bodies would need a shadow copy of the corpus. KnowledgeBase and VaultStore share
    the request thread's cursor (both are built over app.state.dbx), so one BEGIN covers every
    write both stores make here; the in-memory index is dropped on BOTH outcomes so it can never
    outlive a rollback.
    """
    shipped = manifest.get("embeddings") or {}
    vectors_ok = bool(shipped.get("model")) and shipped.get("model") == embed_model
    conn = knowledge.conn
    conn.execute("BEGIN TRANSACTION;")
    try:
        added, kept_new = _land_new(vaults, knowledge, local_id, new_docs, vectors_ok, embed_model)
        updated, kept_changed = _apply_changes(vaults, knowledge, local_id, member_map, changed,
                                               vectors_ok, embed_model)
        deleted, kept_gone = _apply_deletions(vaults, knowledge, local_id, member_map, remote_uids)

        # The bound is on the vault AFTER the update (owner-added members count too), checked
        # inside the transaction so exceeding it costs nothing.
        if vaults.count_documents(local_id) > vault_format.MAX_VAULT_DOCS:
            raise SyncError(
                f"this update would leave the vault over {vault_format.MAX_VAULT_DOCS} documents")

        # Re-pin INSIDE the transaction: the seq floor and the documents move together, or not at
        # all — a pin at seq 5 over seq-4 documents would refuse the very update that fixes it.
        vaults.update_source(local_id, {
            "seq": manifest["seq"],
            "last_checked": datetime.now(timezone.utc).date().isoformat(),
        })
        conn.execute("COMMIT;")
    except Exception:
        try:
            conn.execute("ROLLBACK;")
        except Exception:  # already aborted — the failed txn is discarded either way
            log.warning("rollback after failed vault update was itself refused", exc_info=True)
        raise
    finally:
        # One drop, both outcomes: success rebuilt lazily in a single bulk_load (the O(n^2)
        # incremental path is the one kbindex warns about), failure discards in-memory state the
        # rolled-back rows never committed.
        knowledge.reset_index()
    return {"added": added, "updated": updated, "deleted": deleted,
            "kept_yours": kept_new + kept_changed + kept_gone}
