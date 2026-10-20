"""S2 shadow replay: the four verify constraint classes over the recorded blind-50 results (offline).

``python tests/ni_eval/shadow_replay.py`` (from app/, PYTHONPATH=.) prints one line per live card with the
human verdict and the reasons the classes give; the CI test (tests/test_ni_verify_blind50.py) asserts the
numbers. The recorded run has no clock of its own: the newest earthquake in the USGS card is 02:02 UTC on
Oct 6 and the Minneapolis hourly card starts at 22:00 CDT on Oct 5, so the run was made at about
02:45 UTC = 21:45 America/Chicago; the user zone is taken as Chicago (the same zone as the Minneapolis card).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from smartbrain_3000.ni_verify.classes import shadow_reasons
from smartbrain_3000.ni_verify.rows import SourceInfo, read_result

HERE = Path(__file__).resolve().parent
RESULTS = HERE.parent / "fixtures" / "ni_blind" / "2026-10-05" / "results.json"
GOLD = HERE / "gold" / "gold.json"
RUN_NOW = datetime(2026, 10, 6, 2, 45, tzinfo=UTC)
RUN_ZONE = ZoneInfo("America/Chicago")


def shadow_for_run(run: dict, now: datetime = RUN_NOW, zone: ZoneInfo = RUN_ZONE) -> dict[str, list[str]]:
    """Reasons per class for one recorded run (a card the harness built), from what the record keeps."""
    assert isinstance(run, dict), "run must be a dict"
    assert now.tzinfo is not None, "now must be aware"
    taps = run.get("tapped") or [{}]
    tap = taps[-1] if isinstance(taps[-1], dict) else {}
    texts = [*(str(t) for t in run.get("scene_text") or []), str(tap.get("label") or ""),
             str(tap.get("title") or ""), json.dumps(run.get("preview"), default=str)[:20000]]
    result = read_result(run.get("preview"), texts, zone)
    name = str(run.get("source") or "").split("—")[0].replace("library:", "").strip()
    source = SourceInfo(name=name, url=str(tap.get("url") or ""), scope=tap.get("scope"),
                        params=frozenset(str(k) for k in (tap.get("params") or {})))
    return shadow_reasons(str(run.get("ask") or ""), result, source, now, zone)


def blind50_rows() -> list[dict]:
    """One row per live card of the 2026-10-05 blind-50: ask, human verdict (review.md via the gold), reasons."""
    gold = {g["ask"]: g for g in json.loads(GOLD.read_text()) if g.get("set") == "A"}
    rows = []
    for run in json.loads(RESULTS.read_text()):
        if run.get("outcome") != "live":
            continue
        rec = gold.get(run["ask"]) or {}
        rows.append({"ask": run["ask"], "verdict": rec.get("human_verdict"), "reasons": shadow_for_run(run)})
    assert len(rows) == 23, "the blind-50 has 23 live cards"
    return rows


def main() -> None:
    """Print the table and the two numbers the phase is gated on."""
    rows = blind50_rows()
    for r in rows:
        print(f"{r['verdict'] or '?':8} {r['ask'][:44]:44} {r['reasons'] or ''}")
    right = [r for r in rows if r["verdict"] == "right"]
    wrong = [r for r in rows if r["verdict"] == "wrong"]
    print(f"false rejects: {sum(1 for r in right if r['reasons'])} of {len(right)} right cards")
    print(f"catches: {sum(1 for r in wrong if r['reasons'])} of {len(wrong)} wrong cards")


if __name__ == "__main__":
    main()
