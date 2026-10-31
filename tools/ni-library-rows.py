#!/usr/bin/env python3
"""Record what the installed SmartBrain Library offers for an ask, as a test fixture.

The flow's tests drove invented Library rows for a year while the real pack ranked three keyed, undeclared quote
rows for "NVDA stock price every 30 minutes" (T's Mac, 2026-10-30), a shape no test had. This writes the rows the
installed pack really offers (``ni_flow._library_candidates``: the same rows the auto-pick seals, keyless first),
so a test runs the ask over the Library the user has. Re-run it when the pack version changes; the fixture names
the pack it came from.

Usage (any machine with the pack installed, or a scratch install of it):
    PYTHONPATH=app python3 tools/ni-library-rows.py --pack-dir <dir holding library/> \\
        --ask "NVDA stock price every 30 minutes" --subject NVDA --want price \\
        --out app/tests/fixtures/library/rows-nvda.json

``--subject`` / ``--want`` / ``--place`` give the intent's frame (what the model would have classified), as the
hint locate takes. Nothing is fetched: this reads the pack's index only.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import UTC, datetime

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "app"))

from smartbrain_3000 import library_index, ni_flow  # noqa: E402


def rows_for(pack_dir: pathlib.Path, ask: str, intent: dict, limit: int) -> list[dict]:
    """The pick rows for ``ask`` over the pack at ``pack_dir`` (the auto-pick's ``limit``)."""
    assert pack_dir.is_dir(), f"not a directory: {pack_dir}"
    idx = library_index.LibraryIndex(pack_dir)
    ni_flow.set_library_provider(lambda: idx)
    try:
        return ni_flow._library_candidates(ask, intent, limit=limit)
    finally:
        ni_flow.set_library_provider(None)


def pack_version(pack_dir: pathlib.Path) -> str:
    """The installed pack's release tag as ``installed.json`` records it (``library_index`` writes ``tag``), ""
    when unknown."""
    meta = pack_dir / "library" / "installed.json"
    try:
        body = json.loads(meta.read_text())
    except (OSError, ValueError):
        return ""
    return str(body.get("tag") or body.get("version") or "") if isinstance(body, dict) else ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pack-dir", required=True, type=pathlib.Path)
    ap.add_argument("--ask", required=True)
    ap.add_argument("--subject", default=None)
    ap.add_argument("--want", action="append", default=[])
    ap.add_argument("--place", default=None)
    ap.add_argument("--frame-kind", default=None)
    ap.add_argument("--limit", type=int, default=ni_flow._AUTO_ROWS)
    ap.add_argument("--out", required=True, type=pathlib.Path)
    args = ap.parse_args()
    intent = {"subject": args.subject or args.ask, "wants": list(args.want), "place": args.place,
              "frame_kind": args.frame_kind}
    rows = rows_for(args.pack_dir, args.ask, intent, args.limit)
    out = {"ask": args.ask, "intent": intent, "limit": args.limit, "pack_version": pack_version(args.pack_dir),
           "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"), "rows": rows}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for r in rows:  # bounded: the pick's rows
        print(f"{r['source_id']:<28} {'keyed' if r.get('needs_key') else 'free ':<6} answers={r.get('answers')!s:<5} {r['url']}")
    print(f"wrote {args.out} ({len(rows)} rows, pack {out['pack_version'] or 'unknown'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
