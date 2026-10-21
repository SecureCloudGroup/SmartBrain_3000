# NI eval harness (plan S0)

Measures question -> card quality so each Neural Interface phase is gated on numbers.
Tool: `tools/ni-eval.py`. Data: `app/tests/ni_eval/`. CI test: `app/tests/test_ni_eval_replay.py`.

## Gold

`app/tests/ni_eval/gold/gold.json`, one record per ask, rebuilt by `ni-eval.py convert` from the
committed blind sets (never edit by hand; fix the source or the converter):

| set | source | asks | verdicts |
|---|---|---|---|
| A | blind-50 `ni_blind/2026-10-05` (`review.md` is the authority) | 50 | 23 live cards: 13 right, 6 partial, 4 wrong; the other 27 have `baseline.outcome` links / source / needs-key |
| B-E | `ni_query/gold.jsonl` (live rows only) | 87 | as recorded |
| F | `ni_query/gold_f.jsonl` (live rows only) | 31 | as recorded |
| G | `ni_blind/live_2026-10-12g` | 7 | none recorded, null |

Not yet in the gold: the non-live asks of sets B-G (no asks file is committed for them), and the
sealed held-out 100 (to be written by T and kept out of the repo until frozen). Fields: `id`, `ask`,
`kind`, `constraints {places, window, threshold, compare_n, units, entities}`,
`acceptable_sources` (hosts or Library ids; set only where the human said right), `expected_outcome`
(card / decline / links; null when unknown), `forbidden_outcomes`, `human_verdict` (right / partial /
wrong / null), `defect_class`, `baseline`, `harness`. Constraints are parsed from the ask text only.
`harness: {"mode": "url", "source_url": ...}` drives `run_flow` with a given source and no Library
(used by the CI cassette); null means the product path (`ni-live-e2e.run_one`, needs `--pack-dir`).

## Scoring

`score_ask(run, gold)` is pure. Classes: right / partial / wrong / links (links, needs a
key) / declined / waiting (parked at the source pick) / error. A card is wrong when the gold forbids a card or a checkable constraint fails
(threshold not in the request, compare entities missing, units, source not acceptable); partial when
only some asked places show; right when checks ran and passed. With nothing checkable it is
`partial` with `needs_review`. The same card a human already judged (same source and card text) takes
the human verdict, so replaying the 2026-10-05 results reproduces 13 / 6 / 4 / 27. `aggregate` gives
counts and R, P, W, L rates per kind and overall (L = links + declined). The table has a `waiting`
column: runs parked at the source pick (outcome `waiting-pick` = state `source` with the
`awaiting_pick` marker). They are not links; older results with outcome `source` still score as links.

## Record a live baseline (T's machine, real model)

On macOS use the app's bundled Python for `record`: the system `python3` can lack `cryptography`.
It is at `~/Library/Application Support/SmartBrain/native/versions/<ver>/python/bin/python3`
(substitute it for `python3` below).

```
cd SmartBrain_3000
PYTHONPATH=app python3 tools/ni-eval.py record \
  --pack-dir <dir holding library/> --cassette-dir app/tests/ni_eval/cassettes/v0.26.0-qwen9b \
  --model mlx/Qwen3.5-9B-MLX-4bit --bifrost http://127.0.0.1:38080 \
  --out /tmp/baseline-v0.26.0.json
```

Add `--set A,B` or `--only "<text or id>"` to narrow, and `--flow v1|v2` to run under that `ni_flow`
flag (it sets `SMARTBRAIN_NI_FLOW` for the run; replay takes it too, default is the stored flag, v1). For a cloud model pass its id as `--model`
(the gateway must route it). Output: one `<id>.json` per ask (model replies, fetch responses; headers
are never stored and secret-looking query values are redacted) and the table printed at the end.
Review cassettes for personal data before committing them. Asks in sets B-G have no recorded
card to carry a verdict, but the constraint checks still run: in the live run most of them got a
definite right or wrong, not partial / unchecked. About 29 of the 175 rows were flagged
`needs_review` (reason `unchecked`); review `scored[].needs_review` in the `--out` file and add
verdicts to the converter inputs.

## Replay (offline, deterministic)

```
cd app && PYTHONPATH=. python3 ../tools/ni-eval.py replay \
  --gold tests/ni_eval/gold/ci.json --cassette-dir tests/ni_eval/cassettes/ci
```

Model replies are served in recorded order (a changed prompt is counted as `cassette_drift` and
served anyway); fetches are served by function + URL; anything unrecorded raises `CassetteMiss` and the
ask scores `error`. `pytest tests/test_ni_eval_replay.py` runs the committed `ci` cassette (five asks
built by hand from `fixtures/ni_flow`, no model) through the real `run_flow` in seconds.
Library-mode cassettes also need `--pack-dir` at replay (the pack is not recorded).

## Gating a phase

A phase PR attaches the replay table next to the committed baseline for the same gold subset.
No merge if the wrong rate rises against the baseline or right falls; the per-phase targets in
`neural-interface/plan.md` section 7 are read from this table (for example S2: blind-50 wrong 4 to at
most 1, right not below 13). A change that needs a new target first records a new baseline.
Fix rounds add a class to the gold, never a named example.

## Seams a later PR should add

- an injectable clock (prompts and windows depend on today's date, so old cassettes drift);
- a `fetch` seam on the engine's refresh path and on the page/image/bytes fetches (today patched at
  `netguard`, which cannot replay binary or page fetches);
- a `run_flow` result that exposes the final card text and chosen source, so scoring need not
  rebuild them from snapshots.

## S2 shadow (verify constraint classes, recorded not enforced)

`app/smartbrain_3000/ni_verify/` now holds the verify step (moved out of `ni_flow.py`) and four constraint
classes in `classes.py`, each a pure function over (the ask's constraints, the built result, the source):

| class | reads from the ask | objects when |
|---|---|---|
| window | tonight, today, tomorrow, yesterday, last night, this weekend, in the user's zone (tonight is 17:00-06:00 and after midnight is still last evening's night) | the result shows moments and none falls in the window |
| threshold | above / over / at least / below / under / `M4.5+` | a column the bound is about (earthquakes read `mag`) has a value that fails it; with no column, a feed whose own floor (`M2.5+`, `2.5_day`) is below an "above" bound |
| compare count | `A vs B`, `compare A and B`, `difference between A and B` | fewer of the N named things appear in the result's words |
| source scope | a count ("how many", "number of", "total") over a basin, region, the nation or the world | the source answers for one point (place scope, lat/lon/zip/station params) |

A class stays silent when the result carries nothing it can judge. In the flow the reasons ride the flow
record as `_verify_shadow` (a dict class -> reasons, `{}` when none object) next to the existing verify; a
build is never refused on them in this phase. Measure offline:

```
cd app && PYTHONPATH=. python3 tests/ni_eval/shadow_replay.py     # tests/test_ni_verify_blind50.py asserts it
```

The 2026-10-05 results carry no clock; the replay uses 02:45 UTC Oct 6 (= 21:45 America/Chicago), read from
the newest USGS quake and the Minneapolis hourly card's first row, and Chicago as the user zone.

| blind-50 live cards (23) | result |
|---|---|
| right (13) with any reason (false rejects) | 0 |
| wrong (4) with a reason | 4 of 4: NHL tonight -> window, earthquakes above 4.0 -> threshold (18 of 38 mag values below 4), Denver vs Salt Lake City -> compare (1 of 2), Pacific hurricanes -> scope |
| partial (6) with a reason | 0 (none of their defects is in these four classes) |

Also: the 54 builds the `test_ni_flow_frame_verify` table accepts (recorded Library samples, frozen clock)
record `_verify_shadow == {}`. Not measured: sets B-G have no recorded card text, and the held-out 100 is
not yet written; rerun this table when `ni-eval.py record` produces cassettes for them.
