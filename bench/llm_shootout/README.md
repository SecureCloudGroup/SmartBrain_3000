# Local LLM shootout

Benchmark local **Ollama** and **oMLX** chat models and rank them on the axes that matter for
an agentic assistant: **load time, latency, throughput, and — most importantly — tool-calling
reliability**. Stdlib-only Python: no `pip install`, runs again later as-is.

> **Platform:** oMLX is Apple-Silicon-only (MLX). The Ollama half runs anywhere Ollama does,
> though the free-RAM check uses macOS `vm_stat` (it degrades gracefully elsewhere).
> Benchmark output under `results/` is machine-specific and is **git-ignored** (never committed).

## Quick start

```bash
cd bench/llm_shootout

# Ollama only — benchmark the models you already have installed:
python3 shootout.py --backend ollama --no-pull

# Full sweep — pull every catalog model, test it, delete it again (disk-safe):
python3 shootout.py --pull

# Include oMLX (Apple Silicon) — needs the key; admin key enables memory clearing:
export OMLX_API_KEY=...                 # required to reach oMLX on :8888
export OMLX_ADMIN_KEY=...               # optional; enables MLX unload between tests
python3 shootout.py --pull
```

Each run writes `results/<timestamp>.json` (raw) and `results/<timestamp>.md` (a ranked
table, fastest decode first). It prints the table to the terminal when done.

## Prerequisites

- **Ollama** running (`ollama serve`) for the Ollama half. `--pull` fetches catalog models.
- **oMLX** running on `:8888` with models loaded, for the MLX half:
  - `OMLX_API_KEY` — **required** to reach oMLX (`/v1`).
  - `OMLX_ADMIN_KEY` — **optional**; enables admin unload so MLX memory is cleared between
    tests (and big models load in isolation under the cap). Defaults to `OMLX_API_KEY`.
- **llmfit** (optional) — if the `llmfit` CLI is on `PATH`, the run is annotated with its
  hardware-aware predictions (see below).

If a backend isn't reachable it's skipped with a message — the run never errors out.

## CLI reference

| Flag | Default | Effect |
|---|---|---|
| `--backend {ollama,omlx,both}` | `both` | Which backend(s) to test. |
| `--pull` | off | Pull catalog Ollama models that aren't installed. |
| `--no-pull` | (default) | Only test models already installed / loaded. |
| `--only <substr>` | — | Test only model ids containing this substring (e.g. `--only qwen2.5`). |
| `--max-disk-gb <N>` | `10` | Keep at least N GB free when pulling (a pull that would breach this is skipped). |
| `--keep-pulled` | off | Do **not** delete models pulled this run (default is reap-after-test). |

Environment: `OLLAMA_BASE` (default `http://localhost:11434`), `OMLX_BASE`
(`http://localhost:8888`), `OMLX_API_KEY`, `OMLX_ADMIN_KEY`.

## What it measures (per model)

| Metric | Meaning |
|---|---|
| `cold_load_s` | time-to-first-token from an **unloaded** state (load + first token) |
| `warm_ttft_s` | median time-to-first-token once resident (steady state) |
| `load_s` | derived `cold_load_s − warm_ttft_s` — the load penalty, **isolated for fairness** |
| `decode_tok_s` | median generation throughput (tokens/sec), model resident |
| `tool_call` | did it emit a **structured** tool call for an obvious request? *(the key metric)* |
| `quality` | loose correctness on the prompt suite (factual / reasoning / instruction / summary) |
| `mem_cleared` | memory verified released after the model was evicted |

## How to read the report

Sample (columns trimmed):

```
| Backend | Model              | Load s | TTFT s | tok/s | Tool call    | Quality | Mem freed |
| ollama  | qwen2.5:7b-instruct| 1.2    | 0.10   | 70    | ✅ structured | ✅✅✅✅  | ✅ 15 GB  |
| omlx    | Qwen2.5-Coder-7B   | 2.8    | 0.28   | 90    | ⚠️ text-JSON  | ✅✅✅✅  | ✅ 18 GB  |
| ollama  | gemma2:9b          | 1.4    | 0.18   | 52    | ❌ none       | ✅·✅✅  | ✅ 14 GB  |
```

- **Tool call** — the column that decides agentic usability:
  - **✅ structured** — emitted a real `tool_calls` object. Usable by an agent. *Pick these.*
  - **⚠️ text-JSON** — printed the tool call as text instead of a structured call. Unusable
    raw (needs a text-tool-call parser to recover). Common on coder models and some MLX setups.
  - **❌ none** — didn't attempt a tool call at all.
- **Quality** — four checks shown as `✅`(pass)/`·`(miss): factual, reasoning, instruction,
  summarize. `✅·✅✅` = 3/4 (missed the reasoning trick question).
- **TTFT** — perceived latency. Sub-0.2 s feels instant; "thinking" models can be 4–16 s.
- **Mem freed** — `✅ <N> GB` means the model was evicted and free RAM verified afterward.
- **llmfit score / tok/s / fit** (only if llmfit is installed) — *predicted* numbers next to
  the empirical ones, for cross-checking.

**Picking a model for an agent:** filter to **✅ structured**, then prefer low **TTFT** + high
**quality** + throughput your use case needs.

## How it works (methodology)

- **Cold vs warm.** Each model is evicted first, then measured cold (load + first token),
  warmed up (one discarded call), then sampled `REPEATS` times for steady-state TTFT and
  throughput (median). `load_s` is the cold/warm difference, isolating the load penalty.
- **Pull → test → reap.** Models *pulled* by a run are deleted after testing, so the catalog
  can be broad while only ~one model occupies disk at a time. Pre-installed models are never
  deleted. `--keep-pulled` disables reaping.
- **Verified memory clearing between models.** Before each model the runner evicts every
  resident model and waits until memory is released — **Ollama** via `ollama stop` (polls
  `ollama ps`), **oMLX** via the admin unload API — so every cold-load starts from a clean
  slate. oMLX eviction also lets large models load in isolation under oMLX's memory cap
  (otherwise they return HTTP 507).
- **Deterministic.** temperature 0, fixed `max_tokens`, identical prompts across models.
- Throughput falls back to effective tok/s when a server delivers the whole answer in one
  burst (e.g. "thinking" models), so the number never spikes to a garbage value.

## llmfit (optional)

If the `llmfit` CLI ("right-size LLM models to your hardware") is installed, the runner:
- prints llmfit's **top tool-use picks for your hardware** at the start (selection guidance), and
- adds **predicted** `score` / `tok/s` / `fit` columns beside the empirical results, matched
  across Ollama/oMLX/HF naming (e.g. `qwen2.5:7b-instruct` ↔ `Qwen/Qwen2.5-7B-Instruct`).

Without llmfit those columns are simply blank — nothing else changes.

## Customize

Edit [`catalog.py`](catalog.py) — it's the only file you normally touch:

- `OLLAMA_MODELS` — `tag -> approx size GB` to pull/test (used for the disk guard).
- `OMLX_MODELS` — pin exact ids, or leave empty to auto-discover loaded chat models.
- `PROMPTS` / `PROMPT_EXPECT` — the prompt suite and its loose correctness checks.
- `TOOL_SPEC` / `TOOL_PROMPT` / `EXPECTED_TOOL` — the tool-calling probe.

Tuning constants live at the top of [`shootout.py`](shootout.py): `MAX_TOKENS`, `REPEATS`,
`REQUEST_TIMEOUT`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Ollama is not running` | Start it: `ollama serve`. |
| `oMLX not reachable / no models` | Start oMLX, load models, and `export OMLX_API_KEY=...`. |
| `oMLX admin login failed` | Set `OMLX_ADMIN_KEY` (or `OMLX_API_KEY`) to an admin-capable key; without it MLX models aren't evicted and big ones may HTTP 507. |
| oMLX model shows `⚠ERR … 507` | Over the memory cap with other models resident — enable admin eviction (above) so it loads in isolation. |
| `… not present (use --pull)` | The model isn't installed; add `--pull`, or install it manually. |
| Pull skipped `(disk guard)` | Not enough free disk; free space or lower `--max-disk-gb`. |
| Garbage/huge `tok/s` | Shouldn't happen (burst fallback handles it); if it does, the server isn't streaming incrementally — file it. |
