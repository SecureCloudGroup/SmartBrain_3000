#!/usr/bin/env python3
"""Query layer measurement (Round 20, Q3a): the Query IR an ask becomes, scored against the 109
hand-labeled gold IRs of the blind sets A-E (``app/tests/fixtures/ni_query/``).

  --replies   offline, deterministic: the RECORDED Q2 replies (few-shot TEST 79, zero-shot ALL
              109) through the module's normalization. Prints the v1-contract score (the lead's
              re-score; reads 50/60 and 54/82) and the full-contract score (+ the recognizer
              number validator and the count switch), the prompt-parity check, and the
              code-owned clauses against gold.
  --exec      offline, deterministic: apply_query's gold rows against the Q2 harness's recorded
              ones (port check), then execution agreement of the recorded model IRs (raw and
              final) with the gold IR on the recorded samples.
  --gateway URL --model ID [--zero] [--out FILE]   live: plan_query on the 79 TEST rows (few-shot
              from the 22 clean DEV rows; --zero: zero-shot on all 109), one request at a time,
              temperature 0, max_tokens 600; per-clause, per-kind, validity and latency tables.

Run inside smartbrain_3000:dev with PYTHONPATH=/app (the scratch driver), or natively from the repo.
"Clean" rows have an empty note; noted rows are never scored against the model.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import pathlib
import statistics
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "app"))

from smartbrain_3000.ni_query import plan_query, recognize
from smartbrain_3000.ni_query.apply import apply_query, dig
from smartbrain_3000.ni_query.decl import answer_named, axis_of, cells_of
from smartbrain_3000.ni_query.normalize import (
    DIRECTIONS,
    count_equiv,
    explicit_time,
    order_key,
    stated_numbers,
    structural_errors,
    time_key,
    v1_where,
    where_key,
)
from smartbrain_3000.ni_query.plan import finalize, floor_plan, make_ctx
from smartbrain_3000.ni_query.prompt import (
    build_messages,
    prompt_sha,
    retrieve_examples,
)

FIX = _REPO / "app" / "tests" / "fixtures" / "ni_query"
CLAUSES = ("answer", "select", "where", "time", "order", "limit", "agg")
_TIMEOUT_S = 90
_MAX_ROWS = 200


def jl(path: pathlib.Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load(fix: pathlib.Path = FIX) -> dict:
    """Every fixture, keyed for the scorers."""
    assert fix.is_dir(), f"fixtures missing: {fix}"
    split = json.loads((fix / "split.json").read_text())
    return {"gold": {g["id"]: g for g in jl(fix / "gold.jsonl")}, "split": split,
            "library": json.loads((fix / "library.json").read_text()), "pool": jl(fix / "pool.jsonl"),
            "few": {r["id"]: r for r in jl(fix / "replies_fewshot.jsonl")},
            "zero": {r["id"]: r for r in jl(fix / "replies_zero.jsonl")},
            "index": json.loads((fix / "samples" / "index.json").read_text()),
            "exec_gold": json.loads((fix / "exec_gold.json").read_text()),
            "shas": json.loads((fix / "prompt_sha.json").read_text()), "fix": fix}


def ref_now(data: dict, g: dict) -> dt.datetime:
    """The row's reference instant: its set's date at the split's reference time, in the zone."""
    s = data["split"]
    return dt.datetime.fromisoformat(f"{s['ref_dates'][g['set']]}T{s['ref_time']}:00").replace(
        tzinfo=ZoneInfo(s["zone"]))


def consumed_proxy(ask: str) -> list[str]:
    """rescore_v1's proxy for the parameter-consumed entities: the ask's title-case tokens."""
    return [t for t in ask.split() if t[:1].isupper() and t[1:] == t[1:].lower()]


def row_ctx(data: dict, g: dict):
    src = data["library"][g["source_id"]]
    spans = recognize(g["ask"], now=ref_now(data, g), zone=data["split"]["zone"])
    return make_ctx(g["ask"], g["kind"], src["answers"], spans, consumed_proxy(g["ask"]), src["name"])


def v1_score(g: dict, m: dict | None, ctx) -> dict:
    """rescore_v1.py's four comparisons, ported: A (+ count_equiv), T (explicit phrases decided by
    the recognizers), W (gold answer's cells), O (axis implicit; non-axis for rankings)."""
    if not isinstance(m, dict):
        return {"A": False, "T": False, "W": False, "O": False}
    ir, ga = g["ir"], answer_named(ctx.answers, g["ir"]["answer"])
    exp = explicit_time(ctx.spans, g["kind"])
    ww = [v1_where(x, ga, ctx.ask, ctx.consumed_words, ctx.src_words) for x in (m.get("where") or [], ir["where"])]
    return {"A": m.get("answer") == ir["answer"] or count_equiv(m.get("answer"), ir, ctx.answers),
            "T": time_key(exp if exp is not None else m.get("time")) == time_key(exp if exp is not None else ir["time"]),
            "W": where_key(ww[0]) == where_key(ww[1]),
            "O": order_key(m.get("order") or [], ga, g["kind"]) == order_key(ir["order"], ga, g["kind"])}


def gold_final(g: dict, ctx) -> dict:
    """The gold IR under the full contract's model-owned rules (the comparison side)."""
    ir, ga = g["ir"], answer_named(ctx.answers, g["ir"]["answer"])
    exp = explicit_time(ctx.spans, g["kind"])
    where = stated_numbers(v1_where(ir["where"], ga, ctx.ask, ctx.consumed_words, ctx.src_words), ctx.spans, ctx.ask)
    return {**ir, "where": where, "time": exp if exp is not None else ir["time"]}


def full_score(g: dict, final: dict | None, ctx) -> dict:
    """The final IR (plan_query's code path) against the gold IR under the same rules."""
    if final is None:
        return {"A": False, "T": False, "W": False, "O": False}
    gf, fa = gold_final(g, ctx), answer_named(ctx.answers, final["answer"])
    ga = answer_named(ctx.answers, g["ir"]["answer"])
    return {"A": final["answer"] == g["ir"]["answer"] or count_equiv(final["answer"], g["ir"], ctx.answers),
            "T": time_key(final["time"]) == time_key(gf["time"]),
            "W": where_key(final["where"]) == where_key(gf["where"]),
            "O": order_key(final["order"], fa, g["kind"]) == order_key(g["ir"]["order"], ga, g["kind"])}


def clause_exact(final: dict | None, gold: dict) -> dict:
    """The Q2 harness's per-clause exact match (select ordered, where as a set, limit numeric)."""
    if final is None:
        return {c: False for c in CLAUSES}
    return {"answer": final["answer"] == gold["answer"], "select": list(final["select"]) == list(gold["select"]),
            "where": where_key(final["where"]) == where_key(gold["where"]),
            "time": (final["time"] or None) == (gold["time"] or None),
            "order": [(o["col"], o["dir"]) for o in final["order"]] == [(o["col"], o["dir"]) for o in gold["order"]],
            "limit": final["limit"] == gold["limit"], "agg": final["agg"] == gold["agg"]}


def finalize_reply(m: dict | None, ctx) -> tuple[dict | None, list]:
    """A recorded reply through plan_query's own normalization; None when it is not valid here."""
    if not isinstance(m, dict):
        return None, ["invalid"]
    errs = structural_errors(m, ctx.answers)
    if errs:
        return None, ["structure: " + errs[0]]
    return finalize(m, ctx)


def fully(s: dict) -> bool:
    return all(s[k] for k in "ATWO")


def _pct(n: int, d: int) -> str:
    return f"{n}/{d} ({100 * n / d:.0f}%)" if d else "0/0"


def report_scores(title: str, rows: list[tuple[dict, dict]]) -> None:
    """rows = [(gold, score dict)] over clean rows."""
    n = len(rows)
    ok = [g["id"] for g, s in rows if fully(s)]
    miss = [(g["id"], "".join(k for k in "ATWO" if not s[k])) for g, s in rows if not fully(s)]
    print(f"== {title}: {len(ok)}/{n} fully right; " + ", ".join(f"{k} {sum(s[k] for _, s in rows)}" for k in "ATWO"))
    print(f"   misses: {miss}")


def replies_mode(data: dict) -> int:
    """--replies (module docstring)."""
    gold, test = data["gold"], set(data["split"]["test"])
    rc = 0
    for label, key, ids in (("few-shot TEST (clean)", "few", [i for i in data["split"]["test"]]),
                            ("zero-shot ALL (clean)", "zero", sorted(gold, key=lambda i: (i[0], int(i[1:]))))):
        v1_rows, full_rows, floor_rows, code = [], [], [], {"select": 0, "limit": 0, "agg": 0}
        for rid in ids:
            g = gold[rid]
            if g["note"] or (key == "few" and rid not in test):
                continue
            ctx, m = row_ctx(data, g), data[key][rid]["parsed"]
            v1_rows.append((g, v1_score(g, m, ctx)))
            final, _notes = finalize_reply(m, ctx)
            full_rows.append((g, full_score(g, final, ctx)))
            floor_rows.append((g, full_score(g, final or floor_plan(ctx, []), ctx)))
            ex = clause_exact(final or floor_plan(ctx, []), g["ir"])
            for c in code:
                code[c] += ex[c]
        report_scores(f"v1 contract, {label}", v1_rows)
        report_scores(f"full contract, {label}", full_rows)
        report_scores(f"full contract + rules floor for invalid replies, {label}", floor_rows)
        print(f"   code-owned clauses equal to gold ({len(full_rows)} rows): "
              + ", ".join(f"{c} {v}" for c, v in code.items()))
        rc |= 0 if (key, sum(fully(s) for _, s in v1_rows), len(v1_rows)) in (("few", 50, 60), ("zero", 54, 82)) else 1
    rc |= prompt_parity(data)
    print("REPLIES rc =", rc)
    return rc


def prompt_parity(data: dict) -> int:
    """Our messages hash equal to the Q2 prompts (few-shot TEST with retrieval; zero-shot ALL)."""
    few = zero = n_few = 0
    for rid, g in data["gold"].items():
        src = data["library"][g["source_id"]]
        source = {"id": g["source_id"], "name": src["name"]}
        args = {"source": source, "answers": src["answers"], "params": src["params"], "now": ref_now(data, g),
                "zone": data["split"]["zone"]}
        zero += prompt_sha(build_messages(g["ask"], examples=[], **args)) == data["shas"][rid]["zero"]
        if "few" in data["shas"][rid]:
            n_few += 1
            ex = retrieve_examples(g["ask"], g["kind"], data["pool"])
            same = [e["id"] for e in ex] == data["shas"][rid]["examples"]
            few += same and prompt_sha(build_messages(g["ask"], examples=ex, **args)) == data["shas"][rid]["few"]
    print(f"== prompt parity with the Q2 prompts: zero-shot {zero}/{len(data['gold'])}, few-shot {few}/{n_few}")
    return 0 if (zero, few) == (len(data["gold"]), n_few) else 1


# ---------------------------------------------------------------- execution (--exec)

def _csv_rows(text: str) -> dict:
    rows = list(csv.DictReader(io.StringIO(text)))
    if rows:
        first = next(iter(rows[0].keys()))
        dates = [r.get(first) or "" for r in rows]
        if len(dates) > 1 and dates[0][:4].isdigit() and dates[0][4:5] == "-" and dates[0] < dates[-1]:
            rows.reverse()       # the product's parse_csv: a date series reads newest first
    return {"rows": rows}


def _feed_items(text: str) -> dict:
    root = ET.fromstring(text.encode("utf-8"))
    items = []
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] not in ("item", "entry"):
            continue
        rec = {"title": "", "published": ""}
        for ch in el:
            n = ch.tag.rsplit("}", 1)[-1]
            if n == "title":
                rec["title"] = (ch.text or "").strip()
            elif n in ("pubDate", "published") or (n in ("updated", "date") and not rec["published"]):
                rec["published"] = (ch.text or "").strip()
        items.append(rec)
    return {"items": items}


def payload_of(sample: dict) -> object:
    """A recorded body parsed as the product does (the Q2 harness's normalize, ported)."""
    body, ct = sample.get("body") or "", (sample.get("content_type") or "").lower()
    head = body.lstrip()[:1]
    if head in ("{", "["):
        data = json.loads(body)
        if isinstance(data, list):
            if data and isinstance(data[0], list) and all(isinstance(x, str) for x in data[0]):
                data = [dict(zip(data[0], r)) for r in data[1:]]
            data = {"items": data}
        return data
    if "csv" in ct or "observation_date" in body[:200]:
        return _csv_rows(body)
    if head == "<":
        return _feed_items(body)
    raise ValueError(f"unrecognized payload ({ct})")


def _filled(answer: dict, params_used: dict) -> dict:
    """The declared filter with its ``{param}`` filled (as ni_flow._fill_answer does)."""
    flt = answer.get("filter")
    if not flt:
        return answer
    want = str(flt["equals"])
    for k, v in (params_used or {}).items():
        want = want.replace("{" + k + "}", str(v))
    return {**answer, "filter": {"path": flt["path"], "equals": want}}


def _ids(answer: dict, res: dict, payload: object) -> list[str]:
    """apply_query's indices as the Q2 harness's row ids (path-qualified, so answers compare)."""
    path = str(answer.get("path") or "")
    if answer["kind"] == "value":
        if answer.get("type") == "count" and isinstance(dig(payload, path), list):
            return [f"{path}#{i}" for i in res["indices"]]
        return [f"value:{path}" for _ in res["indices"]]
    if answer["kind"] == "columns":
        base = (axis_of(answer) or cells_of(answer)[0]["path"]).rsplit(".", 1)[0]
        return [f"{base}#{i}" for i in res["indices"]]
    return [f"{path}#{i}" for i in res["indices"]]


def execute(data: dict, g: dict, ir: dict, cache: dict) -> dict | None:
    e = data["index"].get(g["id"]) or {}
    if not e.get("file"):
        return None
    if e["file"] not in cache:
        cache[e["file"]] = payload_of(json.loads((data["fix"] / "samples" / e["file"]).read_text()))
    payload = cache[e["file"]]
    answer = _filled(answer_named(data["library"][g["source_id"]]["answers"], ir["answer"]), g["params_used"])
    now = dt.datetime.fromisoformat(e["fetched_at"])
    direction = "backward" if DIRECTIONS.get(g["kind"]) == "back" else "forward"
    res = apply_query(ir, payload, answer, now=now, zone=data["split"]["zone"], direction=direction)
    return {"ids": _ids(answer, res, payload), "count": res["count"]}


def agree(a: dict, b: dict) -> bool:
    return set(a["ids"]) == set(b["ids"]) and a["count"] == b["count"]


def exec_mode(data: dict) -> int:
    """--exec (module docstring)."""
    cache: dict = {}
    port = [0, 0]
    for rid, g in data["gold"].items():
        mine, theirs = execute(data, g, g["ir"], cache), data["exec_gold"].get(rid)
        if mine is None or theirs is None:
            continue
        port[1] += 1
        port[0] += len(mine["ids"]) == theirs["n"] and mine["ids"][:12] == theirs["ids"] and mine["count"] == theirs["count"]
    print(f"== apply_query port check: gold rows equal to the Q2 harness's on {port[0]}/{port[1]} samples")
    for label, key, scope in (("few-shot TEST", "few", set(data["split"]["test"])), ("zero-shot ALL", "zero", None)):
        raw = fin = empty = n = 0
        for rid, rep in data[key].items():
            g = data["gold"][rid]
            if g["note"] or (scope is not None and rid not in scope) or not (data["index"].get(rid) or {}).get("file"):
                continue
            n += 1
            gold_res = execute(data, g, g["ir"], cache)
            m = rep["parsed"]
            ok_raw = isinstance(m, dict) and not structural_errors(m, data["library"][g["source_id"]]["answers"]) \
                and agree(execute(data, g, m, cache), gold_res)
            final, _ = finalize_reply(m, row_ctx(data, g))
            ok_fin = final is not None and agree(execute(data, g, final, cache), gold_res)
            raw, fin = raw + ok_raw, fin + ok_fin
            empty += ok_fin and not gold_res["ids"]
        print(f"== execution agreement, {label} (clean rows with a sample): raw model IR {_pct(raw, n)}, "
              f"final IR {_pct(fin, n)} (both empty {empty})")
    rc = 0 if port[0] == port[1] else 1
    print("EXEC rc =", rc)
    return rc


# ---------------------------------------------------------------- live (--gateway)

def gateway(url: str, model: str, calls: list) -> object:
    """chat_json's transport over the local gateway; every call's latency/finish lands in ``calls``."""
    endpoint = url.rstrip("/") + "/v1/chat/completions"

    def call(messages: list) -> str:
        body = json.dumps({"model": model, "temperature": 0, "max_tokens": 600, "messages": messages}).encode()
        req = urllib.request.Request(endpoint, data=body, headers={"Content-Type": "application/json"})
        t0 = time.time()
        rec = {"sha": prompt_sha(messages) if not calls else None}
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
                payload = json.load(resp)
            choice = payload["choices"][0]
            content = choice["message"].get("content") or ""
            rec.update(ms=round((time.time() - t0) * 1000), finish=choice.get("finish_reason"), reply=content[:800],
                       cached=((payload.get("usage") or {}).get("prompt_tokens_details") or {}).get("cached_tokens"))
            return content
        except (OSError, KeyError, IndexError, ValueError) as ex:
            rec.update(ms=round((time.time() - t0) * 1000), error=f"{type(ex).__name__}: {ex}"[:200])
            raise
        finally:
            calls.append(rec)
    return call


def live_row(data: dict, g: dict, url: str, model: str, few: bool) -> dict:
    src, calls = data["library"][g["source_id"]], []
    plan = plan_query(g["ask"], kind=g["kind"], wants=[], source={"id": g["source_id"], "name": src["name"]},
                      answers=src["answers"], params=src["params"], consumed=consumed_proxy(g["ask"]),
                      now=ref_now(data, g), zone=data["split"]["zone"], call_model=gateway(url, model, calls),
                      pool=data["pool"] if few else [])
    want = data["shas"][g["id"]].get("few" if few else "zero")
    return {"id": g["id"], "kind": g["kind"], "note": g["note"], "ask": g["ask"], "model_ir": plan.model_ir,
            "ir": plan.ir, "notes": plan.notes, "coverage": plan.coverage, "interpretation": plan.interpretation,
            "model_meta": plan.model_meta, "calls": calls, "prompt_same": bool(calls) and calls[0]["sha"] == want}


def _q(values: list[int], p: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    return s[min(len(s) - 1, round(p * (len(s) - 1)))]


def live_report(data: dict, recs: list[dict]) -> None:
    clean = [r for r in recs if not r["note"]]
    gold = data["gold"]
    ctxs = {r["id"]: row_ctx(data, gold[r["id"]]) for r in clean}
    v1 = [(gold[r["id"]], v1_score(gold[r["id"]], r["model_ir"], ctxs[r["id"]])) for r in clean]
    fm = [(gold[r["id"]], full_score(gold[r["id"]], r["ir"] if r["model_ir"] else None, ctxs[r["id"]])) for r in clean]
    ff = [(gold[r["id"]], full_score(gold[r["id"]], r["ir"], ctxs[r["id"]])) for r in clean]
    report_scores("v1 contract (model replies, as the re-score)", v1)
    report_scores("full contract (final IR; an invalid reply counts wrong)", fm)
    report_scores("full contract with the rules floor (what the card would get)", ff)
    ex = [clause_exact(r["ir"], gold[r["id"]]["ir"]) for r in clean]
    print("   per clause exact vs gold (final IR): " + ", ".join(f"{c} {sum(e[c] for e in ex)}/{len(ex)}" for c in CLAUSES))
    kinds = sorted({r["kind"] for r in clean})
    print("   fully right by kind (full contract + floor): " + "; ".join(
        f"{k} {sum(fully(s) for g, s in ff if g['kind'] == k)}/{sum(1 for g, _ in ff if g['kind'] == k)}" for k in kinds))
    first = sum(1 for r in recs if r["model_meta"]["valid_first_try"])
    never = sum(1 for r in recs if r["model_ir"] is None)
    print(f"   validity (all {len(recs)} rows): first try {first}, after the retry {len(recs) - first - never}, "
          f"never {never}; prompts identical to the Q2 harness's {sum(r['prompt_same'] for r in recs)}/{len(recs)}")
    one = [r["calls"][0]["ms"] for r in recs if r["calls"]]
    tot = [sum(c["ms"] for c in r["calls"]) for r in recs if r["calls"]]
    print(f"   latency first call median {statistics.median(one) if one else 0} ms / p95 {_q(one, 0.95)} ms; "
          f"per row median {statistics.median(tot) if tot else 0} ms / p95 {_q(tot, 0.95)} ms")


def live_mode(data: dict, url: str, model: str, zero: bool, out: str | None) -> int:
    ids = sorted(data["gold"], key=lambda i: (i[0], int(i[1:]))) if zero else list(data["split"]["test"])
    done = {r["id"]: r for r in jl(pathlib.Path(out))} if out and pathlib.Path(out).exists() else {}
    recs, fails = [done[i] for i in ids if i in done], 0
    for n, rid in enumerate([i for i in ids if i not in done][:_MAX_ROWS], 1):
        rec = live_row(data, data["gold"][rid], url, model, few=not zero)
        recs.append(rec)
        if out:
            with open(out, "a") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fails = fails + 1 if "model_unavailable" in rec["notes"] else 0
        print(f"[{n}] {rid:4s} {'valid' if rec['model_ir'] else 'FLOOR':5s} first={rec['model_meta']['valid_first_try']!s:5s} "
              f"{[c['ms'] for c in rec['calls']]} {rec['interpretation']}", flush=True)
        if fails >= 3:
            print("ABORT: three consecutive rows could not reach the model")
            return 2
    live_report(data, recs)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--replies", action="store_true")
    ap.add_argument("--exec", action="store_true")
    ap.add_argument("--gateway")
    ap.add_argument("--model", default="mlx/Qwen3.5-9B-MLX-4bit")
    ap.add_argument("--zero", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--fixtures", default=str(FIX))
    args = ap.parse_args()
    data = load(pathlib.Path(args.fixtures))
    if args.gateway:
        return live_mode(data, args.gateway, args.model, args.zero, args.out)
    rc = replies_mode(data) if args.replies or not args.exec else 0
    return rc | (exec_mode(data) if args.exec else 0)


if __name__ == "__main__":
    sys.exit(main())
