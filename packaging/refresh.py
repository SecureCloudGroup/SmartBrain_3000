#!/usr/bin/env python3
"""Bump every packaging manifest to a new release version + its asset hashes.

Rewrites the Homebrew cask, the three winget manifests, and the Scoop manifest in place so they point
at a new version and the SHA-256 of that version's release assets. The release workflow runs this,
then pushes the results to the tap/bucket; it also works by hand:

    python3 packaging/refresh.py --version 0.5.0 \\
        --macos SmartBrain-macos.zip --windows SmartBrain-windows.zip

`--macos`/`--windows` are the paths to the two release .zip assets; the script hashes them itself, so
the numbers can never drift from the files they describe. Stdlib only.
"""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sub(path: Path, pattern: str, repl: str, *, count: int = 0) -> None:
    """Replace in a file, asserting the pattern was actually found — a manifest that silently fails to
    update would publish a stale hash, which is worse than a loud error."""
    text = path.read_text()
    new, n = re.subn(pattern, repl, text, count=count)
    assert n > 0, f"{path}: pattern not found: {pattern!r}"
    path.write_text(new)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--version", required=True, help="release version, e.g. 0.5.0 (a leading v is ok)")
    ap.add_argument("--macos", required=True, help="path to SmartBrain-macos.zip")
    ap.add_argument("--windows", required=True, help="path to SmartBrain-windows.zip")
    a = ap.parse_args()
    v = a.version.lstrip("v")
    mac, win = sha256(a.macos), sha256(a.windows)

    # Homebrew cask (macOS asset). The download URL uses #{version}, so only the version + hash move.
    cask = HERE / "homebrew/Casks/smartbrain.rb"
    sub(cask, r'version "[^"]*"', f'version "{v}"')
    sub(cask, r'sha256 "[0-9a-fA-F]*"', f'sha256 "{mac}"')

    # Scoop (Windows asset). Bump the version, the concrete 64-bit URL, and the hash — but NOT the
    # autoupdate template URL, which keeps its literal `v$version` (matched by requiring a digit after v).
    scoop = HERE / "scoop/smartbrain.json"
    sub(scoop, r'"version": "[^"]*"', f'"version": "{v}"')
    sub(scoop, r'/download/v\d[^/]*/', f'/download/v{v}/', count=1)
    sub(scoop, r'"hash": "[0-9a-fA-F]*"', f'"hash": "{win}"')

    # winget installer (Windows asset) — version, URL, and the uppercased SHA winget expects.
    wi = HERE / "winget/SecureCloudGroup.SmartBrain.installer.yaml"
    sub(wi, r"PackageVersion: .*", f"PackageVersion: {v}")
    sub(wi, r"/download/v[^/]*/", f"/download/v{v}/")
    sub(wi, r"InstallerSha256: [0-9a-fA-F]*", f"InstallerSha256: {win.upper()}")

    # winget locale + version manifests — version only.
    for name in ("SecureCloudGroup.SmartBrain.locale.en-US.yaml", "SecureCloudGroup.SmartBrain.yaml"):
        sub(HERE / "winget" / name, r"PackageVersion: .*", f"PackageVersion: {v}")

    print(f"bumped packaging to {v}  (macos {mac[:12]}… / windows {win[:12]}…)")


if __name__ == "__main__":
    main()
