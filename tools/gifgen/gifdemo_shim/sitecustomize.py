"""RECORDER-ONLY fetch shim for the public-vault subscribe/update GIF (clip 11).

WHY THIS EXISTS
    netguard's SSRF guard correctly REFUSES any localhost/LAN URL, so a demo cannot point the
    subscribe-by-URL field at a local file server — the guard would (rightly) block it. Production
    netguard must stay exactly that strict. So instead of weakening the guard, this shim replaces
    netguard's three vault fetchers *in the demo container only*, serving the publisher's already-
    generated .sbvault bytes from a local file. EVERYTHING ELSE in the flow is real: the app verifies
    the publisher signature, pins the key on first contact, re-encrypts every document under the
    subscriber's own master key, diffs against the pinned member map, and honours the owner-edit
    ("kept yours") guard. Only the network hop is simulated.

WHY IT CAN NEVER REACH THE SHIPPED IMAGE (three independent locks)
    1. It lives under tools/ — the Dockerfile only `COPY app/ /app/`, so tools/ is never in the image.
    2. It only loads when it is on PYTHONPATH; the shipped container never mounts it or points
       PYTHONPATH at it. Python auto-imports `sitecustomize` from sys.path at interpreter start, which
       is how run.sh injects it (a read-only mount + PYTHONPATH=/gifdemo), and nothing else does.
    3. Even if the file were somehow present, the patch is gated on SB_GIFDEMO=1 (below): with the
       env var unset it imports nothing and patches nothing — a no-op.

    Proof for a reviewer:
        grep COPY Dockerfile                      # -> only `COPY app/ /app/`
        docker run --rm smartbrain_3000:dev \\     # shipped config: no mount, no env
            python -c "from smartbrain_3000 import netguard; \\
                       print(netguard.safe_fetch_vault.__module__)"   # -> smartbrain_3000.netguard
"""
import os
import sys


def _install() -> None:
    # LOCK 3: without the explicit demo env var this module does nothing at all.
    if os.environ.get("SB_GIFDEMO") != "1":
        return

    # The state directory the host controls (a read-only bind mount). It holds the generated
    # publisher-*.sbvault files and a one-line `serve.txt` naming which one is "hosted" right now,
    # so the recorder can flip v1 -> v2 mid-take (the publisher pushing a new version) simply by
    # rewriting that pointer on the host.
    state_dir = os.environ.get("SB_GIFDEMO_STATE", "/gifdemo-state")

    # netguard imports vault_format + httpx; import it explicitly (with /app guaranteed on the path)
    # so patching does not depend on runpy's sys.path[0] timing at interpreter start.
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from smartbrain_3000 import netguard, vault_format

    def _served_bytes() -> bytes:
        pointer = os.path.join(state_dir, "serve.txt")
        with open(pointer, "r", encoding="utf-8") as fh:
            name = fh.read().strip()
        # Refuse anything but a bare filename inside the state dir — the pointer is recorder-authored,
        # but keep the shim honest about staying inside its sandbox.
        assert name and "/" not in name and ".." not in name, "serve.txt must name one local file"
        with open(os.path.join(state_dir, name), "rb") as fh:
            return fh.read()

    def _fake_fetch_vault(url: str) -> bytes:  # noqa: ARG001 - URL ignored: served from a local file
        return _served_bytes()

    def _fake_fetch_vault_manifest(url: str) -> bytes:  # noqa: ARG001
        # A /manifest.json (tree-host) URL would land here; extract the manifest entry from the same
        # served zip so both host shapes stay self-consistent. (The clip uses the single-file path.)
        return vault_format.manifest_entry(_served_bytes())

    def _fake_fetch_vault_object(url: str, max_bytes: int) -> bytes:  # noqa: ARG001
        # Only the single-file (zip) path is exercised by the clip, so no per-object fetch is made.
        # Fail loudly rather than silently serve the wrong bytes if that ever changes.
        raise netguard.FetchError("gifdemo shim serves single-file vaults only (no tree objects)")

    netguard.safe_fetch_vault = _fake_fetch_vault
    netguard.safe_fetch_vault_manifest = _fake_fetch_vault_manifest
    netguard.safe_fetch_vault_object = _fake_fetch_vault_object
    print(f"[gifdemo] netguard vault fetchers shimmed -> {state_dir}", file=sys.stderr, flush=True)


_install()
