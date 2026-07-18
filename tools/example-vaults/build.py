#!/usr/bin/env python3
"""Build the official example vault (landing/vaults/smartbrain-docs.sbvault) from docs/.

Runs a dedicated local "publisher" SmartBrain instance in Docker, syncs docs/*.md into it,
and exports the "SmartBrain Docs" vault in OPEN mode. The publisher's Ed25519 signing key
lives in the ``sb_publisher_data`` Docker volume — subscribers PIN that key, so the volume
must be kept: deleting it orphans every subscriber (updates would look like key-change
tampering and be blocked). Passphrase rotation is safe (it re-wraps the master key, the
publisher key is unchanged): use the app's Change passphrase against this instance.

Usage:
    SB_PUBLISHER_PASS=<passphrase> python3 tools/example-vaults/build.py

First run mints the instance and PRINTS THE RECOVERY KEY ONCE — save it. Re-runs unlock,
re-sync changed docs, and export the next version (subscribers auto-pick up the delta).
Stdlib-only on purpose: runs on any machine with Python 3 and Docker.
"""

import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

IMAGE = "ghcr.io/securecloudgroup/smartbrain_3000:latest"
CONTAINER = "sb_vault_builder"
VOLUME = "sb_publisher_data"
PORT = 34500
BASE = f"http://127.0.0.1:{PORT}"
VAULT_NAME = "SmartBrain Docs"
VAULT_DESC = "The official SmartBrain_3000 user guide — searchable, kept up to date by the project."

REPO = pathlib.Path(__file__).resolve().parents[2]
DOCS = sorted(REPO.glob("docs/0*.md"))  # the numbered user guide only, never docs/internal/
OUT = REPO / "landing" / "vaults" / "smartbrain-docs.sbvault"


def api(method: str, path: str, body: dict | None = None, raw: bool = False):
    """One JSON call against the builder instance; binary response when raw=True."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json", "X-SB-Local": "1"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = resp.read()
    return payload if raw else (json.loads(payload) if payload else {})


def main() -> int:
    passphrase = os.environ.get("SB_PUBLISHER_PASS")
    if not passphrase:
        print("Set SB_PUBLISHER_PASS (the publisher instance's passphrase).", file=sys.stderr)
        return 2
    if not DOCS:
        print("No docs/0*.md found — run from the repo.", file=sys.stderr)
        return 2

    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", CONTAINER,
         "-e", f"SMARTBRAIN_PORT={PORT}", "-e", "SMARTBRAIN_HOST=0.0.0.0",
         "-p", f"127.0.0.1:{PORT}:{PORT}", "-v", f"{VOLUME}:/app/data", IMAGE],
        check=True,
    )
    try:
        for _ in range(60):
            try:
                api("GET", "/api/health")
                break
            except (urllib.error.URLError, ConnectionError):
                time.sleep(1)
        else:
            print("builder instance never became healthy", file=sys.stderr)
            return 1

        if api("GET", "/api/account/status")["initialized"]:
            api("POST", "/api/account/unlock", {"passphrase": passphrase})
        else:
            kit = api("POST", "/api/account/setup", {"passphrase": passphrase})
            print("=" * 72)
            print("NEW publisher instance minted. SAVE THIS RECOVERY KEY (shown once):")
            print("   ", kit["recovery_key"])
            print("=" * 72)

        # Sync docs: title == filename. No content-update endpoint exists, so a changed
        # doc is delete + re-add; the vault attach below re-links the fresh ids.
        existing = {d["title"]: d["id"] for d in api("GET", "/api/kb")["documents"]}
        doc_ids = []
        for path in DOCS:
            if path.name in existing:
                api("DELETE", f"/api/kb/{existing[path.name]}")
            doc_ids.append(api("POST", "/api/kb", {
                "title": path.name, "content": path.read_text(encoding="utf-8"),
            })["id"])
        print(f"synced {len(doc_ids)} docs into the publisher instance")

        vaults = api("GET", "/api/vaults")["vaults"]
        vault = next((v for v in vaults if v["name"] == VAULT_NAME), None)
        if vault is None:
            vault = api("POST", "/api/vaults", {"name": VAULT_NAME, "description": VAULT_DESC})
        api("POST", f"/api/vaults/{vault['id']}/documents", {"doc_ids": doc_ids})

        blob = api("POST", f"/api/vaults/{vault['id']}/export",
                   {"passphrase": passphrase, "mode": "open", "include_vectors": True},
                   raw=True)
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_bytes(blob)

        published = next(v for v in api("GET", "/api/vaults")["vaults"] if v["id"] == vault["id"])
        print(f"wrote {OUT.relative_to(REPO)} ({len(blob):,} bytes)")
        print(f"publisher fingerprint (what subscribers pin): {published.get('publisher_fingerprint')}")
        return 0
    finally:
        subprocess.run(["docker", "stop", CONTAINER], capture_output=True)


if __name__ == "__main__":
    sys.exit(main())
