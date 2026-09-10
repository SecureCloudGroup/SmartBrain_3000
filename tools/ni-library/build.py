#!/usr/bin/env python3
"""Build + sign a Neural Interface template pack (docs/internal/ni-format.md §19).

Reads one template JSON per file from a directory, assembles them into the §19 pack
shape, signs the canonical payload with the ``ni:publisher_ed25519`` key (loaded from a
publisher SecretStore), and writes the signed envelope to an output file.

Offline signing (the one supported v1 mode): load the publisher key straight from a
SecretStore backed by an in-memory master key you provide via SB_PUBLISHER_MASTER_KEY
(32 raw bytes base64). Same crypto as the running app — no docker, no --docker flag.
Intended for scripted publishing.

Usage:
    python tools/ni-library/build.py \\
        --templates <dir>   \\
        --pack-id  <uuid>   \\
        --seq      3        \\
        --publisher-data-dir <path>   \\
        --out landing/ni/library-pack.json

Stdlib + app imports only. Fails loudly on any shape drift — the pack must round-trip
through ``ni_library.parse_pack``, which is the same validator subscribers run.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import pathlib
import sys
from typing import Any

# The app package must be importable — add repo/app to sys.path.
_REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "app"))

from smartbrain_3000 import identity, ni_library, vault_format


def _read_templates(directory: pathlib.Path) -> list[dict[str, Any]]:
    """Read every *.json file in ``directory`` (sorted) and return them as parsed dicts.

    Order matters: pack canonical bytes are byte-identical only when input order is
    deterministic. We sort by filename so a repo-committed template set produces the
    same signed pack every build.
    """
    assert directory.is_dir(), f"templates directory missing: {directory}"
    files = sorted(directory.glob("*.json"))
    assert files, f"no *.json templates in {directory}"
    out: list[dict[str, Any]] = []
    for path in files:  # bounded by the operator's directory listing
        raw = path.read_text(encoding="utf-8")
        assert raw, f"template file is empty: {path}"
        try:
            body = json.loads(raw)
        except ValueError as exc:
            raise SystemExit(f"{path}: not valid JSON: {exc}") from None
        assert isinstance(body, dict), f"{path}: template must be a JSON object"
        out.append(body)
    return out


def _load_publisher_secret_store(publisher_data_dir: pathlib.Path):
    """Open a SecretStore against the publisher instance's DuckDB, using an in-env
    master key. Zero coupling to the running app — this is a one-shot scripted publish.

    SB_PUBLISHER_MASTER_KEY = base64-encoded 32 raw bytes. The operator persists this
    OUT OF BAND (it's the moral equivalent of a signing key file); a compromise leaks
    the pack signing capability. Rotation = unlock the publisher instance and mint a
    new NI publisher key (identity._load_or_create is idempotent).
    """
    import duckdb  # local import — keep build.py's top-level footprint stdlib-shaped
    from smartbrain_3000 import db as dbmod
    from smartbrain_3000.secrets import SecretStore

    assert publisher_data_dir.exists(), f"publisher data dir missing: {publisher_data_dir}"
    key_b64 = os.environ.get("SB_PUBLISHER_MASTER_KEY")
    if not key_b64:
        raise SystemExit(
            "Set SB_PUBLISHER_MASTER_KEY (base64 of the publisher master key). "
            "This is the OFFLINE path — the DOCKER path is documented in the README.")
    master_key = base64.b64decode(key_b64)
    if len(master_key) != 32:
        raise SystemExit("SB_PUBLISHER_MASTER_KEY must decode to 32 raw bytes")
    conn = duckdb.connect(str(publisher_data_dir / "smartbrain.duckdb"))
    dbmod.run_migrations(conn)
    return SecretStore(conn, master_key)


def _assemble_pack(pack_id: str, seq: int, label: str, templates: list[dict]) -> dict:
    """Build the §19 payload dict (WITHOUT the sig envelope — signing runs on canonical(payload))."""
    assert pack_id and seq >= 1, "pack_id + positive seq required"
    assert isinstance(label, str), "label must be a string"
    assert isinstance(templates, list) and templates, "templates required"
    return {
        "version": 1,
        "pack_id": pack_id,
        "seq": int(seq),
        "published_at": datetime.datetime.now(datetime.UTC).date().isoformat(),
        "publisher": {"label": label, "pubkey": "<filled below>"},
        "templates": templates,
    }


def _sign_pack(payload: dict, secrets_store) -> dict:
    """Fill publisher.pubkey with the loaded key, sign canonical(payload), return the envelope."""
    assert isinstance(payload, dict), "payload must be a dict"
    assert secrets_store is not None, "secrets_store required"
    pubkey = identity.public_key_b64(secrets_store, identity.NI_PUBLISHER_SECRET)
    payload["publisher"]["pubkey"] = pubkey
    signed_bytes = ni_library.PACK_SIG_PREFIX + vault_format.canonical(payload)
    signature = identity.sign(secrets_store, signed_bytes, identity.NI_PUBLISHER_SECRET)
    envelope = {"sb_ni_pack": payload,
                "sig": {"alg": "ed25519", "value": signature}}
    return envelope


def _validate_roundtrip(envelope: dict) -> bytes:
    """Serialize the envelope canonically and round-trip through parse_pack.

    The subscriber's parse_pack rejects anything malformed BEFORE checking the
    signature — running it here means a broken pack fails at build time, not on the
    first subscriber's fetch.
    """
    assert isinstance(envelope, dict), "envelope must be a dict"
    raw = vault_format.canonical(envelope)
    ni_library.parse_pack(raw)  # raises LibraryError on drift
    return raw


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a Neural Interface template pack (§19).")
    parser.add_argument("--templates", required=True, type=pathlib.Path,
                        help="Directory of *.json template files")
    parser.add_argument("--pack-id", required=True,
                        help="Stable UUID identifying this pack across seq bumps")
    parser.add_argument("--seq", required=True, type=int,
                        help="Monotonic sequence number (bump on every publish)")
    parser.add_argument("--publisher-data-dir", required=True, type=pathlib.Path,
                        help="Path to the publisher instance's data directory")
    parser.add_argument("--label", default="SmartBrain project",
                        help="Publisher display label (fingerprint is the identity)")
    parser.add_argument("--out", required=True, type=pathlib.Path,
                        help="Output pack file (canonical JSON envelope)")
    args = parser.parse_args()
    templates = _read_templates(args.templates)
    secrets = _load_publisher_secret_store(args.publisher_data_dir)
    payload = _assemble_pack(args.pack_id, args.seq, args.label, templates)
    envelope = _sign_pack(payload, secrets)
    raw = _validate_roundtrip(envelope)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(raw)
    pubkey = envelope["sb_ni_pack"]["publisher"]["pubkey"]
    try:
        display = args.out.relative_to(_REPO)
    except ValueError:  # --out is outside the repo (tests / operator's own paths)
        display = args.out
    print(f"wrote {display} ({len(raw):,} bytes)")
    print(f"publisher fingerprint (what subscribers pin): {vault_format.fingerprint(pubkey)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
