# Query layer fixtures (Round 20, Q3a)

Frozen inputs for `tools/ni-query-eval.py` and `tests/test_ni_query_*.py` — the query layer
(`smartbrain_3000/ni_query`, docs/internal/ni-format.md §36) is measured on these offline.

| file | what |
|---|---|
| `gold.jsonl` | 109 hand-labeled gold Query IRs over the 111 live rows of the blind sets A–E (Q1, 2026-10-09): `id, set, n, kind, ask, verdict, source_id, params_used, ir, note`. A non-empty `note` marks a row the IR cannot express or the chosen source cannot serve (source_wrong, params_wrong, map, compare …) — scored apart, never against the model. |
| `replies_fewshot.jsonl` | the local 9B's recorded replies (Q2, prompt design v2, few-shot k = 3) on the 79 TEST rows: `id, parsed` (the valid object after at most one retry, or null), `reply_raw` (first try). |
| `replies_zero.jsonl` | the same, zero-shot, all 109 rows. |
| `split.json` | DEV (30) / TEST (79), stratified by kind, seed 20261009; the reference date of each set (the prompt's "Reference instant") and the zone. |
| `pool.jsonl` | MOVED (Q3b) to `smartbrain_3000/ni_query/pool.jsonl` — it ships with the app as the flow's few-shot pool: the 22 clean DEV rows (`id, kind, ask, ref, menu, ir`), `menu` = the abbreviated answer menu of the row's source. The tool and the tests read it there. |
| `library.json` | the declared answers, parameters and name of the 42 Library sources the gold rows use (SmartBrain_Library as of 2026-10-07). |
| `samples/` | the payloads fetched from the rows' tapped URLs on 2026-10-09 (`index.json`: row → file, fetch instant). Capped at 300 KB each: E38's Florida statewide alerts (`nws-alerts-state__6106a727.json`, 889,893 bytes) was dropped. E14's moon-phase fetch failed at the time; since Q3c its sample is the set E run's own four `phasedata` rows (`blind50e_run.json`, the host does not resolve from the recording machine). |
| `gold_f.jsonl` | Q3c: the 31 hand-labeled gold IRs of set F's live rows (the lead, 2026-10-12; ids F1…F50, same schema; noted rows scored apart). Set F is development data. |
| `library_f.json` | Q3c: the declared answers, parameters and name of the 24 sources the set F rows use (SmartBrain_Library main + #12, 2026-10-12). |
| `samples_f/` | Q3c: the five wrong set F cards' tapped URLs fetched once on 2026-10-12 (F19 NHL scoreboard, F21 Hacker News `query=Rust`, F22 USGS M2.5+ past day, F24 CoinGecko markets, F26 NIFC Oregon) and `F21-run`: the F run's own 20 Hacker News rows (one title posted three times — the D defect's data). |
| `runs_f.json` | Q3c: set F's reference date and, per row, the texts the run's address consumed (the tapped pick's label parts and word-valued params) — the eval's `consumed` for set F. |
| `asked_subcategories.json` | Q3c: the subcategories the Library (main + #12) classifies each gold / gold_f ask under and their taxonomy keywords — the evidence rule 4 of the grounded filter values reads (the flow classifies the ask itself). |
| `library_codes.json` | Q3d: the `library_codes` rows of a pack built from the Library's answers spec v1.5 tree (2026-10-12): `nws_event`, the 111 NWS alert event types of `/alerts/types` + "Test Message" — the values rule 3 of the grounded filter values reads (`tests/test_ni_query_codes.py`). |
| `library_nws.json` | Q3d: the name, params and answers of `nws-alerts-point`, `nws-alerts-state` and `nws-tropical-alerts-point` from that pack — the event cell declares `codes: "nws_event"`, each list answer `filter: {properties.status = Actual}`. |
| `nws_alerts_national.json` | Q3d: the national active-alerts response the Library's live probe recorded (GET `api.weather.gov/alerts/active`, 2026-10-12 23:15Z), cut to at most three alerts per event (50 of 216; the one `Test` alert kept) and to the properties the answers read (event, status, severity, areaDesc, expires). |
| `exec_gold.json` | the Q2 harness's own execution of each gold IR on its sample (row ids, first 12; row count; count) — the port check for `apply_query`. |
| `prompt_sha.json` | the Q2 prompt fingerprints per row (zero-shot, few-shot) and the few-shot example ids — the port check for `ni_query.prompt`. |
