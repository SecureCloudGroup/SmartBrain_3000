#!/usr/bin/env python3
"""Live end-to-end gate for Neural Interface card creation — the product path, from words.

For every ask: the REAL flow (``ni_flow.run_flow``) on the real local model through the
gateway, the real SmartBrain Library pack, real keyless web search, real network fetches.
When the card pauses at the source pick, the harness taps the FIRST suggestion exactly as
the pick route does (Library row → format + access sealed first). A built card then runs
once through the real engine (``ni.run_item``) — the refresh every card lives on.

Outcomes per ask: ``live`` (built AND its first engine run is ok), ``built-no-run``,
``needs-key`` / ``needs-email`` (an honest pause the user answers), ``no-source``, ``failed``.
Previews are printed so a human judges whether the card shows what was asked — a green
state with the wrong data is still a failure.

Usage (operator's machine; uses the gateway at 127.0.0.1:38080 for model calls only):
    PYTHONPATH=app python3 tools/ni-live-e2e.py --pack-dir <dir with library/> [--set dev|holdout] [--only N]
        [--answers-dir <dir holding answers/<source_id>.json files>]

``--answers-dir`` overlays authored answers files onto the installed pack's records at lookup time
(a monkeypatch inside this harness process only) so a source's answers are live-tested before the
Library pack is rebuilt. Each card prints the answers it was built from.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "app"))

import duckdb  # noqa: E402

from smartbrain_3000 import db as dbmod  # noqa: E402
from smartbrain_3000 import library_index, ni, ni_flow, search  # noqa: E402
from smartbrain_3000.scheduler import ScheduleStore  # noqa: E402
from smartbrain_3000.secrets import SecretStore, gen_master_key  # noqa: E402

_EVAL = importlib.util.spec_from_file_location("ni_flow_eval", REPO / "tools" / "ni-flow-eval.py")
_eval = importlib.util.module_from_spec(_EVAL)
_EVAL.loader.exec_module(_eval)

# Asks written as people type them, across domains — never tuned to a source.
SETS = {
    "dev": [
        "tides for Charleston SC", "temp in Charleston SC", "NYC weather", "Philly forecast",
        "Portland weather", "air quality in LA", "will it rain tomorrow in Seattle",
        "Microsoft stock price", "bitcoin price", "euro to dollar exchange rate",
        "next Dodgers game", "Yankees score", "is GitHub down", "latest earthquakes",
        "hurricanes right now", "unemployment rate", "inflation rate", "gas prices",
        "top news headlines", "Hacker News top stories", "NASA picture of the day",
        "when is sunset in Denver", "flight delays at O'Hare", "river level in Boise",
        "wildfires near me in California", "mortgage rates", "latest Python release",
        "wave height in Santa Cruz", "Apple's latest SEC filings", "space station location",
    ],
    "holdout": [
        "high tide times in Savannah", "weather this weekend in Austin", "Ethereum price",
        "Tesla share price", "Red Sox schedule", "is Slack down", "pollen count in Atlanta",
        "UV index in Phoenix", "Fed interest rate", "USGS earthquakes in Alaska",
        "BBC world news", "yen to dollar", "Chicago snow forecast", "Miami water temperature",
        "Lakers next game", "consumer price index", "drought in Texas", "moon phase tonight",
        "Seahawks score", "Denver air quality",
    ],
}


def _pack(pack_dir: pathlib.Path) -> library_index.LibraryIndex:
    idx = library_index.LibraryIndex(pack_dir)
    if not idx.installed():
        idx.install()
    return idx


def _overlay_answers(answers_dir: pathlib.Path) -> None:
    """Test-only seam: ``<dir>/<source_id>.json`` answers win over the pack's for that source."""
    shipped = library_index.LibraryIndex.answers

    def answers(self, source_id: str) -> list[dict]:
        path = answers_dir / f"{source_id}.json"
        if path.is_file():
            return list(json.loads(path.read_text()).get("answers") or [])
        return shipped(self, source_id)

    library_index.LibraryIndex.answers = answers


def _tap_first(store, item_id: str, secrets) -> tuple[str | None, str]:
    """Tap the first suggestion the way ``pick_flow_source`` does. Returns (url, what)."""
    field = ni_flow.board_flow_field(store, item_id) or {}
    sugs = field.get("suggestions") or []
    if not sugs:
        return None, "no suggestions"
    first = sugs[0]
    url = first["url"]
    record = ni_flow._flow_read(store, item_id) or {}
    row = next((r for r in record.get("_ranked_library") or [] if r.get("url") == url), None)
    if row:
        ni_flow.seal_library_pick(store, item_id, url, row)
        if ni_flow.seal_access(store, item_id, url, row) is not None:
            missing = ni_flow.missing_access(store, item_id, secrets)
            if missing:
                return None, "needs-" + ("key" if "key" in missing else "email") + f" ({first['title']})"
    return url, f"{first.get('kind')}: {first['title']} — {first.get('host')}"


def run_one(ask: str, idx, llm, model: str) -> dict:
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    store, secrets = ni.NIStore(conn, key), SecretStore(conn, key)
    ni_flow.set_library_provider(lambda: idx)
    ni_flow.set_search_provider(lambda: search.SearchService())
    ni_flow.set_secrets_provider(lambda: secrets)
    bridge = lambda _m, prompt: llm(prompt, 600)  # noqa: E731
    out: dict = {"ask": ask, "outcome": "", "source": "", "detail": "", "preview": None}
    started = time.time()
    try:
        item_id = ni_flow.create_shell_item(store, ask)
        rec = ni_flow.run_flow(store, item_id, gateway_call=bridge, ni_route_model=model)
        for _tap in range(3):  # a source that refuses us re-lands the pick: tap the next one, like a person
            if rec.get("state") != "source":
                break
            url, what = _tap_first(store, item_id, secrets)
            out["source"] = (out["source"] + " → " if out["source"] else "") + what
            if url is None:
                out["outcome"] = what.split(" ")[0] if what.startswith("needs-") else "no-source"
                return out
            rec = ni_flow.run_flow(store, item_id, gateway_call=bridge, ni_route_model=model, source_url=url)
        state = str(rec.get("state") or "")
        out["detail"] = str(rec.get("error") or "")[:160]
        if state != "ready":
            out["outcome"] = "failed" if state in ("failed", "unsupported") else state
            return out
        out["answers"] = next((str(n) for n in reversed(rec.get("notes") or [])
                               if "declared answers" in str(n)), "")
        snap = store.read_snapshot(item_id, "preview_data")
        out["preview"] = snap["payload"] if snap else None
        run = _eval._engine_first_run(store, conn, item_id, llm, model, ni, key)
        out["outcome"] = "live" if run == "ok" else "built-no-run"
        if run != "ok":
            out["detail"] = run
        item = store.get_item(item_id)
        latest = store.read_snapshot(item_id, "latest")
        out["scene_text"] = _texts(latest["payload"] if latest else None)
        out["pipeline"], out["latest"] = item["spec"].get("pipeline"), latest["payload"] if latest else None
        out["source"] = out["source"] or item["spec"]["source"].get("url", "")
    except Exception as exc:  # a crash is a failed ask, never a stopped run
        import traceback
        out["outcome"], out["detail"] = "crash", f"{type(exc).__name__}: {str(exc)[:160]}"
        out["trace"] = traceback.format_exc()[-1500:]
        print(out["trace"], file=sys.stderr, flush=True)
    finally:
        out["secs"] = round(time.time() - started, 1)
        ni_flow.set_secrets_provider(None)
    return out


def _number_text(value: float, fmt: str, unit) -> str:
    """What web/src/lib/ni/scene.ts formatNumber shows (en-US), so the printed card is the seen card."""
    if fmt == "percent":
        return f"{value * 100:,.1f}".rstrip("0").rstrip(".") + "%"
    if fmt == "currency":
        return f"${value:,.2f}"
    if fmt == "compact" and abs(value) >= 1000:
        for size, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
            if abs(value) >= size:
                base = f"{value / size:.1f}".rstrip("0").rstrip(".") + suffix
                break
    else:
        base = f"{value:,.2f}".rstrip("0").rstrip(".")
    u = unit.strip() if isinstance(unit, str) else ""
    return f"{base} {u}" if u else base


def _texts(node, acc=None) -> list[str]:
    """The words a person would read on the rendered card."""
    acc = [] if acc is None else acc
    if isinstance(node, dict) and not node.get("hidden"):
        if node.get("type") == "number" and isinstance(node.get("value"), (int, float)):
            acc.append(_number_text(node["value"], node.get("format", "plain"), node.get("unit")))
        elif node.get("type") == "text" and isinstance(node.get("value"), (str, int, float)):
            acc.append(str(node["value"]))
        for child in node.get("children") or []:
            _texts(child, acc)
    return acc[:12]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack-dir", required=True, type=pathlib.Path)
    ap.add_argument("--set", default="dev", choices=sorted(SETS))
    ap.add_argument("--only", default="")
    ap.add_argument("--model", default=_eval._DEFAULT_MODEL)
    ap.add_argument("--bifrost", default=_eval._DEFAULT_BIFROST)
    ap.add_argument("--out", type=pathlib.Path)
    ap.add_argument("--answers-dir", type=pathlib.Path)
    args = ap.parse_args()
    if args.answers_dir:
        _overlay_answers(args.answers_dir)
    idx = _pack(args.pack_dir)
    llm = _eval._bifrost_llm(args.bifrost, args.model)
    asks = SETS[args.set]
    if args.only:
        asks = [a for a in asks if args.only.lower() in a.lower()]
    results = []
    for ask in asks:
        r = run_one(ask, idx, llm, args.model)
        results.append(r)
        print(f"[{r['outcome']:>12}] {ask:<36} {r['secs']:>5}s  {r['source'][:60]}", flush=True)
        if r["detail"]:
            print(f"{'':>15}detail: {r['detail']}", flush=True)
        if r.get("answers"):
            print(f"{'':>15}answers: {r['answers'][:200]}", flush=True)
        if r.get("scene_text"):
            print(f"{'':>15}card:   {' | '.join(r['scene_text'])[:200]}", flush=True)
    counts: dict[str, int] = {}
    for r in results:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    print("\nSUMMARY", args.set, json.dumps(counts))
    if args.out:
        args.out.write_text(json.dumps(results, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
