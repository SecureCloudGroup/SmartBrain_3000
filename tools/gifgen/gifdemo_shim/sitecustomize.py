"""RECORDER-ONLY fetch shim for the public-vault subscribe/update GIF (clip 11) and the Neural
Interface GIF (clip 12, ``_install_ni`` below).

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


def _install_ni() -> None:
    """Clip 12: the Neural Interface builds cards from RECORDED source responses, fully offline.

    ``SB_GIFDEMO_NI`` names a read-only fixtures dir (tools/gifgen/gifdemo_state/ni): ``index.json``
    maps each source address the flow builds to one recorded body (curl'd once from the keyless public
    API). ``safe_fetch_json`` / ``safe_fetch_text`` serve that body; an address with no fixture fails
    loudly (logged ``MISS``), and every other netguard fetcher (pages, feeds, images, search, POSTs)
    refuses — nothing in this take reaches the network through netguard. Everything after the fetch
    is real: locate, the consent pause, the declared-answers build, the query floor, the form design
    and the commissioning runs. Dates are kept current: a body's dates (``dates``: every YYYY-MM-DD,
    shifted so the recorded day is today in the source's zone) or event epochs (``epoch_ms``: whole
    days, so the newest event stays within the last day) move with the clock, so "today" / "tomorrow"
    asks read the same on any recording day. Same three locks as above (tools/ only, PYTHONPATH,
    SB_GIFDEMO=1).
    """
    fixtures_dir = os.environ.get("SB_GIFDEMO_NI", "")
    if os.environ.get("SB_GIFDEMO") != "1" or not fixtures_dir:
        return
    import datetime as dt
    import json
    import re
    from urllib.parse import parse_qsl, urlencode, urlsplit
    from zoneinfo import ZoneInfo

    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from smartbrain_3000 import netguard

    date_value = re.compile(r"^\d{4}-?\d{2}-?\d{2}([T ][0-9:.]+Z?)?$")

    def key(url: str) -> str:
        """An address as the index keys it: query sorted, clock-filled dates as ``{date}``."""
        parts = urlsplit(url)
        query = sorted((k, "{date}" if date_value.match(v) else v) for k, v in parse_qsl(parts.query))
        return f"{parts.scheme}://{parts.netloc}{parts.path}" + (f"?{urlencode(query, safe='{}')}" if query else "")

    def recorded() -> dict:
        """The index, read per fetch (a few lines) so a fixture added mid-session is served at once."""
        with open(os.path.join(fixtures_dir, "index.json"), encoding="utf-8") as fh:
            return {key(f["url"]): f for f in json.load(fh)["fixtures"]}

    def shifted(text: str, shift: dict) -> tuple[str, int]:
        """The recorded body with its dates moved to today (whole days; 0 when recorded today)."""
        if shift.get("mode") == "dates":
            today = dt.datetime.now(ZoneInfo(shift["zone"])).date()
            days = (today - dt.date.fromisoformat(shift["anchor"])).days

            def move(m: re.Match) -> str:
                return (dt.date.fromisoformat(m.group(0)) + dt.timedelta(days=days)).isoformat()
            return re.sub(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)", move, text), days
        if shift.get("mode") == "epoch_ms":
            now_ms = int(dt.datetime.now(dt.UTC).timestamp() * 1000)
            days = (now_ms - int(shift["anchor_ms"])) // 86_400_000
            return re.sub(r'("(?:time|updated|generated)":\s*)(\d{13})',
                          lambda m: m.group(1) + str(int(m.group(2)) + days * 86_400_000), text), days
        return text, 0

    def served(url: str) -> dict:
        fixture = recorded().get(key(url))
        if fixture is None:
            print(f"[gifdemo-ni] MISS {url}", file=sys.stderr, flush=True)
            raise netguard.FetchError("gifdemo: no recorded response for this address")
        with open(os.path.join(fixtures_dir, fixture["file"]), encoding="utf-8") as fh:
            text, days = shifted(fh.read(), fixture.get("shift") or {})
        print(f"[gifdemo-ni] served {fixture['file']} (+{days}d)", file=sys.stderr, flush=True)
        return {"final_url": url, "status": 200, "content_type": fixture["content_type"], "text": text}

    def fake_fetch_json(url: str, headers: dict | None = None, allow_redirects: bool = True) -> object:  # noqa: ARG001
        return json.loads(served(url)["text"])

    def fake_fetch_text(url: str, fmt: str, headers: dict | None = None,  # noqa: ARG001
                        allow_redirects: bool = True) -> dict:
        return served(url)

    def refuse(name: str):
        def fetch(url: str, *args: object, **kwargs: object) -> object:  # noqa: ARG001
            print(f"[gifdemo-ni] refused {name} {url}", file=sys.stderr, flush=True)
            raise netguard.FetchError("gifdemo: offline take — only recorded source responses are served")
        return fetch

    netguard.safe_fetch_json = fake_fetch_json
    netguard.safe_fetch_text = fake_fetch_text
    for name in ("safe_fetch", "safe_fetch_page", "safe_fetch_feed", "safe_fetch_image", "safe_fetch_bytes",
                 "safe_post_json"):
        setattr(netguard, name, refuse(name))
    print(f"[gifdemo] netguard NI fetchers served from {fixtures_dir} ({len(recorded())} recorded)",
          file=sys.stderr, flush=True)


_install()
_install_ni()
