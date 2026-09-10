#!/usr/bin/env python3
"""Validate every Neural Interface template JSON in a directory.

Registry-repo CI helper: runs against an installed ``smartbrain_3000`` package (the
registry repo pins a version), calling the same validators the app uses. Exits
NONZERO on any failure so a bad PR fails the CI.

Usage:
    python -m tools.ni_library.validate --templates <dir>
    python tools/ni-library/validate.py --templates <dir>

Wraps ``ni_library._validate_one_template`` so a template that would round-trip
through ``parse_pack`` at subscriber-side load is exactly what the CI accepts.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

# The app package must be importable — add repo/app to sys.path.
_REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "app"))

from smartbrain_3000 import ni_library


def _validate_dir(directory: pathlib.Path) -> tuple[int, int]:
    """Validate every *.json in ``directory``; return (passed, failed) counts.

    Deterministic order (sorted by filename) so a failing CI log always names the
    same file first when two are broken.
    """
    assert directory.is_dir(), f"templates directory missing: {directory}"
    files = sorted(directory.glob("*.json"))
    if not files:
        # LOW#2 (audit 2026-09-09): a template dir with no *.json is a broken PR (empty
        # pack), not a passing CI. Count it as one failure so main() returns 1.
        print(f"FAIL: no *.json templates in {directory}", file=sys.stderr)
        return 0, 1
    passed = failed = 0
    seen: set[str] = set()
    for path in files:  # bounded by the directory listing
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"FAIL {path.name}: could not read: {exc}", file=sys.stderr)
            failed += 1
            continue
        try:
            ni_library._validate_one_template(body, failed + passed, seen)
        except ni_library.LibraryError as exc:
            print(f"FAIL {path.name}: {exc}", file=sys.stderr)
            failed += 1
            continue
        print(f"ok   {path.name}")
        passed += 1
    return passed, failed


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate NI templates against the app's validators.")
    parser.add_argument("--templates", required=True, type=pathlib.Path,
                        help="Directory of *.json template files")
    args = parser.parse_args()
    passed, failed = _validate_dir(args.templates)
    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
