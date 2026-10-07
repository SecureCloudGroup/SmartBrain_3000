#!/usr/bin/env python3
"""Live end-to-end gate for Neural Interface card creation — the product path, from words.

For every ask: the REAL flow (``ni_flow.run_flow``) on the real local model through the
gateway, the real SmartBrain Library pack, real keyless web search, real network fetches.
When the card pauses at the source pick, the harness taps the FIRST Library suggestion exactly as
the pick route does (Library row → format + access sealed first). It never taps a link (a web page or a
Library source without declared answers — ruling 2026-10-05: those are offered as links, never built). A built card then runs
once through the real engine (``ni.run_item``) — the refresh every card lives on.

Outcomes per ask: ``live`` (built AND its first engine run is ok), ``built-no-run``,
``awaiting-yes`` (built from a web page or a model-mapped dataset: the card holds for the user's YES —
its reading, host and page / dataset title print so a human judges whether the YES would be right;
ruling 2026-10-04 — only a link the user pastes builds one now, so a harness run never reports it),
``links`` (no Library source declares answers for the ask: the pause offers only links — each prints as
host — title), ``needs-key`` / ``needs-email`` (an honest pause the user answers), ``no-source``, ``failed``.
Previews are printed so a human judges whether the card shows what was asked — a green
state with the wrong data is still a failure.

Usage (operator's machine; uses the gateway at 127.0.0.1:38080 for model calls only):
    PYTHONPATH=app python3 tools/ni-live-e2e.py --pack-dir <dir with library/> [--set dev|holdout | --asks-file F] [--only N]
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


def _tap_first(store, item_id: str, secrets, tapped: list, links: list) -> tuple[str | None, str]:
    """Tap the first Library suggestion the way ``pick_flow_source`` does. Returns (url, what); the tapped
    reading (its label and filled params, which place / team it is) is appended to ``tapped``. A link row
    is never tapped: when the pause offers only links they are copied to ``links`` and what is "links"."""
    field = ni_flow.board_flow_field(store, item_id) or {}
    sugs = field.get("suggestions") or []
    sources = [s for s in sugs if s.get("kind") == "library"]
    if not sources:
        links[:] = [{"host": s.get("host") or "", "title": s.get("title") or "", "url": s.get("url") or "",
                     "found": s.get("found") or ""} for s in sugs if s.get("kind") == "link"]
        return None, "links" if links else "no suggestions"
    first = sources[0]
    url = first["url"]
    record = ni_flow._flow_read(store, item_id) or {}
    row = next((r for r in record.get("_ranked_library") or [] if r.get("url") == url), None)
    tapped.append({"title": first.get("title"), "url": url, "label": (row or {}).get("label") or "",
                   "params": (row or {}).get("params") or {}, "scope": (row or {}).get("scope")})
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
    out: dict = {"ask": ask, "outcome": "", "source": "", "detail": "", "preview": None, "tapped": [],
                 "links": [], "notes": []}
    started = time.time()
    try:
        item_id = ni_flow.create_shell_item(store, ask)
        rec = ni_flow.run_flow(store, item_id, gateway_call=bridge, ni_route_model=model)
        for _tap in range(3):  # a source that refuses us re-lands the pick: tap the next one, like a person
            if rec.get("state") != "source":
                break
            url, what = _tap_first(store, item_id, secrets, out["tapped"], out["links"])
            out["source"] = (out["source"] + " → " if out["source"] else "") + what
            if url is None:
                out["outcome"] = what.split(" ")[0] if what.startswith(("needs-", "links")) else "no-source"
                out["notes"] = [str(n) for n in rec.get("notes") or []]
                return out
            rec = ni_flow.run_flow(store, item_id, gateway_call=bridge, ni_route_model=model, source_url=url)
        state = str(rec.get("state") or "")
        out["detail"] = str(rec.get("error") or "")[:160]
        out["notes"] = [str(n) for n in rec.get("notes") or []]  # the flow's own account: frame, verify
        if state != "ready":
            out["outcome"] = "failed" if state in ("failed", "unsupported") else state
            return out
        out["answers"] = next((str(n) for n in reversed(rec.get("notes") or [])
                               if "declared answers" in str(n)), "")
        snap = store.read_snapshot(item_id, "preview_data")
        out["preview"] = snap["payload"] if snap else None
        built = store.get_item(item_id)
        if ni.awaits_yes(built):
            # an open path: the card shows this reading and waits for the user's YES
            found = store.read_snapshot(item_id, "preview")
            origin = built["spec"].get("_built_from") or {}
            out["reading"] = _texts(found["payload"] if found else None)
            out["from"] = {"kind": "web page" if origin.get("path") == "page" else "dataset",
                           "host": origin.get("host") or "", "title": origin.get("title") or ""}
        run = _eval._engine_first_run(store, conn, item_id, llm, model, ni, key)
        out["outcome"] = "awaiting-yes" if out.get("from") else "live" if run == "ok" else "built-no-run"
        if run != "ok":
            out["detail"] = run
        item = store.get_item(item_id)
        latest = store.read_snapshot(item_id, "latest")
        out["scene_text"] = _texts(latest["payload"] if latest else None)
        out["pipeline"], out["latest"] = item["spec"].get("pipeline"), latest["payload"] if latest else None
        out["design"] = _design_line(item["spec"].get("scene") or {}, out["latest"])
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
    elif value and abs(value) < 1:  # 3 significant digits below 1, like the web
        base = f"{value:.3g}"
    else:
        base = f"{value:,.2f}".rstrip("0").rstrip(".")
    u = unit.strip() if isinstance(unit, str) else ""
    return f"{base} {u}" if u else base


def _design_line(scene: dict, payload) -> str:
    """One line a reader diagnoses a form card by (fix round 1a-5): the form, both sealed spans, who
    designed it (model / rules + the pick id), the lint counts with their codes, and the frame (question
    kind + wants) the design was made under; "" for a legacy scene."""
    if not isinstance(payload, dict) or payload.get("type") != "form":
        return ""
    clir = payload.get("clir") or {}
    spans = "/".join(str((clir.get(side) or {}).get("span") or "?") for side in ("desktop", "phone"))
    design = payload.get("design") or scene.get("design") or {}
    lint = payload.get("lint") or {}
    frame = scene.get("frame") or {}
    flags = (" fallback" if (scene.get("design") or {}).get("fallback") else "") + \
        (" design_needs_attention" if payload.get("design_needs_attention") else "")
    return (f"{payload.get('form')} {spans} designer={design.get('designer', '?')}/{design.get('pick', '?')} "
            f"lint red {lint.get('red', '?')} amber {lint.get('amber', '?')} {lint.get('codes') or []} "
            f"frame={frame.get('kind')} wants={frame.get('wants') or []}{flags}")


def _texts(node, acc=None) -> list[str]:
    """The words a person would read on the rendered card. A §34 form node prints its form
    name and the engine's summary line (the CLIR is painted by the client, not printed)."""
    acc = [] if acc is None else acc
    if isinstance(node, dict) and not node.get("hidden"):
        if node.get("type") == "form":
            acc.append(f"{node.get('form')}: {node.get('summary')}")
        elif node.get("type") == "number" and isinstance(node.get("value"), (int, float)):
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
    ap.add_argument("--asks-file", type=pathlib.Path,
                    help="a JSON list of asks (strings or {ask: ...}) — a fresh blind set instead of --set")
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
    if args.asks_file:
        asks = [a if isinstance(a, str) else a["ask"] for a in json.loads(args.asks_file.read_text())]
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
        if r.get("from"):
            src = r["from"]
            print(f"{'':>15}reading: {' | '.join(r.get('reading') or [])[:200]}", flush=True)
            print(f"{'':>15}from:    {src['host']} — {src['title'] or '(no title)'} ({src['kind']})",
                  flush=True)
        elif r.get("scene_text"):
            print(f"{'':>15}card:   {' | '.join(r['scene_text'])[:200]}", flush=True)
        if r.get("design"):
            print(f"{'':>15}design: {r['design'][:240]}", flush=True)
        for link in r.get("links") or []:  # offered as links, never built (ruling 2026-10-05)
            print(f"{'':>15}link:   {link['host']} — {link['title'] or '(no title)'}", flush=True)
    counts: dict[str, int] = {}
    for r in results:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    print("\nSUMMARY", args.set, json.dumps(counts))
    if args.out:
        args.out.write_text(json.dumps(results, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
