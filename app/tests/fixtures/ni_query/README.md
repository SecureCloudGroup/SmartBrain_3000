# Query layer fixtures (Round 20, Q3a)

Frozen inputs for `tools/ni-query-eval.py` and `tests/test_ni_query_*.py` — the query layer
(`smartbrain_3000/ni_query`, docs/internal/ni-format.md §36) is measured on these offline.

| file | what |
|---|---|
| `gold.jsonl` | 109 hand-labeled gold Query IRs over the 111 live rows of the blind sets A–E (Q1, 2026-10-09): `id, set, n, kind, ask, verdict, source_id, params_used, ir, note`. A non-empty `note` marks a row the IR cannot express or the chosen source cannot serve (source_wrong, params_wrong, map, compare …) — scored apart, never against the model. |
| `replies_fewshot.jsonl` | the local 9B's recorded replies (Q2, prompt design v2, few-shot k = 3) on the 79 TEST rows: `id, parsed` (the valid object after at most one retry, or null), `reply_raw` (first try). |
| `replies_zero.jsonl` | the same, zero-shot, all 109 rows. |
| `split.json` | DEV (30) / TEST (79), stratified by kind, seed 20261009; the reference date of each set (the prompt's "Reference instant") and the zone. |
| `pool.jsonl` | the few-shot pool: the 22 clean DEV rows (`id, kind, ask, ref, menu, ir`), `menu` = the abbreviated answer menu of the row's source. |
| `library.json` | the declared answers, parameters and name of the 42 Library sources the gold rows use (SmartBrain_Library as of 2026-10-07). |
| `samples/` | the payloads fetched from the rows' tapped URLs on 2026-10-09 (`index.json`: row → file, fetch instant). Capped at 300 KB each: E38's Florida statewide alerts (`nws-alerts-state__6106a727.json`, 889,893 bytes) was dropped; E14's moon-phase fetch failed at the time. |
| `exec_gold.json` | the Q2 harness's own execution of each gold IR on its sample (row ids, first 12; row count; count) — the port check for `apply_query`. |
| `prompt_sha.json` | the Q2 prompt fingerprints per row (zero-shot, few-shot) and the few-shot example ids — the port check for `ni_query.prompt`. |
