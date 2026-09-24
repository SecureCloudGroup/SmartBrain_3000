#!/usr/bin/env python3
"""Build the official example vault (landing/vaults/smartbrain-docs.sbvault) from docs/.

Runs a dedicated local "publisher" SmartBrain instance in Docker, syncs docs/*.md into it,
and exports the "SmartBrain Docs" vault in OPEN mode. The publisher's Ed25519 signing key
lives in the ``sb_publisher_data`` Docker volume — subscribers PIN that key, so the volume
must be kept: deleting it orphans every subscriber (updates would look like key-change
tampering and be blocked). Passphrase rotation is safe (it re-wraps the master key, the
publisher key is unchanged): use the app's Change passphrase against this instance.

Because that instance holds the signing key, its image is BUILT from this checkout's HEAD,
never pulled: HEAD must be on origin/main (merged, reviewed code), and app/ and docs/ must
match it, so the vault is exactly one commit's docs exported by that commit's code.

Usage:
    SB_PUBLISHER_PASS=<passphrase> python3 tools/example-vaults/build.py

First run mints the instance and PRINTS THE RECOVERY KEY ONCE — save it. Re-runs unlock,
re-sync changed docs, and export the next version (subscribers auto-pick up the delta).
Stdlib-only on purpose: runs on any machine with Python 3, git and Docker.
"""

import datetime
import json
import os
import pathlib
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Built from HEAD by build_image(), never pulled: a registry tag can move (a swapped
# `:latest` would run with the signing key), and `docker run` never refreshes a cached one
# (the vault was exported by a months-old v0.8.23 image until 2026-09-24).
IMAGE = "smartbrain-vault-builder:local"
_LABEL = "com.securecloudgroup.smartbrain.vault-builder=1"  # scopes the old-image cleanup
CONTAINER = "sb_vault_builder"
VOLUME = "sb_publisher_data"
PORT = 34500
BASE = f"http://127.0.0.1:{PORT}"
# R14: the builder's container gets its local API token from us, and every call presents
# it (Desktop authority — export is Desktop-only).
TOKEN = secrets.token_urlsafe(32)
VAULT_NAME = "SmartBrain Docs"
# The description now travels to subscribers (the publisher's own description propagates on
# every update — no longer overwritten by a generic "Public vault · publisher …"), so the
# visible line carries the build date. That way a subscriber can see at a glance how current
# the guide they're reading is even before they open a document. UTC to match the rest of the
# publish-date plumbing (manifest stamps are UTC calendar dates).
_TODAY = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
VAULT_DESC = (
    "The official SmartBrain_3000 user guide — searchable, kept up to date by the project. "
    f"Updated {_TODAY}."
)

REPO = pathlib.Path(__file__).resolve().parents[2]
DOCS = sorted(REPO.glob("docs/0*.md"))  # the numbered user guide only, never docs/internal/
OUT = REPO / "landing" / "vaults" / "smartbrain-docs.sbvault"


def api(method: str, path: str, body: dict | None = None, raw: bool = False):
    """One JSON call against the builder instance; binary response when raw=True."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = resp.read()
    return payload if raw else (json.loads(payload) if payload else {})


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=False)


def build_image() -> bool:
    """Build IMAGE from HEAD's committed tree; False (reason printed) when refused or failed."""
    sha = _git("rev-parse", "HEAD").stdout.strip()
    if not sha or _git("merge-base", "--is-ancestor", sha, "origin/main").returncode != 0:
        print("HEAD is not on origin/main: the publisher runs only merged code "
              "(check out main and pull).", file=sys.stderr)
        return False
    if _git("status", "--porcelain", "--", "app", "docs").stdout.strip():
        print("app/ or docs/ has uncommitted changes: the vault must come from one commit.",
              file=sys.stderr)
        return False
    print(f"building the publisher image from {sha[:12]}")
    # Exactly the committed tree (never the working copy); the Dockerfile needs only app/.
    archive = subprocess.Popen(["git", "archive", "--format=tar", sha, "Dockerfile", "app"],
                               cwd=REPO, stdout=subprocess.PIPE)
    built = subprocess.run(
        ["docker", "build", "--pull", "--label", _LABEL,
         "--label", f"org.opencontainers.image.revision={sha}", "-t", IMAGE, "-"],
        stdin=archive.stdout, check=False)
    archive.stdout.close()
    if archive.wait() != 0 or built.returncode != 0:
        print("building the publisher image failed", file=sys.stderr)
        return False
    # The previous build is untagged now; drop it (only images carrying our label).
    subprocess.run(["docker", "image", "prune", "-f", "--filter", f"label={_LABEL}"],
                   capture_output=True, check=False)
    return True


def main() -> int:
    passphrase = os.environ.get("SB_PUBLISHER_PASS")
    if not passphrase:
        print("Set SB_PUBLISHER_PASS (the publisher instance's passphrase).", file=sys.stderr)
        return 2
    if not DOCS:
        print("No docs/0*.md found — run from the repo.", file=sys.stderr)
        return 2
    if not build_image():
        return 1

    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", CONTAINER,
         "-e", f"SMARTBRAIN_PORT={PORT}", "-e", "SMARTBRAIN_HOST=0.0.0.0",
         "-e", f"SMARTBRAIN_LOCAL_TOKEN={TOKEN}",
         # The key-holding instance stays offline: no voice-model download, no broker link.
         "-e", "SMARTBRAIN_NO_VOICE_PREFETCH=1", "-e", "SMARTBRAIN_SIGNALING_URL=",
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

        # Full-sync docs: title = the doc's H1 (it becomes the row subscribers see in their
        # Knowledge list — "Getting started", not "01-getting-started.md"). The instance
        # exists ONLY to publish docs/, so anything not in the current file set
        # (renamed/removed upstream) is deleted — else a rename would ship both the old and
        # new doc. No content-update endpoint exists, so a changed doc is delete + re-add;
        # the vault attach below re-links the fresh ids.
        for d in api("GET", "/api/kb")["documents"]:
            api("DELETE", f"/api/kb/{d['id']}")
        doc_ids = []
        for path in DOCS:
            text = path.read_text(encoding="utf-8")
            title = next((ln[2:].strip() for ln in text.splitlines() if ln.startswith("# ")),
                         path.name)
            doc_ids.append(api("POST", "/api/kb", {"title": title, "content": text})["id"])
        print(f"synced {len(doc_ids)} docs into the publisher instance")

        vaults = api("GET", "/api/vaults")["vaults"]
        vault = next((v for v in vaults if v["name"] == VAULT_NAME), None)
        if vault is None:
            vault = api("POST", "/api/vaults", {"name": VAULT_NAME, "description": VAULT_DESC})
        else:
            # Keep the description in sync: the "Updated {date}" line is computed each build and
            # propagates to subscribers on the next update. Without this PATCH the line would
            # freeze at the date the vault was FIRST minted and re-runs would ship stale text.
            api("PATCH", f"/api/vaults/{vault['id']}",
                {"name": VAULT_NAME, "description": VAULT_DESC})
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
