#!/usr/bin/env python3
"""Release plan P5: seed the bundled sources (``app/smartbrain_3000/ni_seed.json``) from SmartBrain's own runs, every
card checked by hand before it ships.

Subcommands
  run     the real flow over the seed asks on the operator's machine: the local model builds, the cloud researcher
          runs with the operator's consent (Keep looking tapped as a person would), the real Library pack, keyless
          search and real fetches. A card the Library already answers needs no seed and is only counted. A card
          research built is a candidate: it prints with its reading and address, and is saved to
          ``<out>/candidates.json``, with ``<out>/verdicts.json`` listing every candidate for the hand check
          (fill in "right" or "wrong"; anything else is left out).
  export  the candidates marked "right", merged by address (at most ``ni_intake.MAX_ENTRIES`` questions each), as
          the seed file. The file is replaced, so an export always ships exactly what was checked.

The asks come from ``tools/ni-seed-asks.txt`` (everyday asks across the families the tuning gold shows). The
held-out set is never used for seeding and this tool never reads it.

Usage (operator's machine; the gateway at 127.0.0.1:38080 serves both models):
    PYTHONPATH=app python3 tools/ni-seed.py run --pack-dir <dir with library/> --out <dir> [--asks F] [--only TEXT]
        [--model M] [--research-model M]
    PYTHONPATH=app python3 tools/ni-seed.py export --out <dir> [--seed-file F]
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

ASKS = REPO / "tools" / "ni-seed-asks.txt"
SEED_FILE = REPO / "app" / "smartbrain_3000" / "ni_seed.json"
NO_SEED = REPO / "tools" / "ni-seed-none.json"   # never written: a run reads no seed
VERDICTS = ("right", "wrong")


def _tool(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "tools" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_asks(path: pathlib.Path) -> list[str]:
    """One ask per line; blank lines and ``#`` comments are skipped."""
    lines = [line.strip() for line in path.read_text("utf-8").splitlines()]
    return [line for line in lines if line and not line.startswith("#")]


def run_one(ask: str, idx, llm, model: str) -> dict:
    """One ask on a fresh in-memory store, as a new user: the outcome, and for a card research built the seed row
    its Keep would write (``ni_intake.seed_row``) with the reading the card shows."""
    import duckdb

    from smartbrain_3000 import db as dbmod
    from smartbrain_3000 import ni, ni_flow, ni_intake, ni_standing, search
    from smartbrain_3000.secrets import SecretStore, gen_master_key
    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    key = gen_master_key()
    store, secrets = ni.NIStore(conn, key), SecretStore(conn, key)
    ni_flow.set_library_provider(lambda: idx)
    ni_flow.set_search_provider(lambda: search.SearchService())
    ni_flow.set_secrets_provider(lambda: secrets)
    bridge = lambda _m, prompt: llm(prompt, 600)
    out: dict = {"ask": ask, "outcome": "", "path": "", "host": "", "title": "", "url": "", "reading": [], "row": None}
    shipped, ni_intake.SEED_FILE = ni_intake.SEED_FILE, NO_SEED   # research again, never confirm the last export
    try:
        item_id = ni_flow.create_shell_item(store, ask)
        rec = ni_flow.run_flow(store, item_id, gateway_call=bridge, ni_route_model=model)
        if ni_standing.lookable(rec) and not rec.get("_looking"):  # Keep looking, tapped as a person would
            ni_standing.seal(store, item_id, rec)
            rec = ni_flow.run_flow(store, item_id, gateway_call=bridge, ni_route_model=model)
        out["outcome"] = str(rec.get("state") or "")
        item = store.get_item(item_id)
        if out["outcome"] != "ready" or item is None:
            out["detail"] = str(rec.get("error") or "")[:160]
            return out
        built = item["spec"].get("_built_from") or {}
        out.update(path=str(built.get("path") or ""), host=str(built.get("host") or ""),
                   title=str(built.get("title") or ""), url=str((item["spec"].get("source") or {}).get("url") or ""))
        found = store.read_snapshot(item_id, "preview")
        out["reading"] = _e2e()._texts(found["payload"] if found else None)
        out["row"] = ni_intake.seed_row(store, [item])
        out["outcome"] = "candidate" if out["row"] is not None else "library" if built.get("path") in (
            "auto", "declared") else "not-replayable"
    except Exception as exc:  # a crash is one ask's outcome, never a stopped run
        out["outcome"], out["detail"] = "crash", f"{type(exc).__name__}: {str(exc)[:160]}"
    finally:
        ni_flow.set_secrets_provider(None)
        ni_intake.SEED_FILE = shipped
    return out


_E2E = None


def _e2e():
    global _E2E
    if _E2E is None:
        _E2E = _tool("ni_live_e2e", "ni-live-e2e.py")
    return _E2E


def cmd_run(args: argparse.Namespace) -> int:
    ev, flow_eval = _tool("ni_eval", "ni-eval.py"), _tool("ni_flow_eval", "ni-flow-eval.py")
    asks = load_asks(args.asks)
    if args.only:
        asks = [a for a in asks if args.only.lower() in a.lower()]
    idx = _e2e()._pack(args.pack_dir)
    args.model = args.model or flow_eval._DEFAULT_MODEL
    args.research_model = args.research_model or ev.DEFAULT_CLOUD_MODEL
    llm = flow_eval._bifrost_llm(args.bifrost, args.model)
    chat = ev.bifrost_chat(args.bifrost)
    args.out.mkdir(parents=True, exist_ok=True)
    runs, spend = [], 0.0
    for n, ask in enumerate(asks, 1):
        session = ev.Session(f"S{n}", recording=True)
        started = time.time()
        with ev._env({"SMARTBRAIN_NI_RESEARCH": "cloud", "SMARTBRAIN_NI_RESEARCH_WEB": "on"}), \
                ev.research_seam(session, "cloud", local_model=args.model, cloud_model=args.research_model,
                                 real_chat=chat, real_search=ev.live_search):
            run = run_one(ask, idx, llm, args.model)
        stats = session.research_stats()   # cost_usd only when the gateway reports one; the tokens always
        cost = float(stats.get("cost_usd") or 0.0)
        spend += cost
        run.update(id=f"S{n}", secs=round(time.time() - started, 1), cost_usd=round(cost, 4),
                   research={k: stats[k] for k in ("calls", "prompt", "completion", "cache_read", "cache_write")},
                   research_models=sorted({str(t.get("model") or "") for t in session.data.get("research") or []}))
        runs.append(run)
        print(f"[{run['outcome']:>14}] S{n:<3} {ask[:44]:<44} {run['secs']:>5}s ${cost:.3f} "
              f"{stats['calls']} calls {stats['prompt'] + stats['completion']} tok  {run['host']}", flush=True)
        if run["row"] is not None:
            print(f"{'':>17}reading: {' | '.join(run['reading'])[:200]}", flush=True)
            print(f"{'':>17}from:    {run['url'][:160]} ({run['title'] or 'no title'})", flush=True)
        elif run.get("detail"):
            print(f"{'':>17}detail:  {run['detail']}", flush=True)
    candidates = [r for r in runs if r["row"] is not None]
    (args.out / "runs.json").write_text(json.dumps(runs, indent=1, default=str))
    (args.out / "candidates.json").write_text(json.dumps(candidates, indent=1, default=str))
    verdicts = args.out / "verdicts.json"
    old = json.loads(verdicts.read_text()) if verdicts.is_file() else {}
    verdicts.write_text(json.dumps({r["id"]: old.get(r["id"], "") for r in candidates}, indent=1) + "\n")
    counts: dict[str, int] = {}
    for r in runs:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    used = {k: sum(r["research"][k] for r in runs) for k in ("calls", "prompt", "completion", "cache_read")}
    print(f"\nSUMMARY {json.dumps(counts)} research ${spend:.2f} {json.dumps(used)}; check {len(candidates)} in "
          f"{verdicts}")
    return 0


def merge(candidates: list[dict], verdicts: dict) -> list[dict]:
    """The rows of the candidates marked right, one per address, their questions merged (newest last, capped)."""
    from smartbrain_3000 import ni_intake
    rows: dict[str, dict] = {}
    for cand in candidates:  # bounded: one run's candidates
        row = cand.get("row")
        if verdicts.get(cand.get("id")) != "right" or not isinstance(row, dict):
            continue
        key = row["kept"]["key"]
        if key not in rows:
            rows[key] = row
            continue
        have = rows[key]["kept"]["entries"]
        fresh = [e for e in row["kept"]["entries"] if e["question"] not in [h["question"] for h in have]]
        rows[key]["kept"]["entries"] = [*have, *fresh][-ni_intake.MAX_ENTRIES:]
    return list(rows.values())


def cmd_export(args: argparse.Namespace) -> int:
    from smartbrain_3000 import ni_intake
    candidates = json.loads((args.out / "candidates.json").read_text())
    verdicts = json.loads((args.out / "verdicts.json").read_text())
    unknown = sorted(v for v in set(verdicts.values()) if v not in VERDICTS)
    if unknown or not verdicts:
        print(f"every candidate needs a verdict (right or wrong) first; found {unknown or 'none'}", file=sys.stderr)
        return 2
    rows = merge(candidates, verdicts)
    body = {"version": ni_intake.SEED_VERSION, "made": time.strftime("%Y-%m-%d", time.gmtime()), "rows": rows}
    args.seed_file.write_text(json.dumps(body, indent=1, ensure_ascii=False, sort_keys=True) + "\n")
    ni_intake._seed_at.cache_clear()
    readable = ni_intake._seed_at(str(args.seed_file))
    print(f"wrote {len(rows)} rows ({sum(len(r['kept']['entries']) for r in rows)} questions) to {args.seed_file}; "
          f"{len(readable)} read back")
    return 0 if len(readable) == len(rows) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--pack-dir", required=True, type=pathlib.Path)
    run.add_argument("--out", required=True, type=pathlib.Path)
    run.add_argument("--asks", default=ASKS, type=pathlib.Path)
    run.add_argument("--only", default="")
    run.add_argument("--model", default="", help="the local model that builds (default: the eval's)")
    run.add_argument("--research-model", default="", help="the cloud researcher (default: the eval's)")
    run.add_argument("--bifrost", default="http://127.0.0.1:38080")
    export = sub.add_parser("export")
    export.add_argument("--out", required=True, type=pathlib.Path)
    export.add_argument("--seed-file", default=SEED_FILE, type=pathlib.Path)
    args = ap.parse_args()
    return cmd_run(args) if args.cmd == "run" else cmd_export(args)


if __name__ == "__main__":
    sys.exit(main())
