#!/usr/bin/env python3
"""Local LLM shootout — benchmark Ollama + oMLX chat models on this Mac.

Both backends are driven through their OpenAI-compatible ``/v1/chat/completions`` API,
so the only per-backend differences are the base URL/auth and how a model is pulled and
unloaded. Stdlib-only (no pip install) so it runs again later with zero setup.

Per model it measures, on a fixed prompt suite (see catalog.py):
  - cold_load_s   : time-to-first-token from an UNLOADED state (load + first token)
  - warm_ttft_s   : median time-to-first-token once resident (steady state)
  - load_s        : derived cold_load_s - warm_ttft_s (the load penalty, isolated)
  - decode_tok_s  : median generation throughput (tokens/sec), model resident
  - tool_call     : did it emit a STRUCTURED tool_call for an obvious request? (key metric)
  - quality       : simple correctness checks on the prompt suite

Usage:
  python shootout.py --pull                 # pull the Ollama catalog, then run everything
  python shootout.py --no-pull              # only models already present
  python shootout.py --backend ollama       # one backend
  python shootout.py --only qwen             # filter model ids by substring
  OMLX_API_KEY=... python shootout.py        # needed to reach oMLX on :8888

Output: results/<timestamp>.json  and  results/<timestamp>.md
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import shutil
import statistics
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import catalog
import llmfit

OMLX_ADMIN_KEY = os.environ.get("OMLX_ADMIN_KEY", "")  # falls back to OMLX_API_KEY (same key on this host)

OLLAMA_BASE = os.environ.get("OLLAMA_BASE", "http://localhost:11434")
OMLX_BASE = os.environ.get("OMLX_BASE", "http://localhost:8888")
OMLX_KEY = os.environ.get("OMLX_API_KEY", "")

MAX_TOKENS = 256          # generation cap for throughput (bounded, comparable)
REPEATS = 3               # throughput samples per model (median taken)
REQUEST_TIMEOUT = 300.0   # per-call hard ceiling (seconds)
THROUGHPUT_PROMPT = "Write about 150 words explaining what a hash map is and when to use one."
_HERE = Path(__file__).resolve().parent


# --- HTTP (stdlib) -----------------------------------------------------------

def _headers(key: str) -> dict[str, str]:
    """JSON content-type plus a Bearer token when a key is supplied."""
    h = {"Content-Type": "application/json"}
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def _post(url: str, payload: dict, key: str, timeout: float = REQUEST_TIMEOUT) -> dict:
    """POST JSON, return the parsed JSON response (non-streaming)."""
    assert url and isinstance(payload, dict), "url + payload required"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=_headers(key), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    assert isinstance(data, dict), "response must be a JSON object"
    return data


def _stream_chat(base: str, key: str, model: str, prompt: str) -> tuple[float, float, str]:
    """Stream a chat completion; return (ttft_s, decode_tok_s, text).

    ttft_s = seconds to the first content token; decode_tok_s = generated tokens divided
    by the post-first-token time. Token count comes from the final usage chunk when present,
    else the streamed-delta count (a close approximation for one-token-per-delta servers).
    """
    assert base and model, "base + model required"
    payload = {
        "model": model, "stream": True, "temperature": 0, "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
        "stream_options": {"include_usage": True},
    }
    req = urllib.request.Request(
        f"{base}/v1/chat/completions", data=json.dumps(payload).encode("utf-8"),
        headers=_headers(key), method="POST",
    )
    t0 = time.monotonic()
    ttft = None
    deltas = 0
    completion_tokens = 0
    text: list[str] = []
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        for raw in resp:  # iterates SSE lines; bounded by max_tokens server-side
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                break
            try:
                obj = json.loads(chunk)
            except ValueError:
                continue
            usage = obj.get("usage")
            if isinstance(usage, dict) and usage.get("completion_tokens"):
                completion_tokens = int(usage["completion_tokens"])
            choices = obj.get("choices") or []
            piece = choices[0].get("delta", {}).get("content") if choices else None
            if piece:
                if ttft is None:
                    ttft = time.monotonic() - t0
                text.append(piece)
                deltas += 1
    t_done = time.monotonic()
    ttft = ttft if ttft is not None else (t_done - t0)
    tokens = completion_tokens or deltas
    decode_time = t_done - t0 - ttft
    # A "thinking" model (or a non-incremental server) delivers the whole answer in one burst:
    # decode_time -> ~0 and tokens/decode_time explodes. Fall back to effective throughput over
    # the full call so the number stays sane and comparable instead of a garbage spike.
    if decode_time < 0.05 or (decode_time > 0 and tokens / decode_time > 5000):
        rate = tokens / max(t_done - t0, 1e-3)
    else:
        rate = tokens / decode_time
    return ttft, rate, "".join(text)


# --- Ollama backend ----------------------------------------------------------

def ollama_running() -> bool:
    """True if the Ollama daemon answers on OLLAMA_BASE."""
    try:
        urllib.request.urlopen(f"{OLLAMA_BASE}/api/tags", timeout=3).read()
        return True
    except (urllib.error.URLError, OSError):
        return False


def ollama_present() -> dict[str, float]:
    """Map of installed Ollama model tag -> size GB."""
    try:
        data = json.loads(urllib.request.urlopen(f"{OLLAMA_BASE}/api/tags", timeout=5).read())
    except (urllib.error.URLError, OSError, ValueError):
        return {}
    return {m["name"]: round(int(m.get("size", 0)) / 1e9, 1) for m in data.get("models", [])}


def ollama_pull(model: str) -> bool:
    """Pull a model tag via the Ollama CLI; True on success."""
    assert model, "model required"
    print(f"  pulling {model} ...", flush=True)
    proc = subprocess.run(["ollama", "pull", model], capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0:
        print(f"  pull FAILED: {proc.stderr.strip()[:200]}", flush=True)
    return proc.returncode == 0


def ollama_unload(model: str) -> None:
    """Evict the model from memory so the next call measures a true cold load."""
    assert model, "model required"
    subprocess.run(["ollama", "stop", model], capture_output=True, text=True, timeout=60)
    time.sleep(1.0)  # let the runner release the weights


def ollama_ps_names() -> list[str]:
    """Models currently resident in memory (per `ollama ps`)."""
    try:
        out = subprocess.run(["ollama", "ps"], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    names = []
    for line in out.splitlines()[1:]:  # skip header; bounded by resident-model count
        parts = line.split()
        if parts:
            names.append(parts[0])
    return names


def ensure_all_unloaded(timeout: float = 90.0) -> bool:
    """Stop every resident model and wait until memory is actually released (ps empty).

    Guarantees a clean slate between models so each cold load is truly cold and RAM
    is not held by the previous model — the fairness requirement.
    """
    assert timeout > 0, "timeout must be positive"
    for m in ollama_ps_names():
        subprocess.run(["ollama", "stop", m], capture_output=True, text=True, timeout=60)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:  # bounded by timeout (P10 #2)
        if not ollama_ps_names():
            time.sleep(1.0)  # brief settle so RSS/free-RAM reflects the release
            return True
        time.sleep(1.0)
    return False


def ollama_rm(model: str) -> None:
    """Delete a pulled model from disk (pull-test-reap, to free space for the next)."""
    assert model, "model required"
    subprocess.run(["ollama", "rm", model], capture_output=True, text=True, timeout=120)


def free_ram_gb() -> float:
    """Approx free system RAM (free + inactive + speculative pages) via vm_stat."""
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return 0.0
    page_match = re.search(r"page size of (\d+) bytes", out.splitlines()[0]) if out else None
    page = int(page_match.group(1)) if page_match else 16384
    pages = 0
    for key in ("Pages free", "Pages inactive", "Pages speculative"):  # bounded
        m = re.search(rf"{key}:\s+(\d+)\.", out)
        if m:
            pages += int(m.group(1))
    return round(pages * page / 1e9, 1)


# --- oMLX backend ------------------------------------------------------------

def omlx_present() -> list[str]:
    """Chat model ids oMLX currently reports (embedding ids filtered out by name)."""
    try:
        req = urllib.request.Request(f"{OMLX_BASE}/v1/models", headers=_headers(OMLX_KEY))
        data = json.loads(urllib.request.urlopen(req, timeout=5).read())
    except (urllib.error.URLError, OSError, ValueError):
        return []
    ids = [m["id"] for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]
    return [i for i in ids if not any(h in i.lower() for h in ("embed", "bge", "rerank"))]


def omlx_admin_session():
    """Log in to oMLX admin (api_key == OMLX_ADMIN_KEY or OMLX_API_KEY) and return a
    cookie-bearing opener for the unload API, or None if login fails. This is what lets the
    framework force-evict MLX models between tests (true memory clearing, like Ollama)."""
    key = OMLX_ADMIN_KEY or OMLX_KEY
    if not key:
        return None
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    body = json.dumps({"api_key": key, "remember": True}).encode("utf-8")
    req = urllib.request.Request(f"{OMLX_BASE}/admin/api/login", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        resp = json.loads(opener.open(req, timeout=10).read())
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return opener if resp.get("success") else None


def omlx_loaded(opener) -> list[str]:
    """Model ids currently resident in oMLX (per the admin model list)."""
    try:
        data = json.loads(opener.open(f"{OMLX_BASE}/admin/api/models", timeout=10).read())
    except (urllib.error.URLError, OSError, ValueError):
        return []
    ms = data if isinstance(data, list) else data.get("models", data.get("data", []))
    return [m.get("id") or m.get("name") for m in ms
            if isinstance(m, dict) and m.get("loaded", m.get("is_loaded"))]


def omlx_unload_all(opener, timeout: float = 90.0) -> bool:
    """Unload every resident oMLX model so the next test loads in isolation (memory cleared)."""
    if opener is None:
        return False
    for mid in omlx_loaded(opener):  # bounded by resident count
        req = urllib.request.Request(
            f"{OMLX_BASE}/admin/api/models/{urllib.parse.quote(mid)}/unload", method="POST")
        try:
            opener.open(req, timeout=timeout)
        except (urllib.error.URLError, OSError):
            pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:  # bounded by timeout (P10 #2)
        if not omlx_loaded(opener):
            time.sleep(1.0)  # settle so free-RAM reflects the release
            return True
        time.sleep(1.0)
    return False


# --- measurement -------------------------------------------------------------

def _tool_probe(base: str, key: str, model: str) -> dict:
    """Does the model emit a STRUCTURED tool_call (vs printing JSON as text)?"""
    payload = {
        "model": model, "temperature": 0, "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": catalog.TOOL_PROMPT}],
        "tools": catalog.TOOL_SPEC, "tool_choice": "auto",
    }
    try:
        data = _post(f"{base}/v1/chat/completions", payload, key)
    except (urllib.error.URLError, OSError, ValueError, AssertionError) as exc:
        return {"structured": False, "correct_name": False, "error": str(exc)[:160]}
    msg = (data.get("choices") or [{}])[0].get("message", {})
    calls = msg.get("tool_calls") or []
    names = [c.get("function", {}).get("name") for c in calls]
    leaked = (not calls) and ('"name"' in (msg.get("content") or "") and "get_weather" in (msg.get("content") or ""))
    return {
        "structured": bool(calls),
        "correct_name": catalog.EXPECTED_TOOL in names,
        "leaked_as_text": bool(leaked),  # tried to call but printed JSON instead
    }


def _quality(base: str, key: str, model: str) -> dict:
    """Run the prompt suite once each; loose correctness via PROMPT_EXPECT."""
    out: dict[str, object] = {}
    for p in catalog.PROMPTS:  # bounded by the catalog
        try:
            _, _, text = _stream_chat(base, key, model, p["text"])
        except (urllib.error.URLError, OSError, AssertionError) as exc:
            out[p["key"]] = f"error: {str(exc)[:80]}"
            continue
        expect = catalog.PROMPT_EXPECT.get(p["key"])
        out[p["key"]] = "pass" if (expect is None or expect in text.lower()) else "miss"
    return out


def measure_model(backend: str, base: str, key: str, model: str, size_gb: float, omlx_admin=None) -> dict:
    """Full measurement for one model. Never raises — records errors in the result."""
    assert backend and model, "backend + model required"
    print(f"[{backend}] {model} ...", flush=True)
    result: dict[str, object] = {"backend": backend, "model": model, "size_gb": size_gb, "errors": []}
    try:
        if backend == "ollama":
            ensure_all_unloaded()  # clear ANY resident model first (fairness: a true cold slate)
            result["free_ram_before_gb"] = free_ram_gb()
        elif backend == "omlx" and omlx_admin is not None:
            omlx_unload_all(omlx_admin)  # evict resident MLX models so THIS one loads in isolation
            result["free_ram_before_gb"] = free_ram_gb()
        cold_ttft, _, _ = _stream_chat(base, key, model, "Say hello.")
        result["cold_load_s"] = round(cold_ttft, 2)
        _stream_chat(base, key, model, "Say hi.")  # warmup (discarded)
        ttfts, toks = [], []
        for _ in range(REPEATS):  # bounded
            ttft, tps, _ = _stream_chat(base, key, model, THROUGHPUT_PROMPT)
            ttfts.append(ttft)
            toks.append(tps)
        warm_ttft = statistics.median(ttfts)
        result["warm_ttft_s"] = round(warm_ttft, 3)
        result["load_s"] = round(max(cold_ttft - warm_ttft, 0.0), 2)
        result["decode_tok_s"] = round(statistics.median(toks), 1)
        result["tool_call"] = _tool_probe(base, key, model)
        result["quality"] = _quality(base, key, model)
    except (urllib.error.URLError, OSError, ValueError, AssertionError) as exc:
        result["errors"].append(str(exc)[:200])  # type: ignore[union-attr]
    return result


# --- driver + reporting ------------------------------------------------------

def _select(present: dict | list, catalog_ids, only: str) -> list[str]:
    """Pinned catalog ids (or all present if unpinned), filtered by the --only substring."""
    ids = list(catalog_ids) if catalog_ids else list(present)
    return [m for m in ids if (not only or only.lower() in m.lower())]


def run_ollama(do_pull: bool, only: str, max_disk_gb: float, reap: bool) -> list[dict]:
    """Benchmark the Ollama catalog: pull (disk-guarded) → measure → reap pulled models."""
    if not ollama_running():
        print("Ollama is not running — start it with `ollama serve` (skipping).")
        return []
    pre_installed = set(ollama_present())  # never delete models the user already had
    results: list[dict] = []
    for model, size in catalog.OLLAMA_MODELS.items():  # bounded by catalog
        if only and only.lower() not in model.lower():
            continue
        pulled = False
        if model not in ollama_present():
            if not do_pull:
                print(f"[ollama] {model} not present (use --pull) — skipping")
                continue
            free_gb = shutil.disk_usage("/").free / 1e9
            if free_gb - size < max_disk_gb:
                print(f"[ollama] {model}: only {free_gb:.0f} GB free — skipping pull (disk guard)")
                continue
            if not ollama_pull(model):
                continue
            pulled = True
        size_gb = ollama_present().get(model, size)
        result = measure_model("ollama", OLLAMA_BASE, "", model, size_gb)
        cleared = ensure_all_unloaded()  # evict + verify memory released BEFORE the next model
        result["mem_cleared"] = cleared
        result["free_ram_after_gb"] = free_ram_gb()
        results.append(result)
        if pulled and reap and model not in pre_installed:
            print(f"  reaping {model} (free disk for the next) ...", flush=True)
            ollama_rm(model)
    return results


def run_omlx(only: str) -> list[dict]:
    """Benchmark loaded oMLX chat models, evicting between each when admin login succeeds."""
    present = omlx_present()
    if not present:
        print("oMLX not reachable / no models (set OMLX_API_KEY and load models) — skipping.")
        return []
    admin = omlx_admin_session()
    if admin is None:
        print("oMLX admin login failed — testing WITHOUT eviction (models accumulate; big ones may 507). "
              "Set OMLX_ADMIN_KEY to enable memory clearing.")
    results: list[dict] = []
    for m in _select(present, catalog.OMLX_MODELS, only):  # bounded by discovered models
        result = measure_model("omlx", OMLX_BASE, OMLX_KEY, m, 0.0, omlx_admin=admin)
        if admin is not None:
            result["mem_cleared"] = omlx_unload_all(admin)  # evict + verify before the next model
            result["free_ram_after_gb"] = free_ram_gb()
        results.append(result)
    return results


def _host_label() -> str:
    """Best-effort host description for the report header (CPU + RAM), detected not hardcoded."""
    cpu, ram = "", ""
    try:
        cpu = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        mem = subprocess.run(["sysctl", "-n", "hw.memsize"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        if mem.isdigit():
            ram = f"{int(mem) / 1e9:.0f} GB"
    except (OSError, subprocess.SubprocessError):
        pass
    parts = [p for p in (cpu, ram) if p]
    return " · ".join(parts) if parts else "this host"


def write_reports(results: list[dict], stamp: str, preds: list[dict]) -> Path:
    """Write the raw JSON + a ranked markdown table (with llmfit columns); return the md path."""
    out_dir = _HERE / "results"
    out_dir.mkdir(exist_ok=True)
    (out_dir / f"{stamp}.json").write_text(json.dumps(results, indent=2))
    lines = [f"# LLM shootout — {stamp}", "", f"{_host_label()} · temperature 0 · "
             f"max_tokens {MAX_TOKENS} · {REPEATS} throughput samples (median). Memory cleared "
             "between every model (Ollama: `ollama ps` empty; oMLX: admin unload). llmfit "
             "columns are PREDICTED (its model database), the rest are EMPIRICAL.", "",
             "| Backend | Model | Load s | Warm TTFT s | Decode tok/s | Tool call | Quality | Mem freed "
             "| llmfit score | llmfit tok/s | fit |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(results, key=lambda x: (-(x.get("decode_tok_s") or 0))):  # fastest first
        tc = r.get("tool_call") or {}
        tool = "✅ structured" if tc.get("correct_name") else ("⚠️ text-JSON" if tc.get("leaked_as_text") else "❌ none")
        q = r.get("quality") or {}
        qual = "".join("✅" if v == "pass" else "·" for v in q.values()) or "—"
        err = " ⚠ERR" if r.get("errors") else ""
        mem = "✅" if r.get("mem_cleared") else ("⚠️" if r.get("free_ram_after_gb") is not None else "—")
        if r.get("free_ram_after_gb") is not None:
            mem += f" {r.get('free_ram_after_gb')} GB"
        lf = llmfit.match(r["model"], preds) or {}
        lf_s = f"{lf.get('score'):.0f}" if lf.get("score") is not None else "—"
        lf_t = f"{lf.get('estimated_tps'):.0f}" if lf.get("estimated_tps") is not None else "—"
        lf_f = lf.get("fit_level", "—")
        lines.append(f"| {r['backend']} | {r['model']}{err} | "
                     f"{r.get('load_s','—')} | {r.get('warm_ttft_s','—')} | {r.get('decode_tok_s','—')} | "
                     f"{tool} | {qual} | {mem} | {lf_s} | {lf_t} | {lf_f} |")
    report = out_dir / f"{stamp}.md"
    report.write_text("\n".join(lines) + "\n")
    return report


def main() -> None:
    """Parse args, fetch llmfit guidance, run the selected backends, write the report."""
    ap = argparse.ArgumentParser(description="Local LLM shootout (Ollama + oMLX).")
    ap.add_argument("--backend", choices=["ollama", "omlx", "both"], default="both")
    ap.add_argument("--pull", dest="pull", action="store_true", help="pull missing Ollama catalog models")
    ap.add_argument("--no-pull", dest="pull", action="store_false")
    ap.add_argument("--only", default="", help="filter model ids by substring")
    ap.add_argument("--max-disk-gb", type=float, default=10.0, help="keep at least this many GB free when pulling")
    ap.add_argument("--keep-pulled", dest="reap", action="store_false",
                    help="do NOT delete models pulled this run (default: reap to free disk)")
    ap.set_defaults(pull=False, reap=True)
    args = ap.parse_args()

    # llmfit guides selection (which models are worth testing) and annotates the report
    # with predicted score/speed/fit. Optional: detected, never auto-installed.
    have_llmfit = llmfit.available()
    preds = llmfit.predictions() if have_llmfit else []
    if preds:
        print(f"llmfit: {len(preds)} models fit this hardware. Top tool-use picks:")
        for p in llmfit.top_tool_use(preds, 6):
            print(f"  {p.get('score',0):5.1f}  {p.get('estimated_tps',0):4.0f} tok/s  "
                  f"{p.get('memory_required_gb',0):5.1f} GB  {p.get('name','?')[:54]}")
        print()
    elif not have_llmfit:
        print("llmfit not found (optional) — install the llmfit CLI to add predicted "
              "fit/speed/score columns. The benchmark runs fine without it.\n")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    results: list[dict] = []
    if args.backend in ("ollama", "both"):
        results += run_ollama(args.pull, args.only, args.max_disk_gb, args.reap)
    if args.backend in ("omlx", "both"):
        results += run_omlx(args.only)
    if not results:
        print("No models measured. Check Ollama is running / OMLX_API_KEY is set / --pull.")
        return
    report = write_reports(results, stamp, preds)
    print(f"\nDone — {len(results)} model(s). Report: {report}")
    print(report.read_text())


if __name__ == "__main__":
    main()
