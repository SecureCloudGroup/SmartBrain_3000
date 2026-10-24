# Neural Interface format

The contract of the Neural Interface (NI) subsystem: the item spec, the scene grammar, the pipeline, the
data contract, the lifecycle, the creation flow and the surfaces around them. The Python validators
(`app/smartbrain_3000/ni.py`) and the TypeScript renderer (`web/src/lib/ni/`) both implement this
document. Where they disagree with it, this document wins and both are bugs. Section numbers are stable:
code and tests cite them (`§13`, `§29`, `§36`), so a new topic takes the next number and a section is
never renumbered.

How a change is measured is in `docs/internal/ni-eval.md`; the gold set is `app/tests/ni_eval/gold/`.
`docs/internal/ni-cases.md` points there.

## Design laws

- **Deterministic core, AI at the edges.** Models run at design time, commissioning and repair time. The
  runtime path (fetch, extract, transform, contract check, bind, render) is pure deterministic code: the
  same payload always produces the same scene.
- **Authorization is not trust.** Choosing or keeping a source authorizes the connection; fetched content
  stays untrusted forever. Every model output derived from fetched bytes is a closed-schema, egress-inert
  artifact (scene trees, extract paths, transform ops), never a URL, header, schedule or free-form markup.
- **Template-native.** Every item is an instantiated template. `params` declare the slots; a local item has
  them all filled. Export is a strip of filled values back to slots, never a schema change.
- **Nothing renders that did not validate.** The scene validator and the payload binder run server-side on
  every snapshot write; the client renderer additionally refuses unknown node types.
- **A decline is acceptable, a wrong card is the failure.** An ask the system cannot answer reliably ends in
  an honest stop (§39), never a plausible-looking card. Cards that came from an open path wait for the
  user's Keep (§33).
- **No dead ends.** Every stopped card shows a reason and a way forward (a question, a reopen, or an
  action). The oversight plane audits the cards the board actually serves for it (§29, Oversight).

## 1. Storage

The house convention: a sealed body plus plaintext operational columns.

- `ni_items` - plaintext: `id TEXT PRIMARY KEY`, `enabled BOOLEAN`, `state TEXT` (§6), `interval_minutes
  INTEGER`, `last_checked TIMESTAMP`, `last_status TEXT` (host-free), `consecutive_failures INTEGER`,
  `first_failure_at TIMESTAMP` (the streak marker), `position INTEGER`, `spec_rev INTEGER`, `next_clock
  TIMESTAMP` (§34, clock pass), `created_at`, `updated_at`. Sealed body (AAD `ni_item:<id>`): the spec (§2).
- `ni_snapshots` - `item_id`, `slot`, `nonce`, `ciphertext` (AAD `ni_snapshot:<item_id>:<slot>`), `ok`,
  `created_at`, `PRIMARY KEY (item_id, slot)`. Sealed body: the bound payload (§4.3), the data and not the
  scene. The slot set is app-level and closed: `latest`, `last_good`, `preview`, `preview_data`, `history`
  (§11), `alert_state` (§12), `last_failure` (§23), `image` (§24), `outputs` (§34), `journal` (§28),
  `flow` (§29), `llm_state` (§13), `render` (§35). Reserved-id row families that are not items (ids
  start `__`): `__library__` (source pin, verified pack, outbox, §20) and `__mcp_servers__` (§22).
- `ni_revisions` - `item_id`, `rev`, sealed spec (AAD `ni_revision:<item_id>:<rev>`), `origin` (`user`,
  `agent`, `repair_l1`, `repair_l2`, `template`), `created_at`. The last 10 per item are kept.
- `ni_runs` - `item_id`, `ts`, `status`, `duration_ms`, `error` (host-free class only), `contract_ok`. The
  last 50 per item are kept. Plaintext telemetry, no content.
- `ni_findings` - the oversight plane's findings (§29, Oversight): code-authored host-free strings.

No foreign keys; `NIStore.delete` cascades in code.

## 2. Item spec

```json
{
  "version": 1,
  "title": "Spending this week",
  "goal": "<the user's words, verbatim, never rewritten>",
  "params": {"symbol": {"label": "Ticker symbol", "kind": "string", "value": "ACME"}},
  "source": { },
  "pipeline": [ ],
  "scene": { },
  "display": {"size": "small"},
  "contract": null,
  "repair_policy": {"l1": true, "l2_frontier": false},
  "model": null
}
```

- `params.*.kind` is `string | number | secret | clock`. A `secret` param's `value` is a SecretStore key
  name in the `ni:<item_id>:` namespace, never the secret. `{{param:NAME}}` placeholders may appear only
  where a field's schema says so.
- A `clock` param carries `{label, kind: "clock", format, offset_days}` instead of a value: the engine fills
  it from the current clock on every fetch. `format` is a strftime pattern over the closed characters
  `%A-Za-z%:- _,./` and the closed codes `%Y %m %d %H %M %S %y %j %u %w %-m %-d %B %b %A %a`;
  `offset_days` is a bounded int. Date-windowed Library sources use it so day 2 reads day 2's date.
- One `now` per build: the flow freezes `fetch_now` at the start of a build, rebuilds the fetch address from
  the sealed `_library_url_template` at that moment, and the verify reads the sealed spec at the same
  instant, so a build that crosses midnight stays consistent.
- `display.size` is `small | wide | large | tall`, the desktop grid cell a design takes (1x1, 2x1, 2x2, 1x2),
  derived from the sealed desktop span (`display_size_for_span`). The board is a fixed-rhythm grid (2
  columns on a phone, 3 from 780 px, 4 from 1040 px, 176 px rows) and sizes a form card from the span key of
  the CLIR it paints (`cardSpan`); `display.size` drives the card menu's Size entry and legacy scenes.
  Absent means small.
- `contract` is system-written at commissioning (§7); the agent may never set it.
- `model` optionally overrides the `ni` route for `model` sources. The sentinel `local/auto` (§37) resolves
  to the first local model on the ni, chat or agent routes each time the card runs.
- Interval floor: `interval_minutes >= 1`, clamped, never an error. `enabled=false` is the pause. Third-party
  hosts have a 15-minute floor (`ni_polite`); cards with an `llm` stage clamp to 5 minutes; rendered cards
  to 15.
- Optional keys: `history` (§11), `alerts` (§12), `brief` (§37), `compare` (inside `source`, §38).
- System-written, underscore-prefixed keys (validated by shape, stripped on export, refused in template
  packs): `_c2_ok`, `_present_ok`, `_born` (`flow | recipe | chat`), `_shell`, `_model_consent`, `_built_from`
  (§33), `_fit` (§34), `_l1_trial`, `_l1_last_attempt`, `_l2_proposal`, `_l2_last_attempt`,
  `_research_last_attempt` (§23.1), `_template` (§20), `_render_ok`, `_feed_candidates` (§35), `recipe`
  (§29), `query`, `interpretation` (§36).

## 3. Sources

The closed set (`_SOURCE_TYPES`; validators refuse anything else): `http_json`, `model`, `internal.schedule`,
`computed`, `http_page`, `internal.kb` (§15), `mcp_tool` (§22), `http_image` (§24), `internal.ni` (§25).

### `http_json`

```json
{"type": "http_json", "url": "https://api.example.com/quote?sym={{param:symbol}}",
 "headers": {"X-Api-Key": {"$secret": "ni:<item_id>:api_key"}}}
```

- Fetched through `ni_fetch` over `netguard` only: SSRF pin, redirect re-validation, byte cap, content-type
  check, and the politeness layer `ni_polite` (per-host budget, Retry-After pause, fetch dedupe, port policy
  80/443 or the one port a Library row declares, robots.txt for page reads and for any source that is not
  a Library row). A host over budget defers the card (`host_busy`), it is never a failure.
- `validate_public_url` runs on the param-substituted URL at every create, update and commission. The
  scheme and authority are literal: `{{param:...}}` may appear only in the path or query. Param values are
  percent-encoded at substitution (`quote(safe="")`).
- Header values are literal strings or `{"$secret": "ni:..."}` refs. `{{param:...}}` is forbidden in plain
  header values. Auth-shaped header names (`authorization`, `proxy-authorization`, `cookie`, any name
  containing `token`, `secret` or `key`) are refused unless the value is a `$secret` ref. A ref must start
  with `ni:` at validation and the loader enforces the full `ni:{item_id}:` scope.
- `$secret` values resolve at fetch time and are host-bound: they attach only when the request host equals
  the host recorded when the credential was entered (`secret_host_mismatch` is a permanent `broken`). They
  never ride plain http (`secret_requires_https`).
- Whenever a request carries any header, a typed key or the contact email, redirects are refused.
- `secret_query: {<name>: {"$secret": "ni:<id>:<name>"}}` (at most 2, https only) carries a key in the query;
  `contact_ua: true` adds the user's email to the honest User-Agent (`contact_missing` when unset).
  `ni.http_request_parts` is the one request builder for the engine and the flow's sample.
- `port` (optional, a Library row's declared port) widens the port policy to that one port.
- `format` (optional, default `json`): `json`, `csv`, `feed` (RSS and Atom), `xml`, `text`, `table` (§40).
  Non-JSON formats parse through `smartbrain_3000.formats` into the walker-shaped dict the pipeline reads:
  `csv` is `{"columns", "rows"}` (delimiter sniffed among `,;\t|`, header required, 500 rows, 4 KB cells);
  `feed` is `{"title", "items": [{title, link, summary, published, guid}]}` (DOCTYPE and ENTITY refused
  before parse); `xml` is `{root: {"@attr", "_text", child}}` (namespaces stripped, depth 12, 5000 nodes,
  DOCTYPE refused); `text` is `{"text"}` (200,000 characters). A parse failure is `NIError("fetch_failed",
  "not_<fmt>")`. A bare-list JSON response is wrapped as `{"items": [...]}` before the pipeline.
- Response caps: 8 MB, 8 s per read.

### `model`

`{"type": "model", "instruction": "..."}`. Runs through `gateway` on the `ni` route (or the item's `model`).
Output is plain text handed on as `{"text": ...}`. A cloud model needs the card's own consent
(`_model_consent`, §29). The instruction is user-visible spec content.

### `internal.schedule`

`{"type": "internal.schedule", "schedule_id": "<id>"}`. Reads the newest `schedule_runs` row (message only);
zero egress. Payload `{"message", "status", "ts"}`.

### `computed`

`{"type": "computed", "compute": "days_until", "date": "YYYY-MM-DD"}`. A pure clock read, zero egress. Payload
`{"days": int}` = target date minus today (UTC), negative for past dates. The "interpreted" chip stays off.

## 4. Pipeline

An ordered list of stages, each consuming and producing a JSON value. The implicit last stage is bind and
render-validate (§4.3).

### 4.1 `extract`

`{"op": "extract", "paths": {"price": "quote.latest", "rows": "items[0:5]"}}`. Path grammar, shared with scene
bindings (one implementation):

```
path     = segment ("." segment)*
segment  = key | key "[" index "]" | key "[" slice "]"
key      = [A-Za-z_][A-Za-z0-9_-]*
index    = "-"? digits
slice    = digits? ":" digits?
```

Quoted keys spell what the plain grammar cannot (`near_earth_objects["2026-09-27"][0].name`, `["bitcoin"].usd`):
`'["' qchars '"]'` with 1..200 characters, no `"`, no control characters. A quoted key opens a path or
follows a key or subscript directly, never a dot. Denied keys stay denied. A path that does not resolve is a
stage failure. The output is the object of named extracts; downstream stages and the scene see only those
names. `graph_extract` (§29, page cards) is a further first-stage op.

### 4.2 `transform`

`{"op": "transform", "apply": [{"fn": "round", "field": "price", "digits": 2}, ...]}`. A closed function set, all
pure; a type mismatch is a stage failure, never a coercion guess.

- Basics: `round(field, digits)`, `scale(field, factor)`, `offset(field, value)`, `rename(field, to)`,
  `pick(field, keys)`, `sort_by(field, key?, dir)`, `top_n(field, n<=50)`, `reverse(field)`.
- Aggregates over a list of objects: `sum`, `avg`, `min`, `max` `(field, key, as)` and `count(field, as)`.
  Each writes a new numeric output `as` (no collisions). An empty list gives count 0; the others fail with
  `empty_aggregate`.
- `delta_prev(field, series, as)`: the current numeric `field` minus the last point of history series
  `series` (§11), written to `as` as `{value, direction: up|down|flat}`. An empty series gives
  `{value: 0, direction: "flat"}`, never a failure.
- `where(field, key, op, value)`: filters a list in place. `op` is `lt|le|gt|ge|eq|ne|starts_with|contains|in`.
  Ordering ops need finite numbers on both sides; eq and ne compare scalars strictly (bool is not a number);
  `starts_with` and `contains` take a non-empty string (at most 200); `in` takes 1..12 scalars. A non-matching
  type, or an item missing `key`, is excluded, not a failure (selection, not validation). A non-list
  `field` is `transform_type`.
- Typed readers: `number(field, key?)` (a number sent as text), `time(field, key?, utc?, unless?, lenient?)`
  (an ISO, RFC 2822 or epoch timestamp to the user's local time; a zoneless ISO time is the source's local
  clock, or UTC with `utc: true`; `lenient` leaves a junk row without a time instead of failing), `date(field,
  key?)` (a `YYYY-MM-DD` date, never shifted by zone). `key` is dotted, `[n]` allowed, and converts that field
  of every row. The text keeps its instant (§34).
- `zip(field, with, as)`: parallel arrays of equal length become rows (at most 500; `with` 1..7 names;
  length mismatch `transform_type`, missing name `transform_miss`).
- `label(field, table, key?)`: an integer code to words from an app-shipped table (`wmo_weather`, the WMO
  interpretation codes). An unknown code is `transform_type`, never a guess.
- `window(field, key, window, zone, utc, unless, step, floor)`: cuts a time-indexed list to the asked stretch on
  every run (§32), keyed by an axis cell. `zone` is the source's zone, `utc` marks zoneless times as UTC,
  `unless` is a to-be-decided flag, `step` is `day|hour|period`, and `floor: false` keeps `step` while
  disabling the current-hour floor.

### 4.3 Bind and render-validate

Binding walks the scene, resolves every `{"$bind": path}` and `{{path}}` against the pipeline output,
enforces the §5 caps, and produces the bound payload: the scene with data inlined. That is what
`ni_snapshots` stores and what the client renders. An unresolved binding, a type mismatch or a cap violation
fails the run; `last_good` keeps rendering.

## 5. Scene grammar

A scene is a tree of nodes. The set is closed: an unknown `type` fails validation on the server and is refused
by the client.

Layout: `stack {dir: v|h, gap: sm|md, children}`, `grid {cols: 2..4, children}`, `divider`.

Content (each value-like prop is a JSON literal or `{"$bind": "<path>"}`):

- `text {value, role: title|label|value|caption, tone, size: sm|md|lg}`; `{{path}}` interpolation in the value.
- `number {value, format: plain|compact|percent|currency, unit, tone, size}`.
- `chip {value, kind}`, `bar {value, max, tone}`, `icon {name, tone}` (a lucide subset).
- `repeat {items: {"$bind": list}, max <= 50, template}`; inside the template, `item.` paths resolve against the
  current element.
- `spark {points: {"$bind": "history.<name>"}, kind: line|bars, tone}`: at most 500 numeric points or `{t, v}`
  points; inline SVG, no axes.
- `gauge {value, min, max, tone, label}`: `max > min`; clamping is presentation only.
- `image {alt <= 200}`: the pixel channel (§24). A spec carries only `alt`; the binder injects the server-owned
  same-origin `src`. Requires an `http_image` source.
- `form {...}`: a designed card (§34). Hand-authored `stack` scenes (chat, templates) keep this grammar.

Conditions (bind time; the client never sees them): any content node may carry `"when": [rule...]` (at most
5), each `{left, op, right, set}` with `left`/`right` a `$bind` or a scalar, `op` in `lt|le|gt|ge|eq|ne`
(ordering ops need numbers: mismatch is bind failure `when_type`), and `set` `{tone}` and/or `{hidden: true}`.
Rules run in order, the last tone wins, `hidden` removes the node. The bound payload carries only the result;
the client validator rejects `when` in a bound payload.

Rules: text is text (no markdown, HTML or links; a lying string can render but cannot become UI); tones map to
design tokens only; caps are 100 nodes after repeat expansion, depth 8, text 2000 characters. `on_tap` is
reserved and refused.

## 6. Lifecycle

States (`ni_items.state`): `draft`, `commissioning`, `live`, `degraded`, `failing`, `broken`. Pause is not a
state: `enabled=false` is the pause, and resume restores nothing else. (`paused` sits in `_STATES` reserved and
unused.)

Approval is consent: NI write tools are reviewed, egress, non-rememberable, so a create runs only after explicit
approval and lands in `commissioning`, except in `draft` when a secret param is empty or `draft: true` is
passed. An update whose source effectively changed returns to `commissioning`. Every `update_spec` strips
`_c2_ok` and `contract` and resets the failure streak.

- `draft`: spec and `preview` snapshot; never fetches. Left by `POST /api/ni/items/{id}/commission` (409
  unless draft, every secret filled and no param empty, and not a shell or a draft waiting for its sites).
- `commissioning`: C1 is one real run (failure stays here, error surfaced); C2 is the user's verdict on
  the real result (`POST /validate`, 409 unless commissioning); C3 is the automatic next-cadence run that must
  satisfy the contract captured at C1. If `_c2_ok` is set but no contract exists, the first clean run
  captures it and stays commissioning for one more clean run.
- `live`: runs at cadence, every run contract-checked.
- `degraded`: the latest run failed or violated the contract; `last_good` renders dimmed.
- `failing`: 3 or more consecutive failures; the effective interval doubles per failure beyond 3 (cap 24 h).
- `broken`: 8 consecutive failures over at least 7 days measured from `first_failure_at` (not `created_at`),
  or a permanent refusal. The engine stops scheduling it; only an edit and re-commission leaves it.

Every failure records an `ni_runs` row with a host-free class; `mark_checked` on every attempt is the backoff.

## 7. Data contract

Captured at commissioning (C1 result, confirmed by C2): `{"shape": {output: type}, "bounds": {output: {min, max}}}`.
`shape` fingerprints every output the scene binds. `bounds` are honoured if present, but nothing captures them
today (`capture_contract` writes `shape` only). The contract is checked after binding on every run; a violation
fails the run and degrades the card even when everything parsed. Repair targets contract satisfaction, not
mere parsing.

## 8. Engine pass

`ni.tick(app, pass_budget_seconds, breaker_open=None)` runs from `scheduler.tick` after the feeds pass. It
returns when locked, keeps its own cursor, and selects items that are `enabled`, not `draft`/`paused`/`broken`,
not held for the user's Keep (§33), and due (`last_checked` null or older than the effective interval, or
`next_clock <= now`), oldest first, at most 3 per pass, with a wall-clock budget, per-item try/except and a
host-free `last_status`. `model` sources need `gateway.local_available()` when the route is local and skip
while the scheduler breaker is open (they stay due). Rendered cards have their own window (§35).

Guards on every write: bound props are re-checked (`bind_type`); the bound payload is capped at 256 KiB
(`payload_too_large`); any exception on the finalize path becomes an `NIError`, so `mark_checked`, the run
row, the failure bump and the `latest` ok=false snapshot always fire. A due item that is not fetch-due takes
the clock pass (§34). Capacity kinds (`host_busy`, `render_busy`, `render_unavailable`) are never failures.

## 9. Tools and tiers

The model registry exposes four NI tools: `list_ni_items` and `read_ni_item` (OBSERVE; the read returns a
provenance line first, the journal, `state_explanation` and `user_next_action`) and `run_ni_item_now`
(REVIEWED, egress; clears `last_checked`, refuses draft, paused and broken). Creating, updating, deleting and
pausing are not model tools: the NI page owns every write. The handlers survive as `tools.INTERNAL_NI_TOOLS`
and `INTERNAL_NI_PREVALIDATE` for the routes and tests; a fabricated call to a retired name has no executor
and is answered inline with `cannot run tool '<name>'`.

`NI_WRITE_TOOLS = {run_ni_item_now}` joins `UNATTENDED_NEVER_AUTO`; none are rememberable. Create and update
carry a pure pre-park `prevalidate` that runs the same checks as the handler. Item ids are validated as
UUID-shaped before an approval card can park, and a miss returns the real card list. `create_ni_item` refuses
`http_json` sources: the flow is the only door for them (§26).

## 10. HTTP surface

All handlers answer 423 when locked. Desktop-local means the launcher token or a loopback browser session;
writes are audited.

Reads: `GET /api/ni/board` (every item: plaintext state and freshness, the decrypted bound payload from
`preview` for drafts else `latest` ok or `last_good`, `flow`, `actions`, `awaiting_yes`, `shell`, `born`,
`needs_credentials`, `needs_params`, `c2_ok`, `fit`, `interpretation`, `query.coverage`, `brief`, and for
page-browser cards `rendered`, `render`, `draft`, `preview`); `GET /api/ni/items/{id}` (spec with secrets as
names, health, runs); `GET /api/ni/items/{id}/flow` (the per-item poll: `{id, state, flow, awaiting_yes,
actions}`, strictly read-only); `GET /api/ni/items/{id}/image` (§24); `GET /api/ni/notices` (§17); `GET
/api/ni/findings`; `GET /api/ni/library`; `GET /api/ni/mcp-servers`; `GET /api/ni/items/{id}/export-template`.

Item writes: `POST /validate` (the C2 verdict; §33 for Keep and "try another source"), `POST /commission`,
`POST /activate` (§35), `POST /switch-feed` (§35), `POST /run` (409 for draft, broken and paused), `PATCH` (position, display, enabled, title, `interval_minutes`), `DELETE`, `PUT /credential` (`{name,
value, host}`, host-bound under `ni:<id>:<name>`, never through chat), `PUT /param`, `POST /refine`, `POST
/repair-policy`, `POST /brief-summary` (§37), `POST /l2-proposal/apply` and `/dismiss` (§23),
`POST /apply-template-update` (§20).

Creation flow: `POST /api/ni/intake {request, source_url?, allow_duplicate?}` (the composer; §29),
`POST /flow/retry`, `/flow/cancel`, `/flow/pick-source`, `/flow/access`, `/flow/fix`, `/flow/answer`.
Library and MCP management routes are under `/api/ni/library/*` and `/api/ni/mcp-servers`.

## 11. History

`{"history": {"track": {"priceLog": "price"}, "max_points": 100}}`.

- `track` maps a series name to a pipeline-output path. At most 4 series, numeric at run time (else
  `history_type`). A name must not collide with a pipeline output or be `item` or `history`.
- After every successful run (commissioning included) the engine appends `{"t": "<iso8601 UTC>", "v": number}`
  per series and trims to `max_points` (at most 500, default 100) in the sealed `history` slot. Failed runs
  append nothing.
- The binder exposes each series as `history.<name>` (a list of `{t, v}`) to `$bind`, `spark.points` and
  `delta_prev`. A series with no points yet is seeded as an empty list, so a chart over its own history can
  commission.
- Deleting the item deletes the slot. A source-change rewind keeps history. Renaming or removing a series
  drops the old one on the next append.

## 12. Alerts

`{"alerts": [{"name": "price-drop", "left": {"$bind": "delta.value"}, "op": "lt", "right": 0, "message":
"{{title}}: price fell to {{price}}", "cooldown_minutes": 60}]}`.

- At most 5 rules; `name` matches `[a-z0-9-]{1,40}` and is unique; `left`, `op`, `right` are exactly the §5
  conditions; `message` interpolates the run's outputs plus `{{title}}` (plain text, at most 500 characters);
  `cooldown_minutes` is clamped to at least 5 (default 60).
- Evaluated after every successful bind on live items. Edge-triggered: a rule fires on the false-to-true
  transition, never again until it has been false at least once, and never within its cooldown. Per-rule state
  lives in the sealed `alert_state` slot. Renaming a rule resets its state.
- A fired alert, and every `broken` transition, posts to the NI carrier row (a reserved schedule id,
  `neural-interface`), riding the existing badge, chat notice and /info archive. A rule whose binds cannot
  resolve fails the run (`alert_bind`): alerts are part of the contract surface.

## 13. `llm` pipeline stage

`{"op": "llm", "instruction": "...", "output": {"summary": "string", "count": "number"}}`. The one
deterministic-core exception.

- At most one per item, anywhere in `pipeline`. `instruction` is user-visible, at most 2000 characters, free of
  `{{param:}}`. `output` is a flat closed map of at most 6 fields, types `string | number | boolean`
  (strings at most 2000 characters; `max_chars` 1..2000 clips them; `optional: true` is for string outputs
  only).
- The current pipeline value is serialized (at most 8 KB, truncation marked), fenced and heading-forgery
  neutralized, and sent with the instruction to the `ni` route. Local models only: a non-local model fails the
  run with `llm_requires_local`, never a cloud fallback. The call carries no tools. The reply must be a single
  JSON object matching `output` exactly (strict parse, exact keys, finite numbers). One retry appends a fixed
  generic sentence, never the raw parse error. A second failure is `llm_output` and `last_good` keeps
  rendering.
- Optional stage: any soft failure (no local model, a busy slot, an error, a bad reply) yields empty outputs and
  the run continues. It waits at most 1 s for the local-model slot (`LocalBusy` becomes a soft `llm_error`), and
  `run_item` tolerates the `llm_requires_local` or `llm_unrouted` refusal when every llm stage is optional.
- Engine: items with an llm stage clamp `interval_minutes` to at least 5, at most one such item runs per pass,
  and they require `gateway.local_available()` and a closed breaker.
- Monitor hash: on a live llm-stage card the engine hashes the stage's source; unchanged bytes skip the model
  (`llm_state`). The hash records the `spec_rev`, so turning a summary on again runs the model.
- Quote grounding (`ni.ground_llm_outputs`): every number an llm string output states must appear literally in
  the fetched page text (comma and space insensitive); a value with an ungrounded number is replaced with "".
  Word paraphrases are not policed. Previous values are kept in `llm_state.values` for run continuity.
- Honesty: the board row carries `interpreted: true` and the card shows an "Interpreted" chip. A flow shell does
  not wear the chip.
- Contract: llm outputs are contract-checked for shape and type; their values are non-deterministic.

## 14. L1 self-repair

Trigger: an item reaches `failing` with a spec-shape failure class (`extract_miss`, `transform_type`,
`contract_violation`, `bind_type`, `when_type`, `history_type`, `alert_bind`, `llm_output`) and
`repair_policy.l1` is true. Never for transport classes (`fetch_failed`, `redirect refused`, `secret_*`,
`llm_requires_local`) or `param_empty`.

- One attempt per failure streak: the sealed `_l1_last_attempt` must predate the current `first_failure_at`.
  It survives reverts and trial clears; the streak marker resetting on success, update or commission is what
  grants the next streak a fresh attempt. Requires a local `ni` route, `local_available()` and a captured
  `contract` (otherwise the streak continues toward `broken`).
- `_l1_trial` must be `{"rev_before": int >= 1}` and `_l1_last_attempt` an ISO-8601 string. Any user or agent
  update strips `_l1_trial` (a user edit voids the trial, so a later revert can never restore a pre-update
  revision or its consent).
- Model input (fenced, neutralized, no tools): `goal`, the current extract and transform stages, the failure
  class and detail, the captured contract, and at most 4 KB of the failing run's raw payload.
- Model output, the entire repair surface: a JSON object with optional `extract` (full replacement paths map)
  and/or `transform` (full replacement apply list, closed ops). Never `source`, `url`, `headers`, `schedule`,
  `scene`, `llm.instruction`, `alerts` or `params`. Anything else, an invalid spec or non-JSON means the
  attempt failed with no change.
- `apply_repair(item_id, candidate, expected_rev=...)` rechecks `spec_rev` under `_SPEC_LOCK`; a concurrent user
  update aborts it with `spec_changed`.
- Under the tick, a repair needs a closed breaker, the pass's shared llm slot, and at most one repair per tick.
  A manual run bypasses this (the click is the consent).
- Trial: the repaired stages apply as a revision with origin `repair_l1`, keeping `contract`, `_c2_ok`, state
  and counters (a repair never triggers re-consent). The next run is the trial: success with the contract
  satisfied sticks, clears counters and posts a carrier notice ("<title> repaired itself - data mapping
  updated."); failure reverts to the pre-repair revision. The revert holds `_SPEC_LOCK`, strips `_l1_trial`
  inline, and records `repair_reverted` with `bad_rev_before`, `missing_prior` or `revert_unavailable` rather
  than wedging.
- Attempts write `ni_runs` rows (`repair_applied`, `repair_reverted`, `repair_failed`).

## 15. `http_page` and `internal.kb`

`http_page`: `{"type": "http_page", "url": "https://example.com/status", "headers": {...}}`, with the URL,
param, header, credential and redirect rules of `http_json`. Text is fetched through netguard (8 MB) and
extracted in the subprocess jail (§16). Payload `{"text", "title"}` (text at most 200 KB). A page source may
also carry `render: "browser"` and `sites` (§35). Compiled page cards add the `graph_extract` first-stage op:
a page is read into a deterministic PageGraph (`pagegraph.py`: JSON-LD, OpenGraph, feed autodiscovery, h1-h3
outline, bounded typed tables, data-bearing JSON blocks), and the card's values are closed selectors
(`title`, `meta{key}`, `entity{etype, field}`, `outline{index}`, `table_cell`, `table_lookup`, `data_leaf`) a
model picked from a code-enumerated menu. A selector that no longer resolves is graph drift and Fix recompiles.

`internal.kb`: `{"type": "internal.kb", "query": "...", "limit": 5}`. Zero egress; runs the user's knowledge-base
hybrid search. `limit` is clamped 1..10. Payload `{"results": [{"title", "snippet", "doc_id"}]}` (snippets at most
500 characters). Imported-vault content keeps its third-party status; the pipeline treats the bytes as
untrusted.

## 16. Subprocess jail

For parse steps over hostile input (`http_page` extraction, `jail_extract`). The child is `sys.executable -c`
importing only the extractor, with a stripped environment (no `SMARTBRAIN_*`, no `ANTHROPIC_*`, minimal PATH),
a private cwd and `start_new_session=True`. Input goes over stdin; output is one size-capped JSON object on
stdout; stderr goes to devnull. Resource limits come from `resource.setrlimit` (CPU, address space, no cores;
POSIX only). A watchdog kills the process group at the deadline. Timeout, crash or malformed output is the
run-failure class `extract_jail`. The parent validates the payload as a closed set, so a compromised child
cannot widen it. A busy extractor slot is a capacity outcome (`jail_busy`).

## 17. Notices endpoint and tray notifications

`GET /api/ni/notices?limit=N` is desktop-local and unlocked-only. It returns the newest carrier-row entries
`[{id, kind: alert|broken|repaired|proposal, body, ts}]`, newest first, limit at most 20. Kind derives from the
carrier run status (`complete` is `alert`; unknown statuses read as `alert`). Bodies are sanitized at write
time (alert messages by the interpolation guard; titles by newline collapse and a leading `#` quote). The
launcher polls every 60 s, treats 423, connection failure or non-200 as skip, de-duplicates by highest-seen
id, posts at most 3 notifications per poll ("...and N more on your Neural Interface."), and a locked vault
produces none.

## 18. Source catalog: the SmartBrain Library

There is no built-in catalog. Sources come from the SmartBrain Library (§19, §20, §30 to §32), a signed
collection of source records with declared answers. Search results and pasted links are offered as links or
read through the open paths (§33).

## 19. Template pack format

A template is an item spec with its slots empty; a pack is a signed, versioned collection. Trust machinery is
the vault subscription model: Ed25519 over canonical JSON (sorted keys, no floats, duplicate keys rejected),
fingerprint display law, TOFU pin, monotonic seq, rollback refusal, KeyChanged blocking.

```json
{"sb_ni_pack": {"version": 1, "pack_id": "<uuid>", "seq": 3, "published_at": "2026-09-09",
  "publisher": {"label": "...", "pubkey": "<b64 Ed25519>"},
  "templates": [{"id": "<slug>", "title": "...", "goal": "...", "category": "...", "tags": ["..."],
    "spec_template": {"...": "§2 spec: params with kind and label but empty values; secret params carry the 'ni:self:<name>' placeholder; contract null; no repair fields, no repair_policy, no credentials or personal data"},
    "preview_payload": {"...": "bound scene, dummy data"}, "notes": "one honest sentence"}]},
 "sig": {"alg": "ed25519", "value": "<b64>"}}
```

Signed over `b"sb-ni-pack-sig:v1\n" + canonical(payload)`. Verification order on every fetch: shape and bounds,
`pack_id` matches the pin, signature against the pinned key over the served bytes, then `seq >` pinned is an
update, `==` up to date, `<` a rollback refusal. A different key is KeyChanged: reported, never applied, the
source blocked until the user re-trusts the exact offered key. Bounds: at most 200 templates, 2 MB, every
`spec_template` passing the full §2 validator (empty param values allowed) and every `preview_payload` the §5
bound-scene validator at install and at pack load. The publisher label is decoration; the fingerprint
(`SB-XXXX-...`) is the identity. Packs refuse the system-written keys of §2.

## 20. Library source, install and fleet healing

- One Library source (the official), stored like a vault subscription: sealed record `{url, publisher_pubkey,
  pack_id, seq, added_at, last_checked, blocked}`, TOFU-pinned on connect. Connect and disconnect are
  desktop-local explicit acts. The official URL is prefilled, never trusted without the pin.
- Install (a UI act): the sheet shows title, goal, every source host and path, and required params; the user
  fills params (secrets through the credential path); the item is created in `draft` with sealed provenance
  `_template = {pack_id, template_id, seq, spec_hash}`; the user's Activate is the consent. Installing never
  runs anything. `repair_policy` is forced to `{l1: true, l2_frontier: false}`.
- Fleet healing: when an update changes a template (`spec_hash` differs), every item carrying that `_template`
  shows "Template update available". Applying is per item and user-driven: the diff is shown, acceptance
  re-enters `draft` then `commissioning` (never silent, never inheriting consent). Param values and credentials
  carry over; `_c2_ok`, contract and trial markers reset.
- Update checks piggyback the tick (24 h default, 1 h floor; manual "Check now"). A validly signed older pack
  is not unreachable: `last_checked` advances, the status reads "host is serving an older pack", and the
  unreachable counter is untouched. The first transition to blocked or rollback posts one carrier notice.

## 21. Export as template and submission

`GET /api/ni/items/{id}/export-template` (desktop-local) returns the §19 template for the item. The sanitizer
strips secret values (secret params reset to `ni:self:`), string and number param values (labels kept),
`contract`, `_c2_ok`, `_l1_*`, `_l2_*`, `_research_*`, `repair_policy`, `_template`, `_built_from`, `recipe`,
`brief`, and rewrites this item's `ni:<item_id>:<name>` refs to `ni:self:<name>`. It refuses `internal.schedule`
and `internal.kb` sources, compare cards (§38), and any spec whose substituted URL (also percent-decoded) or
any `llm` instruction contains an entered credential value (a belt-and-braces guard; the primary rule is
`$secret` discipline). A card with the summary on first drops the summary. Items created before the
`preview_data` slot existed refuse export with a message naming the cause. Submission is a GitHub PR to the
registry repo; only the publisher-key holder signs and publishes.

## 22. `mcp_tool` source

SmartBrain's MCP is inbound by design; this is the one approved outbound path: servers the user explicitly
configures, never ambient config, never discovery, never tool listings fed to models. Credentials stay in the
user's own server process.

Registry: a sealed store of `{id, label, transport: stdio|http, command?, args?, url?, enabled}`. CRUD is
desktop-local UI only; no agent tool edits it. `stdio` launches the user's command per fetch with a stripped
`SMARTBRAIN_*`/`ANTHROPIC_*` environment, a fresh process group and a watchdog. `http` connects to the typed
URL and deliberately does not ride netguard (the point is the user's loopback or LAN server); the address was
typed in a desktop-local act and is frozen, and redirects are refused by constructing our own
`httpx.AsyncClient(follow_redirects=False)`.

Source: `{"type": "mcp_tool", "server_id": "<id>", "tool": "query", "arguments": {...}}`. `tool` and
`arguments` are frozen literal JSON; any change is a source change and re-consent, and the consent surface
shows `MCP: <server label> -> <tool>` with the full arguments. One `tools/call` per run (20 s deadline). Text
content items are joined (each truncated at 200 KB, the join stops at 200 KB); JSON-parsable text gives
`{"data": ..., "text": ...}`, else `{"text": ...}`. Errors are host-free: `mcp_unavailable`, `mcp_tool_error`,
`mcp_timeout`. Tool descriptions are never fetched into a prompt or spec.

## 23. L2 frontier repair

When L1 has exhausted its attempt in a streak and the item still fails, a frontier model may propose a fix
under the strictest consent in the system.

- Gates (all required): `repair_policy.l2_frontier` true (per-item opt-in, default false, set only by an approval
  card or the desktop-local `POST /repair-policy`; packs cannot ship it); the Claude Code provider connected;
  L1 exhausted this streak (`repair_policy.l1` false, or `_l1_last_attempt` predating `first_failure_at`);
  one L2 attempt per streak (`_l2_last_attempt`).
- Sent: the L1 envelope (goal, stages, failure class and detail, contract, at most 4 KB fenced raw excerpt) read
  from the sealed `last_failure` slot. Never vault or KB content, secrets, or headers. Items whose source is
  `internal.*` seal an empty excerpt.
- Returned: the L1 closed schema, fully revalidated.
- Park-only: a valid proposal is never applied by code. It is sealed as `_l2_proposal = {stages, created_at,
  model}`, shown as "Fix proposed - review" with a stage diff and Apply or Dismiss, and posts a carrier
  notice. Apply uses the L1 trial discipline (origin `repair_l2`) and refuses 409 when the item is `broken`.
  Dismiss clears it. A user spec edit or a clean run voids a pending proposal.
- The call takes minutes, so it never runs inside the tick: the tick marks eligibility and one bounded daemon
  worker (one in flight process-wide) writes the proposal. Attempts are `ni_runs` rows (`repair_l2_proposed`,
  `repair_l2_failed`); Apply and Dismiss are audited.

### 23.1 Constraint drift and re-research

A card with a sealed `recipe` (the researcher's, §29) adds two things.

- `constraint_drift`: after a successful fetch and pipeline, `_finalize_run` re-runs on the fresh outputs the
  verifier checks that passed at Keep (`ni_refresh.drift_reasons`: place, window, threshold, compare, empty).
  A failing one fails the run with status `constraint_drift` (the normal streak, backoff and `last_good`;
  no L1 and no stage-patch L2). A spec with no `recipe` skips the check.
- Parked re-research: a failing card may ask for a new source when `ni_refresh` says so and the research plan
  is `cloud`. The L2 worker runs `ni_research_flow.reresearch` (the researcher on `recipe.question`, Library
  only, the current source withheld) and parks `_l2_proposal = {spec, origin: "research", reason:
  constraint_drift|source_dead, created_at, model}`, a whole replacement card. Apply swaps the card through
  `apply_repair(replace_spec=True)`; the old `contract` and `_c2_ok` are not carried; the L1 trial and
  auto-revert apply.
- Triggers (`ni_refresh.TRIGGER`, by `recipe.tier`): `curated` asks once the card is `failing`;
  `verified_endpoint` and `page_reading` ask on the first `constraint_drift` and, for a dead source, once
  `failing`.
- Bounds: the sealed marker `_research_last_attempt = {at, streak, outcome: started|proposed|declined,
  declines}`. No second attempt within 24 h of a start; none in a streak that already parked a proposal; after
  each decline the wait is 1, 2, 4 days, capped at 7. Process-wide, `take_budget` spends one of
  `SMARTBRAIN_NI_RERESEARCH_DAILY` / meta `ni_reresearch_daily` (default 10, 0 is off) per UTC day. Research
  off, ask or local never sends anything.

## 24. `http_image` source and the `image` node

Cards can watch an image the user consented to. Pixels are fetched, sealed and re-served same-origin, the only
CSP-legal path (`img-src 'self' data:`).

`{"type": "http_image", "url": "...", "headers": {...}}`, with the `http_json` rules, netguard, image content
types, a 4 MB cap and a wall-clock deadline. The body must sniff as PNG, JPEG, GIF or WebP by magic bytes (the
served Content-Type is ignored; no SVG; no decoding or re-encoding); anything else is `image_type`. Bytes are
sealed in the `image` slot; the pipeline sees metadata only: `{"image": {"bytes_len", "format"}}`.

The bound `image` node is written by the server only: `{"type": "image", "src":
"/api/ni/items/<id>/image?v=<created_at>", "alt"}`. The client refuses any other src. `GET
/api/ni/items/{id}/image` serves the sealed bytes with the sniffed type and `Cache-Control: no-store`.

## 25. `internal.ni` composite source

`{"type": "internal.ni", "items": {"stock": "<item-id>", "spend": "<item-id>"}}`. At most 5 items; zero egress.
Depth 1 by construction: a referenced item whose own source is `internal.ni` is refused at create, update and
run (`composite_depth`). Per alias the payload is `{"title", "state", "payload_at", "history": {name: [{t,
v}]}}`: history series plus health metadata, never another card's scene. A missing item yields empty history,
never a failure. The consent surface lists every referenced item by title.

## 26. Creation doors

The flow (§29) is the only door for `http_json` cards. `create_ni_item` refuses `http_json` sources at
prevalidate and remains the door only for the source types the flow does not build (`model`, `internal.*`,
`mcp_tool`, `http_page`, `http_image`, `computed`). A refusal of a source carrying `render` or `sites`
(`_refuse_render_seal`) keeps a model from writing the page-browser seal. Cards built from the Library or
from research install through the flow's handoff: `ni:self:<name>` refs rewrite to `ni:<item_id>:<name>`, and
anything code cannot derive stays empty on purpose, so the landing rule forces `draft` and the card asks the
user (never a guessed value). A keyed card whose secret is absent may reuse another card's same-host,
same-name credential through the desktop secrets provider, copy-based under the new item's own key and
journaled; different hosts never reuse.

## 27. Sample-grounded authoring

When no Library source fits, drafting is grounded in a real sample: the internal tool `derive_ni_paths`
(`{sample <= 32 KB, want?}`) walks the sample in code and returns candidate leaf paths with types and example
values (the §4.1 grammar), with no model in the loop. A fix conversation reads `read_ni_item`, whose
`last_failure` excerpt (http sources only, §23 rules) lets paths be corrected against the real payload.

## 28. Item journal and session continuity

Per-item memory for any later model: the sealed `journal` slot, an append-only list pruned to the newest 20 of
deterministically composed entries `{ts, kind: created|updated|param_changed|commissioned|c2_wrong|repaired|
reverted|source_changed|recipe, summary}`. Summaries are built from structured facts by code (plus the user's
own C2 notes verbatim); models never write them. It cascades on delete and is excluded from export.
`read_ni_item` returns the journal plus deterministic `state_explanation` and `user_next_action` strings per
state and condition, so a model reports state truth ("waiting for the user to add the key on the card; do not
describe this card as live").

Cloud sessions (the Claude Code provider): a turn opens one session, later steps resume it with only the
delta, and the turn's end or watchdog deletes the artifacts. Containment is unchanged (empty toolset,
`--setting-sources ""`, strict MCP, stripped environment, private 0700 directory). Stateless per call is the
fallback.

## Status truth

The card must never lie about its state.

- Board rows carry `needs_credentials` (secret params with no stored key) and `needs_params` (`[{name, label}]`,
  unfilled non-secret slots), and render `last_status` and `consecutive_failures` in words ("First run failed -
  couldn't find the expected data, 4 attempts; last tried 2m ago"), not "Waiting for the first run...".
- A `{{param:X}}` that resolves empty raises `param_empty` at substitution, so the engine never fetches
  `?symbol=`. The landing rules force `draft` while any referenced non-secret param is empty; the commission
  route 409s naming it; the card offers Fill (`PUT /param`, desktop-local, secrets refused). `param_empty` is
  not L1-repairable.
- Cards with `needs_credentials` offer Add key (the host-bound credential modal). Any item declaring a
  secret-kind param lands in `draft`.
- `create_ni_item` refuses a title that case-insensitively matches an existing item unless
  `allow_duplicate: true`, and names the existing card.
- The C2 verdict `ok=true` kicks the C3 proof run synchronously (the same body as `/run`, with its double-run
  guard) and reports the post-run `state` and `run` outcome; a kick failure never masks the verdict. Board rows
  carry `c2_ok`, so the card shows "Confirmed - verifying..." after the tap.
- A flow shell (`spec._shell`) is a not-yet-finalized placeholder: commission refuses it and the board hides
  Activate. Shell titles are bound to one readable line.

## 29. The creation flow

Code owns the sequence; models fill narrow, validated blanks. There is one flow. A card is made by
`POST /api/ni/intake {request, source_url?}`: a shell card appears at once (before any model call) and a
single-flight background worker walks the stages. The tick never runs flows. Every later step is a card
affordance.

### States

A flow record rides the sealed `flow` slot. `FLOW_STATES`: `intent`, `source`, `awaiting_access`, `sampling`,
`mapping`, `assembling`, `awaiting_credential`, `awaiting_params`, `ready`, and the terminal `unsupported`,
`failed`, `declined` (`ni_master.TERMINAL_STATES`). `source` with the `awaiting_source_pick` marker is the pause
that offers links and the paste-a-link field (§33); a bare `source` is the locating window, work in progress.
A record left at the retired `confirm_source` state by an older build re-lands the `source` pause when the
board reads it, or when the sweep runs. The board shows `progress {step, of, label, gate?, try?}` for a
non-terminal flow: four steps (Understanding your ask, Finding a source, Reading a sample, Checking it answers
your ask / Building the card), computed from the state alone, with `try {n, of, host}` while the auto-pick
works through Library sources.

`sweep_stranded_flows` (from the tick, budgeted) fails any non-terminal record older than one hour to
`failed(stale)`, except user-gated pauses (`awaiting_access`, `awaiting_credential`, `awaiting_params`,
`source` with the pick marker), and skips live in-flight workers. A cancelled flow is cooperative: the worker
finishes its current fetch and seals nothing.

### Stages

1. **Intent** (one model call, closed schema, one retry): `{kind: external_data|computed_only, subject,
   cadence_minutes (default 15), wants[], place, threshold|null, display_hint}`. The frame is code's parse:
   `frame_kind` over `library_index.FRAME_KINDS` (`current_value | next_event | schedule | forecast |
   result | trend | ranking | latest_items | alerts | status | count`), left null when the words state none;
   the form engine's question kinds (`ni_forms.types.QUESTION_KINDS`) add `lookup`, `map`, `image`,
   `text_brief` and `compare`. `computed_only` builds the countdown (§3) or asks "when is it?" (an answerable `unsupported`).
2. **Source** (code). A user-named `known_url` (the composer's field, a pasted link, a Fix) is the consent and
   is sampled directly. Otherwise the **auto-pick** runs (below). There is no pick pause for Library rows.
3. **Sampling** (code): one consented fetch through `ni_fetch`; lists are deterministically downsampled to 2
   exemplars (1 if still over 30 KB) before any model sees them. An address with no sealed format (a pasted link,
   a research fetch) is read as JSON first, then once more as `netguard`'s `sniff` read, which admits any
   `application/*` or `text/*` label (case-insensitive) and is parsed by what the first bytes say. Every textual
   read refuses a binary body by its bytes (`looks_binary`: a pdf/zip/png/gif/jpeg/gzip/xls/rar/7z/bz2 magic, or
   control bytes making up 10% or more of the first KB, or high bytes making up 30% or more that are not UTF-8
   when no legacy charset is declared; UTF-16 is text, and a stray NUL in a text export is still text), after one
   chunk. HTML goes to the page door. A fault on
   the second read (`host_busy`, a dropped connection, an oversize body) is reported as that fault, not as
   `not_json`.
4. **Mapping** (one model call): only for a pasted JSON link, a Fix, or a remap. The menu is pre-filtered by
   code to the field's expected type; the model selects paths only; code verifies membership and type, retries
   once, else `failed(mapping)`. A tapped or auto-picked Library row is never model-mapped: it builds from its
   declared answers (§32).
5. **Assembly** (code): pipeline and a `form` scene (§34) from the answers, with typed verification against the
   sample. An unsupported `display_hint` degrades honestly.
6. **Verify** (code, `_verify_frame`, before every declared-answers handoff): the build is checked against the
   ask's frame. It refuses a source whose categories share no top level with the ask, one about a different
   named subject, one that cannot take the named place, answers that cannot be this kind of question (a next
   event needs a time still to come; a result needs a score), a label for another day than asked, and wants
   nothing chosen speaks to. A refusal drops that source ("<provider> doesn't answer this (<reason>)") and the
   flow moves on. Computed notes name what the source does not report. A text-product want (the forecast
   discussion) is answered only by a genuine text cell.
7. **Fit verdict** (advisory, build time, one closed local call): `ni_forms.verdict.fit_verdict` asks whether
   the data answers the ask; the verdict is sealed as `_fit` and only ever shown as a marker (`visible_fit`:
   `no` or `partly` with at most 3 missing labels, never `yes`, suppressed on a designed empty state). It never
   blocks or changes a card.
8. **Handoff**: `_finalize` seals the spec: `_born`, `_built_from` (§33), the `recipe` for researched cards,
   `query` and `interpretation` (§36), history tracking (§34). Landing rules: secret params make `draft` with
   `awaiting_credential`; empty params make `draft` with `awaiting_params`; a held card (§33) lands
   `commissioning`; otherwise `commissioning`.

### The auto-pick

`_auto_pick` runs for an ask with no named source. In order:

1. An ask that compares two to four named things goes to `ni_compare_flow.try_compare` first (§38).
2. A brief-shaped ask builds a headline card when the sample holds a list of titled, linked or dated items (§37).
3. The Library is asked for up to 5 rows (`_library_candidates`, with the frame as a hint; a row whose lookup
   leaves its host is dropped). Rows whose source declares its answers are buildable; others ride along as
   link rows. Keyless rows come first, then rows that need the user's key or contact email.
4. `_auto_continue` tries the buildable rows in order. Each try seals the pick, fetches the one address through
   the same netguard path as a tap (audited as `ni_auto_pick`, with the host), and builds from the declared
   answers; every post-fetch check is the gate, and a row that does not fit is dropped with its reason. A row
   that needs a key or email pauses at `awaiting_access` and sends nothing until the user types it
   (`POST /flow/access`).
5. No buildable row, or every row failed: the researcher runs (below), else the declined card (§39).

Consent posture: the only fetch before Keep is of an installed Library row that declares its answers. Nothing
recurs until Keep: the card is held (§33).

### Research

`ni_research_flow.fallback` runs before the honest stop. The tier is chosen by `ni_research.policy`: the setting
(`SMARTBRAIN_NI_RESEARCH` or meta `ni_research`, `auto | off | local | cloud`, default `auto`), the routes, and
this card's consent.

- `cloud`: an `anthropic/*` route and the card's `_model_consent` (and, for the open web, consent given under
  the web wording). A bounded tool loop in which the model only chooses and code runs every tool
  (`library_search`, `web_search`, `fetch_sample`, `try_recipe`, `propose_card`, `propose_compare`). The open web
  (search and public pages) is on unless the kill switch (`SMARTBRAIN_NI_RESEARCH_WEB` or meta
  `ni_research_web` set to `off`) limits it to the Library. Fetches carry no headers, no POST, no redirects,
  and every address must have been handed over by a search or the Library, never composed.
- `local`: the closed-world mode. Code ranks Library rows and the local model only picks one and fills the
  recipe. Never the web.
- `ask`: the card shows the consent question (`model_consent`, §29 Consent) and nothing is sent.
- `off`: no researcher.

A proposal is checked by code (`ni_research.tools._checks`: place, window, threshold, compare, empty,
`subject` for page readings), may be refused twice, and lands as a held card with `_built_from.path:
research` and a sealed `recipe = {question, sources, constraints, tier, verification?, provenance?, research?}`
(`ni_recipe`). `tier` is `curated`, `verified_endpoint` or `page_reading`. Researcher drafts may carry no `llm`
stage, `$secret`, model or MCP source, or model-chosen interval. The interval is stamped by code from
`recipe.question.cadence`. A run that could not finish ends `fault` (a failed, retryable card), never a
decline.

### Consent

- A card whose resolved model is not local and differs from the card's `_model_consent` stops before any model
  call with an answerable `unsupported` whose question is `model_consent {model, local, prompt, web}`. The
  wording names what leaves the device (the request and samples from the source), and with the open web on,
  the search and page items. `POST /flow/answer {kind: model_consent, value: allow|local}` stamps the consent
  (sealed into the spec so Fix does not re-ask) or builds with the local model. The engine's `llm` stage stays
  local-only.
- `run_flow` resolves its model live from the `ni` route with `chat` and `agent` fallbacks and prefers a local
  one when the ni-resolved model is cloud and a local one exists (an explicit `ni` route wins).
- A named `source_url` is shape-validated at intake and netguard-fetched at sampling.

### Card actions

The backend declares the buttons: board rows and flow fields carry `actions`, a list from `ni_master.ACTION_KINDS`
(`keep, dismiss, cancel, retry, fix, refine, run, pause, resume, edit, delete, activate, activate_sites,
give_access, answer_date, allow_model, use_local, fill_param, add_credential, review_fix, pick_source`).
`ni_master.actions_for` is the one table; the client renders the declared list and ignores unknown kinds. Every
reopen kind and question a terminal card names maps to its action (`_REOPEN_ACTIONS`, `_QUESTION_ACTIONS`), so a way
out the card offers is always a button on its row.

- **Retry** (`POST /flow/retry`) re-runs a terminal shell's sealed request from a fresh record
  (`ni_flow._fresh_record`): the card's own answers (`_declined`, `_model_consent`, `_model_consent_web`,
  `_use_local`) and the address the user named themselves (`_pasted`, written only at intake, by a paste or by
  an approved chat call) ride along; nothing a previous build sealed does, so the engine's last address is never
  read as the user's. 409 while a worker still runs, for a finished card, and for a non-terminal flow. No and
  Paste a link start from the same fresh record.
- **Cancel** (`POST /flow/cancel`) stops a running flow; 409 when nothing is building.
- **Paste a link** (`pick_source`, `POST /flow/reopen`): a declined or fetch-failed shell re-enters the source
  pause from a fresh record (no Library seal of the stopped build rides into the paste; the decline's links are
  shown as pages), where the user pastes a link to the data (`/flow/pick-source` below). Nothing is looked up or
  fetched by the reopen. Shells only: a finished card
  reopens via Fix, and its terminal surface never names `pick_source`. 409 when the row does not offer it.
- **Fix** (`POST /flow/fix`, `begin_remap`) re-enters sampling on the item's own frozen source (http_json and
  page cards; unfilled params refuse). **Refine** (`POST /refine`) routes a note by code: a cadence phrase sets
  the interval; a source-change phrase relocates exactly as **Try another source** does (`decline_reading`: the
  card's source joins `_declined`, the flow re-runs from the words, the new card is held for Keep); anything else
  re-samples the frozen source with the note sealed on the record for the rebuild and the verify.
- **Answer** (`POST /flow/answer`): `supply_date` (YYYY-MM-DD, sealed as `_supplied`, the user's date being the
  truth) and `model_consent`.
- **Pick source** (`POST /flow/pick-source {url, pasted}`): the paste-a-link field of the stop card. A link row
  the card offered is refused (409) unless `pasted: true`; a pasted link builds through the mapping path and is
  held for Keep.
- Duplicate titles 409 naming the existing card.

### The Master and the ledger

`ni_master.py` is the execution-plane contract: the closed move set (`advance retry reroute ask_user research
degrade finish park`), the append-only `_ledger` on every flow record (one `_transition` hook covers every stage
entry and terminal), and the single-writer law: terminal card copy (a `reason` sentence, a closed-kind `question`
from `QUESTION_KINDS`, `reopen` affordances from `REOPEN_KINDS`) is derived in one place,
`terminal_surface`, and rendered verbatim. The law guarantees a non-empty reason and a question or a reopen on
every terminal state; shells are exempt from terminal-slot hiding (a shell's only payload is its sample, so
hiding the record would erase the truth), finalized cards keep it (a failed remap must not mask a working card;
the board hides a terminal record only when the card renders a payload and holds no question).

### Oversight

`ni_watch.py` (W-CREATE) rides the NI tick. It sweeps stranded flows and audits **the cards the board actually
serves**: each item is read the way `GET /api/ni/board` builds its row (`ni_routes._board_row`), never from the
internal flow slot, and checked against the table `ROW_CHECKS` (a code, a severity, a host-free title and a
predicate over a served row):

- `dead_end` (high): a terminal flow served with no reason (`reason` or `decline.line`), or with no question,
  reopen or way-on action; or a terminal record the board serves with no flow and nothing to render (a shell,
  or a card with no payload).
- `no_actions` (high): a served card whose `actions` is empty (`delete` is on every list).

A new audit is a new row of the table. It also clusters the same failure class across cards (3 or more in 24 h
is one warn finding; a decline is a verdict, not a class). Findings are code-authored host-free strings in
`ni_findings`, edge-triggered by the open-finding dedupe; high findings ride the carrier row; the Status
surface lists them with a Dismiss (`GET /api/ni/findings`, `POST .../resolve`).

## 30. The Library and the user's key

The SmartBrain Library is the flow's source step: a signed record collection with `answers` (§32), an index
(`library_index`), a resolver that fills parameters from the user's words (`library_resolve`, including geo
and named-place resolution with closed data) and a client (`library_client`). A tap or an auto-pick is recorded
as a vote (`{source_id, "yes", app_version}`) queued in a sealed outbox (reserved row, slot `outbox`, at most
200) and sent by the scheduler (at most 10 per tick, backoff 15 minutes doubling to 24 h, dropped on
400/413/415/422 or after 8 tries; `SMARTBRAIN_LIBRARY_API=""` switches sending off). The add form's `suggest`
queues a record with parameter values, headers, tier, origin and votes removed.

## 31. The user's own key and contact email

A Library source whose parameter fills `from: vault_key` yields its address without the key plus `needs_key
{in: query|header, name, prefix, docs_url}` (a key in the path is refused); `contact_ua` sources carry
`needs_contact`. Keyless rows are offered first. A row that needs access seals `_access` (bound to that exact
URL) and the flow pauses at `awaiting_access` with the question `give_access`; nothing is fetched. `POST
/flow/access {key?, email?}` (desktop-local, audited without values) stores the key as `ni:<id>:api_key` bound
to the host (with the header prefix) and the email as SecretStore `contact:email`, then starts the worker. A
same-host `api_key` another card holds is reused. Sampling and assembly attach the key only when the URL equals
the sealed `_access.url`. The built spec carries refs and an `api_key` secret param, never a value.

## 32. Cards built from a Library source's declared answers

A curated Library record may carry `answers`. Each is closed and `_clean_answer` drops any that breaks the
shape (the rest still serve).

- `value`: one path, type `number | text | time | date | count`, `unit` or `unit_path`, `codes`, optional
  `window` (`now | today | tonight | tomorrow | latest`) and `measure` (`temperature`, `feels_like`,
  `precip_chance`, `precip_amount`, `conditions`, `thunderstorm`, `snow`, `wind`, `humidity`, `waves`, `swell`,
  `wave_direction`, `water_temp`, `alerts`, `kp`, `uv`, `air_quality`, `tide`, `sunrise`, `sunset`), a `time`
  may say `utc: true`, and `tbd_if {path, equals}`.
- `list`: rows at `path`, 1..4 (up to 5) row cells relative to one item, `newest_first`, `may_be_empty`,
  `filter {path, equals}` (a `"{param}"` or a short fixed value), an optional `axis {cell, step:
  day|hour|period}` (the rows are indexed by time and can be cut).
- `columns`: 2..4 parallel arrays with `limit` and an optional `axis`.
- `codes`: `wmo_weather` on a number cell (the engine's `label` step), or on a text cell a Library code table
  (`library_codes(name, value)`, a slug `[a-z][a-z0-9_]{0,39}`, for example `nws_event`). A table is kept on its
  cell type only.
- `param_bounds` on list and columns answers: `{number cell path: {min?/max?: parameter name}}`, so an asked
  comparator ("above magnitude 4") is pushed into the address.
- Paths may hold whole `{param}` segments, filled at build as quoted keys.

**Seal.** A Library pick (`seal_library_pick`, the auto-pick and the live harness alike) seals `_library_source`,
`_library_url`, `_library_params`, `_library_format`, `_library_provider`, `_library_scope`, `_library_lookup`,
`_library_label` and `_library_readings`.

**Sampling.** A sealed same-host lookup runs first (`library_resolve.resolve_lookup`); a step off the host, or a
lookup that fails, moves on. A row that promised JSON and answers with a page moves on: it never becomes a page
card.

**Choice and build.** The query layer (§36) picks: the plan's `ir.answer` is the answer (value companions from
`ir.select`, at most 4), and the build binds the IR's `time` to it. A measure no declared answer reports, or a
stretch the chosen answer cannot show, is nothing: the next source, "<provider> doesn't give <the weekend>" or
"doesn't report wind", never the headline answers instead. `_try_answers_build` builds before any derive or
mapping call: `{param}` segments filled, then extract and the typed transforms (`number`, `time`, `date`,
`count`, `label`, `zip` + `top_n` (7 daily, 12 hourly), `reverse`, `where`, `window`). A `unit_path` is read once
and frozen as a literal. A list fits when every cell is present in some row; a row missing a cell shows a dash
there; a cell missing from every row is drift (the run fails and repair fires). A `may_be_empty` list shows
"No <label> right now" while empty. A list whose rows name a place (`port`, `station`, `city`, `state`,
`county`, `country`, `location`, `site`, `place`, `venue`, `airport`, `region`) is scoped to the asked place
by a sealed `where eq`; no row naming the place means the source has nothing for it.

**Windows.** With a window and an `axis`, the `window` transform replaces `top_n` and runs before the cells'
conversions, so "this weekend" stays Saturday and Sunday on every refresh. "Today" is the source's today (its
named zone, else the offset of the row nearest now), except a UTC stamp with no zone named, whose day is the
user's. On an hour-step axis `today` and `tonight` floor at the current hour for forecast-style series
(columns); event, schedule and result lists keep the whole asked period; a next-event list floors. `tonight`
before 06:00 is the current night. A next-event or schedule list with no asked window gets a forward cut every
run (rows from now minus 15 minutes). The engine clock is the user's zone as the desktop reports it (`meta
user:timezone`; a remote device may seed it when none is stored, and `user:timezone_by` records which authority
wrote it).

**Nothing here, or a misfit: the next source.** When the response holds none of the chosen answers or an asked
value is missing, the flow moves to the next row ("<provider> has nothing for this right now"); with no other
row an asked value that is missing may fall back to the source's other `primary` answers, named "not reported
by this source right now"; otherwise the card ends honestly.

**Verify replaces the judge.** The build is deterministic, so no judge runs; `_verify_frame` (§29) checks it and
the note says "built from the Library's declared answers: <labels>" plus what the source does not report. A
declared build never renders in the page browser: a row that answers with a page moves on.

## 33. Open paths wait for the user's Keep

Every confidently wrong card came from the two open paths: a model-mapped dataset and a page. A card built from
a path other than a Library source's declared answers is held until the user keeps it.

- **Seal.** `_finalize` seals `_built_from: {path, host, title}` (closed, `ni._validate_built_from`). `path` is
  `declared` (a Library source's declared answers), `auto` (the same build picked by the auto-pick), `page`
  (an `http_page` card), `mapping` (a pasted or harvested JSON link, a Fix, a remap) or `research` (a
  researcher's recipe). `host` is the fetched host; `title` is the page's own title, else the picked row's.
  Export strips it and packs refuse it.
- **Held.** `ni.awaits_yes(item)`: state `commissioning`, `_built_from.path` in the open set (`page`, `mapping`,
  `auto`, `research`) and no `_c2_ok`. A held card lands `commissioning` like any other and differs only in
  that the due query skips it (no cadence fetch before Keep), and the board shows the reading the flow found
  (`preview`, or a manual run's ok `latest`) with `awaiting_yes {from: library|page|dataset, host, title,
  used_key?}`. Nothing sets `_c2_ok` except `POST /validate ok=true`; the scheduler, runs, repairs, export,
  import, template install and template update never do, and `update_spec` strips it. A card already past
  commissioning is never re-held by an edit; a Fix or a source-changing edit returns it to commissioning.
- **Keep** is the C2 verdict (`ok=true`; the client labels it "Keep" for a Library-picked card and "Yes,
  that's it" otherwise): `_c2_ok` sealed, the C3 kick runs, `last_checked` is cleared so the proof run follows,
  then `live`. For a form card `presentation_id: pick|second` chooses the design before the verdict (§34).
- **Try another source** is `ok=false` on a held card: a `c2_wrong` journal row, state `draft`, then
  `ni_flow.decline_reading`. The declined address (and the row's own pre-lookup address) joins `_declined` (at
  most 10, never offered again on this card: the auto-pick and research's Library hide it, and research's landing
  refuses a proposal on it however the run found it) and the flow re-runs from the user's words, so the
  auto-pick tries the Library's other sources, then research, then ends on the declined card. The response carries `repick:
  "relocate"` (or `"none"` when the re-run could not start). Never a dead end.
- **Library-only reads, links only for the rest.** A Library source without declared answers, and a web page a
  search found, are never built from; they are offered as links (`kind: link`, `found: dataset|page`) on the
  stop card, named honestly, with nothing read off them. A link the user pastes is theirs to build from
  (`mark_pasted` seals `_pasted`) and waits for Keep. A web page's `<table>` grids are readable (§40), its
  prose never.
- **Card (/ni).** Health chip "Needs your OK", a dashed border, the reading rendered as the card's own scene,
  "From the SmartBrain Library: <title> (<host>)" or "From the web page <host> - <title>" / "From the dataset
  <host> - <title>", the one-read notice for a Library card ("SmartBrain read this source once to build this
  preview. It won't read it again until you tap Keep."), and the footer "not updating yet".

## 34. Form scenes

Every card the flow builds (a Library source's declared answers, a pasted dataset, a page card, the countdown)
seals one scene node of type `form`. The engine is `smartbrain_3000/ni_forms`; this section is the product
contract around it.

**Sealed node** (closed, `ni._validate_form`). A `form` node is a leaf (no children, no `when`):
`{type: "form", form, variant, params, record: {kind, fields: [{name, label, path, type, role, unit?, currency?,
scale?, precision?, wallclock?}], rows: str|null}, spans: {desktop, phone}, design: {designer: model|rules, pick,
second: null | {id, form, variant, params, spans}, fallback?: true, gates?: [str]}, frame?: {kind, wants}}`.
`form` is in `ni_forms.types.FORMS`; at most 8 fields; `rows` names the pipeline output holding the list rows,
`null` for value answers; spans are `ni_forms.spans` keys (`d1x1`, `p2x1`...). `design.second` is the runner-up
sealed whole, so it can be chosen without a refetch. `design.fallback` marks the universal fallback (no form
survived: a plain `table` or `kv_grid` at its smallest lint-clean span). `design.gates` (at most 12 short
strings) names what a lint gate skipped and implies `designer: rules`. `frame` is the ask's frame
(`kind`, `wants` at most 8).

**Build** (design time, once). `record.from_answers` turns the answers the pipeline was built from, plus the
sample outputs, into a `DataRecord`: value gives `measure` (one row); a list gives `events` when a time cell
sits on a declared axis, else `records`; columns give `series` on an axis, else `records`. Types follow the
declaration (`number` with a unit gives `currency`, `percent` or `quantity`; `time` gives `datetime`; `codes`
give text; text with at most 12 distinct values is `category`; a blank string is a missing cell). Roles follow
the shape (the asked field or the first displayable leads as `measure`; a later time that is the reading's own
stamp is `as_of`; a text over 36 characters is `text_body` only when exactly one value answer is chosen; rows
take `name`, `status`, `kind`, `meta`, `time`, `date`, `value`, `secondary`, `lat`, `lon`). Labels are the pack's
or the user's words (`label_src: lexicon`). Then `profile`, `enumerate`, `present`: PRESENT runs through the
flow's own model call (the ni route, the consent gate) with a literal JSON skeleton; with one candidate, no
model, an invalid reply or a transport error, the rules floor picks. Both sealed spans must lay out red-free:
a red span moves to the next candidate in floor order, then the universal fallback. `display.size` is
`display_size_for_span(spans.desktop)`.

**Frame-aware design.** The ask's question kind is a prior over forms (`KIND_FORMS`: a forecast on a sub-daily
axis gives `series_line`, `conditions`, `day_table`, `agenda`; on a day axis `day_table`; schedule gives
`agenda` or `day_table`; next_event gives `next_event`; lookup, ranking and latest_items give `table`,
`ranked_list`, `kv_grid`; status and alerts give `entity_list` or `stat`; trend gives `series_line` or `stat`).
Fields the ask names (`ni_forms.asked`, including the `field_words` synonym groups) are never dropped by a plan,
`pick_default` takes the smallest span that keeps every asked field, and PRESENT's hard gate rejects a pick that
drops one. `series_line` needs at least 3 points; a series that mostly lies ahead is a forecast whose hero is the
current period. A one-row list or columns result reads like a value answer.

**Time.** The `time` and `date` transforms emit display text that keeps its instant; the record stores the
instant (ISO 8601 Z) or the calendar day, and forms format it on the device. A zoneless string with no instant is
never guessed. `Context.card_tz` is the source's IANA `zone` output when the pipeline extracted one, else the
user's zone.

**Bind** (every run, no model). `bind_scene(scene, outputs, history, image_ref, form_ctx)` with `form_ctx =
{source_url, fetched_at, viewer_tz, title, ask, cadence_s}` from the sealed spec. `record.from_spec` re-reads
the sealed field specs by `path` (types and roles are never re-derived); a sealed `rows` output that is not a
list raises `bind_type`; a measure with an empty cell, or a sealed row field gone from every row, raises
`extract_miss`. Under `ni_forms.llm.no_model()` the engine re-enumerates and takes the candidate matching the
sealed `(form, variant, params)`; when the data drifted past it, the rules floor applies and the bound node
carries `design_needs_attention: true`. The bound node is `{type: "form", form, clir: {desktop, phone}, summary,
hash, lint: {red, amber, codes}, design: {designer, pick}, clock: {next}, design_needs_attention?,
alternatives?}`; `ni._enforce_form_shape` runs `check_clir` on both CLIRs. `hash` is the desktop CLIR's
canonical sha256; two binds of one run's outputs at one fetch instant hash the same. `alternatives` (at most
one runner-up, with its own two CLIRs) appears only in the preview bind the C2 panel reads.

**History.** The handoff seals `history: {track: {<name>_h: <output path>}, max_points: 200}` for every numeric
`measure` field (at most 4); rows-shaped records track nothing.

**C2 presentation.** `POST /validate {ok, note?, presentation_id?: pick|second}`: `second` re-seals the node
under the runner-up (`swap_to_second`; the record is byte-identical, attestations preserved, the rev bumps)
before the verdict is stamped; `_present_ok` records the choice. 409 when the card is not commissioning, not a
form, or has no second; 422 for any other id.

**Clock pass.** A sealed card can go wrong with the clock alone ("Today" becoming "Yesterday", a daylight
change, a live binding's edge, the as-of age passing the stale threshold). `ni_forms.clock.next_boundary`
finds the earliest such instant; `_bind_form` seals it as `clock.next` and the engine writes it to
`next_clock`. A due item that is not fetch-due takes the clock pass: no fetch, no model, it re-binds from the
`outputs` snapshot at the layout clock's instant with the original `fetched_at` held fixed, and writes `latest`
and `last_good`. It never touches `last_checked`, the failure counters, alerts or `llm_state`; a failure
declines the pass instead of failing the card.

**Fit verdict.** See §29 stage 7: advisory, sealed as `_fit`, shown as a marker, never a block.

**What the client paints.** The CLIR for its device (`clir.desktop` on the board, `clir.phone` on a phone):
primitives (`text`, `time`, `rect`, `line`, `tri`, `dot`, `path`, `cells`, `icon`...) in content-box px with
`reading_order`, `hitmap` and `live` bindings (now marker, countdown, past-dim, age) it re-evaluates on its own
clock. Every `text` prim carries `src` in `TEXT_SRC` (data, lexicon, ask, title, key, code; never a model
word); `time` prims carry the ISO instant, `fmt` and `zone`. `lint.red == 0` on everything the server ships;
`glyph_missing` is amber. The web painter (`IrPaint.svelte`) paints DOM text and inline SVG; a client that
predates it shows an unrenderable placeholder (the version-skew rule).

## 35. Browser components

A browser component is a standalone, replaceable, hash-pinned headless browser the app loads to read a page the
way a browser does. It lives in `smartbrain_3000/browsers/`. The first engine is Obscura (v0.2.4), run as a
one-shot `fetch` subprocess, never `serve`, `mcp` or `scrape`, with no CDP port and no `--eval`. The engine only
runs inside an OS wall (macOS Seatbelt, Linux namespaces, the Docker renderer sidecar); where no wall can be built
the platform reports `unavailable` with a reason and nothing starts (fail closed).

**Manifest** (`browsers/engines/<name>.json`, validated on every use). Closed keys: `name, version, released,
source_url_template, platforms{darwin-arm64|linux-x86_64|linux-aarch64: {asset, sha256, size, members{<file>:
{sha256, size}}}}, files, executable, capabilities, argv{fetch, assets, honest}, forbidden_flags, limits{timeout_s,
script_deadline_ms, heap_mb, rss_mb, rlimit_data_mb, html_cap_bytes}, identity{mode, pool_size}, min_glibc`. An
unknown key, a placeholder outside `{url, run_dir, proxy_port, timeout_s, heap_mb, output_path, user_agent}`, a
flag outside the allowlist (`--proxy --storage-dir --timeout --dump --output --wait-until --v8-flags
--user-agent --stealth`), a forbidden token, a `--v8-flags` other than `--max-old-space-size={heap_mb}`, an
`html_cap_bytes` above `netguard._MAX_BYTES` or a non-`https://github.com/` source is a `ValueError`. `members`
pins every installed file's sha256. `SELECTABLE = {obscura}`.

**Forbidden, always.** `--allow-private-network`, `--host`, `--port`/`-p`, `--eval`/`-e`, `--file`,
`--screenshot`/`-s`, `--quiet`/`-q`, the subcommands `serve`/`mcp`/`scrape`, `--stealth` unless the manifest
declares it, and the env key `OBSCURA_ALLOW_PRIVATE_NETWORK`. The builder validates every value (an https URL
in lowercase IDNA ASCII with no userinfo and never an IP literal; a printable-ASCII UA; absolute paths; bounded
ints), inserts it verbatim, and re-walks the rendered argv token by token. The engine environment is a closed
set (minimal PATH, `LANG`, HOME/TMPDIR/XDG inside the run dir, a validated `OBSCURA_TIMEZONE`,
`OBSCURA_SCRIPT_DEADLINE_MS`, `OBSCURA_PROFILE`); no proxy variable is ever passed.

**Installer.** Layout `<data>/browsers/<name>/<version>/`. The pinned URL is fetched through netguard with exactly
one redirect allowed, to https on `{github.com, objects.githubusercontent.com,
release-assets.githubusercontent.com}`; the body streams to a `.part` file hashed as it arrives, aborting beyond
the pinned size, and the sha256 is checked before unpacking. The untar is by hand (top-level regular files named
in `files`; absolute paths, `..`, links, devices, directories, duplicates and strangers refused; written
`O_EXCL|O_NOFOLLOW` with exactly the manifest's mode), then one atomic rename and a self-test. `verify_installed`
re-hashes every member on every launch. `ensure(name)` installs only when that fails, never on an unsupported
platform, single-flight, at most 3 retries for a transient failure. Phases: `absent`, `downloading(pct)`,
`verifying`, `unpacking`, `self-testing`, `ready`, `unavailable`, `error`, `stale`, `disabled`.
`SMARTBRAIN_NO_BROWSER=1` is `disabled`. In Docker the app container never downloads an engine; the sidecar image
holds it.

**Egress child** (`jail_egress.py`, `egress.py`). The engine's only network route, spawned with a stripped
environment in its own session. CONNECT only; request head at most 8 KB; hostnames only (IP literals refused);
port 443 or the render URL's explicit port; site policy by registrable domain checked before any DNS (`own`,
`sealed`, `open`); one bounded resolution per host, refused if any answer fails `netguard._is_unsafe`. Caps per
run: 24 concurrent tunnels, 150 tunnels, 24 sites, 16 MB down, 1 MB up, 15 s idle. It reports a census
`{site, tunnels, up, down, verdict}` (at most 64 rows) to the parent on exit and writes no log.

**Walls.** `check`, `prepare`, `wrap`, `verify` (every watchdog poll; one unconfined PID is `confinement/wall_verify`),
`teardown`, `self_test`. Closed unavailable reasons: `platform`, `sandbox`, `userns_blocked`, `apparmor_userns`,
`docker_no_sidecar`, `missing_libs`.

- macOS: Seatbelt (`walls/obscura.sb`, hash pinned in code): exec of the engine only, no fork, read of the run dir
  only, outbound TCP only to the egress port on localhost. `verify` is `sandbox_check`.
- Linux: `walls/userns_boot.py` unshares USER|NS|NET|PID, maps only the app's uid, masks `/home /root /run /tmp
  /var/tmp /dev/shm /mnt /media` and the data dir, binds the engine dir read-only and the run dir read-write,
  mounts a fresh `/proc`, relays 127.0.0.1:3128 to the egress socket, sets NO_NEW_PRIVS and runs the engine.
- Docker: the renderer sidecar (`render_agent.py`, compose profile `render`) runs `network_mode: none`, read-only,
  `cap_drop: [ALL]`, no app data volume, sharing only a tmpfs volume with closed-key `job.json` and
  `result.json` files read with `O_NOFOLLOW`. The agent re-hashes the engine per job and writes a `READY.json`
  once per boot after proving it has no network. The launcher starts the `render` profile when the app reports
  `renderer.wanted` and the sidecar is `missing` or `stale` (at most one start per 10 minutes).
- NullWall: tests only, built only when `SMARTBRAIN_BROWSER_TEST_NULLWALL=1` and every executable in the engine
  dir is the fake-engine fixture.

**Runner** (`runner.py`, `proc.py`). `render(url, engine, site_policy, identity, timezone, timeout_s, want_assets)`
returns a `RenderResult {ok, status, reason, html, final_url, assets, engine, version, identity, timings,
census}`. Refusals are results. One render at a time (`_LIVE_RENDERS`, `busy` never a failure); a fresh 0700 run
dir; the egress child; the wall; a watchdog (timeout plus a 5 s grace, tree RSS, any child process, `Wall.verify`);
the whole process group killed and proven dead. Identity is `mimic` (one of 8 built-in profiles, pinned per card by
sha256 of the card id), `rotate` or `honest`. Statuses and reasons: `refused`, `unavailable`, `busy`
(`render_busy`), `confinement` (`child_spawned`, `wall_verify`, `no_main_connect`, `egress_census`), `oversize`
(`rss`, `heap`, `html_cap`), `timeout`, `blocked`, `offsite`, `engine_error`, `ok`.

**Router** (`router.py`). Tier 1 is the static page door (`pagegraph.fetch_page_graph`). A render runs only with
`allow_render` and an escalating signal: `js_shell` (under 400 visible characters with no data-bearing layer and
at least 3 scripts or a noscript notice), `blocked` (a plain 403) or `want_miss` (the static compile found
nothing). A refusal outranks every other signal; a 429 or a challenge never renders (never retry a refusal, never
solve a challenge). netguard names `FetchError(kind="challenge")` from a closed vendor header and marker table. A
per-engine breaker opens after 3 consecutive `engine_error` or `timeout` for 10 minutes.

**Eligibility.** `browsers.available(engine)` is the one answer every caller consults:
`{eligible, engine, version, reason}`, eligible only after a self-test passed in this process under the wall in
use. `browsers.prepare()` starts the install on first need, never at boot. `/api/status/overview.browsers[]`
reports each engine; `.browsers_flow` reports `{rendered_cards, drafts, worker}`; `.renderer` reports `{wanted,
sidecar}`.

**The page browser in the card flow.**

- **Static data layer first.** The extractor jail reads plain `<script type="application/json">` documents
  (never evaluated) into `data` (at most 8 documents of 1 MB, 200 leaves each and 600 in all) and ARIA grids into
  `tables`, with `signals {script_count, script_bytes, noscript_js}`. A `data_leaf` selector reads one leaf.
- **One visit per build**, at creation or a Fix only. The rendered HTML goes through the same extractor jail.
  Closed flow notes: `render_skipped:<reason>`, `render_failed:<reason>`.
- **Sealing.** Only `ni_flow._page_source` writes `source: {type: "http_page", url, render: "browser", sites}`,
  `sites` being the census's registrable domains with a tunnel, the URL's own first, at most 12
  (`ni._validate_render_fields`). Spec keys `_render_ok` and `_feed_candidates` (at most 5 `{url, site}`). Packs
  refuse them, export and install strip them, tools refuse them. Rendered cards have a 15-minute floor.
- **Consent.** No render before the tap: the pick never renders, and the paste's consent text says the one visit
  may also load the sites the page depends on. After the build: every site the page's own means the card
  commissions under the tap (then waits for Keep); any other site means state `draft`, a note "waiting for your
  OK to also load: ...", and a board `draft: {reason: "render_sites", sites}`. `POST /activate` (desktop-local,
  audited) seals `_render_ok` and commissions (409 unless a draft waits for its sites); `/commission` refuses it.
  `_render_ok` lasts only while the source is unchanged. The run allows only the sealed sites.
- **The render worker.** The tick never renders. Rendered items have their own due window (`_MAX_RENDER_CANDIDATES
  = 3`), at most one handoff per pass; one worker thread at a time calls `run_item`, whose fetch is
  `router.render_sealed`. `CAPACITY_KINDS = {render_busy, render_unavailable}` are never failures. Other outcomes
  are host-free classes: `render_refused`, `render_timeout`, `render_blocked`, `render_offsite`,
  `render_confinement`, `render_oversize`, `render_engine`. A manual run on a busy engine is 409 `{code:
  "render_busy"}`; an unavailable engine is a 200 `{status: "error", kind: "render_unavailable", code}`.
- **Feed candidates** (creation only): the visit's `--dump assets` filtered to JSON-looking URLs on the sealed
  sites, at most 5, shown on the held panel. `POST /switch-feed {url}` (desktop-local; 409 unless the url is a
  sealed candidate) re-seeds the flow with that URL as a pasted link.
- **Client.** Every field is optional (version-skew rule). The consent clause on the paste, the "Preparing the page
  browser (NN%)..." stage line, the render-sites panel, the `page browser` chip, the feed offer, and the Status
  card (`PageBrowserStatus`, from `browsers[]`) are all plain code-authored text; server strings shown are the
  sealed sites and addresses, as text.
- **Startup sweep.** At app start `browsers.proc.sweep_strays` SIGKILLs processes of this user whose executable
  lies under the browsers dir.

The SG gate (`tools/browsers-sg.py`, `.github/workflows/browsers-sg.yml`) runs the real engine under the real wall:
probe, self-test, broken-config, canary rows (0 hits on every loopback, LAN and metadata target, with and without
the proxy), no state, about:blank, kill rows, resource caps; the executed set must equal the mode's.

## 36. Query layer

An information ask becomes a closed Query IR over one declared Library answer (§32): `{answer, params, select,
where: [{col, op, value}], time: {from, to} | null, order: [{col, dir}], limit, agg}`. Ops are `= != < <= > >= in
contains names`. Time tokens: `now today tonight tomorrow yesterday weekend this_week next_week this_month
next_days:N past_days:N next_hours:N past_hours:N dow:mon..sun date:YYYY-MM-DD month:YYYY-MM year:YYYY`. `agg` is
`none | count`. Entry points: `plan_query(...) -> QueryPlan{ir, model_ir, coverage, interpretation, notes,
model_meta}`, `apply_query(ir, payload, answer, now, zone, direction)`, `recognize`, `say`. The package is
`ni_query/` (a package).

- **Ownership.** The model (one local call) owns `answer`, `time`, `where`, `order`. Code owns `select`, `limit`,
  `agg`; `params` stays `{}` (parameters belong to the resolver and the time binding). Recognizers validate the
  model's clauses and are never substitutes.
- **The call.** A system prompt with the closed grammar and a default object naming this call's answers; a user
  message with the reference instant, examples retrieved from a pool, the ask, the answer menu and the parameter
  menu. `llm.chat_json` validates against the IR schema and a structural check, with one retry whose message states
  the violated rule and repeats a skeleton naming no answer.
- **Recognizers.** `recognize` reads the ask with Microsoft Recognizers (DateTime, Number, NumberWithUnit,
  Ordinal; en-us) and dates US holidays, into `{time, numbers, ordinals, comparators}`. An explicit time phrase
  decides `time`; a counted range the model read stands against a calendar reading of a relative phrase. A
  numeric `where` survives only with a number the ask states. A `where` value must be a run of whole ask words,
  not a parameter-consumed entity and not a word of the source's own name. A non-axis `order` survives only for
  kind `ranking`.
- **Code-owned clauses.** `select` is the quantity cells (at most 4); `limit` and `agg` come from the ask's kind
  and words (a count is the IR's `agg`, not a declared count answer).
- **Rules floor.** With the model off, unavailable or invalid after the retry, the floor chooses among the
  answers the ask's words and kind fit, window-aware, and the interpretation says so.
- **Coverage and notes.** `coverage[clause]` is `bound | unbound | approximated | rules | recognizer`.
  `say(ir, answers)` renders the interpretation line by code ("<answer label> [(<selected>)] ... "), sealed as
  `spec.interpretation`; `spec.query = {ir, coverage, notes, model: {attempts, used_fewshot}}`.
- **Execution.** `apply_query` is pure Python over the sample: filters, time cut by the answer's axis and the
  direction (`_rows_direction`: a row time after now reads forward), order, limit, count. The engine grammar
  gains the matching `where` ops (`contains`, `in`) and the clock rebinding of start, since, begin and date
  parameters to the stretch's first day (`_clock_rebind`).
- **Honesty surface.** `_verify_frame` keeps its wants check; a primary asked quantity that is unbound is
  disclosed. The board row carries `interpretation` (only when the spec has one) and `query.coverage` (never the
  whole query); the C2 panel shows "Reads as ..." and a muted chip per `approximated` or `unbound` clause (named
  through `CLAUSE_WORDS`), and the card footer shows the line first.
- **Transport.** `run_flow`'s model is a `FlowModel(prompt, chat)`: the prompt stages (intent, mapping) use
  `prompt`; the message-shaped stages (query, form presentation) use `chat`. The consent gate applies to both.
- **Code tables.** A `where` value on a text cell with a `codes` table is grounded in the table's values
  (`LibraryIndex.code_values`, at most 1,000); a pack that predates tables reads as empty.

## 37. Brief cards

An ask for "the latest news on X" or "a brief on Y" (question kinds `latest_items`, `text_brief`) lands as a
brief card: a list of the source's own headlines, written by code, never a model. `ni_brief.py` owns it.

**Headlines.** `layout_of(parsed)` looks (breadth first, three levels deep, at most 200 containers) for the list
of objects with a title key (`title`, `headline`, `name`...) plus a link key (https only) or a time key. The key
tables are generic; no site, feed or ask is named. `pipeline_for` seals `extract rows <path>[0:24]`, `where title
ne ""`, `reverse` for an oldest-first source, `top_n 8`, a lenient `time` and `pick` of title, link and time.
`form_node` lays out a `ranked_list` form through `design_record(prefer_form=, prefer_spans=)`, skipping the
presentation model. A source with no such list builds nothing and the caller builds what it would have. The same
function serves the auto-pick or mapping flow (`_try_brief_build`, held for Keep) and the researcher
(`brief_spec`). A Library row that declares its answers keeps them unless the sample is an RSS or Atom feed.

**The sealed `brief` key.** Closed `{summary: bool}`; absent elsewhere. Off: no `llm` stage and `model` null. On:
exactly one `llm` stage whose only output is the string `brief_summary`, `optional: true`, `max_chars <= 600`,
and `spec.model` names a model. A researcher draft may carry neither an `llm` stage nor a summary. Export and
template install strip `brief`.

**The opt-in.** `POST /api/ni/items/{id}/brief-summary {on}` (desktop-local): 404 missing; 409 not a brief; 409
not kept (not live, degraded, failing or paused, or held for Keep); 409 with a plain reason when no local model
exists. On seals `local/auto` in `spec.model` and adds the stage (a non-local model is never resolved); off
removes both (`update_spec(origin="user")`, journaled, audited `ni_brief_summary_set`). The summary is written by
the next refresh. Headlines refresh every time, with the summary on or off. A re-research proposal carries the
summary only to another brief.

**Board row.** `brief: {summary, local, state: off|pending|ready|unavailable, label, text}`; `label` is "Summary
written by your local model", `text` at most 600 characters and read from stored outputs only when the summary is
on. The text is drawn by the web component under the label, never inside the form scene.

## 38. Compare cards

An ask that compares two to four named things (question kind `compare`) lands on a card showing every one of them
with its own number, or ends in "can't compare these yet" and why. Never a one-thing card. `ni_compare.py` owns
the card and the check; `ni_compare_flow.py` is the flow glue.

**A fan-out source, reduced at fetch time.** One `http_json` source carries a sealed `compare = {entities: [{name,
url, path}]}`. A run fetches every address through `ni_fetch.fetch_parsed` (politeness, port policy and robots
apply to each request) and reduces each response to one row `{name, value}` with `ni_compare.number_at`. The
unchanged pipeline and the `compare` form take it from there.

**The sealed key.** Closed to `{entities}`: 2..4 items, each exactly `{name, url, path}`. Names are printable,
1..80 characters and distinct. Addresses are literal, pass the `http_json` URL shape check and share one scheme,
host and port. Things are told apart by their `(address, path)` pair; two things with the same pair are refused.
`source.url` equals the first address; no headers, `secret_query` or `contact_ua`. Paths pass `ni.parse_path`.
Export and install refuse a compare card.

**Labels come from the question.** `ni_compare.asked(request)` takes the things from the ask in the user's casing
(a temporal pair is not a compare). Row names, the title and the form labels are those words; a model only says
which asked thing an address is for. The value label is the Library answer's declared label, or the closed
measure the ask names, or "Value".

**The check.** `ni_compare.shown / reasons / refusal`: an asked thing counts as shown when a row names it in a
text cell and has a number of its own, or when one row names every asked thing and has a number. A title naming a
thing does not count. `refusal(spec, outputs)` returns "can't compare these yet: it shows N of M compared (X
missing)" or the more-than-four reason. It runs on every build path (`ni_flow._finalize` and
`ni_research_flow._can_land`), and `ni_compare.drift` re-runs it on every refresh for a card whose source is a
compare (`constraint_drift`, §23.1).

**Statuses.** `compare_partial` (an `NIError` kind): an address failed or had no number; the run fails naming the
things and `last_good` keeps showing. It maps to `source_dead` in `ni_refresh` and is not L2-eligible. A busy
host defers the whole run.

**The auto-pick.** `try_compare` runs first. For each thing it asks the Library about that thing alone, keeps rows
that need no key, email, lookup or clock parameter, and looks for one source with one row per thing and a declared
plain number answer for the asked metric; it fetches each address through the flow's fetcher, needs one unit
across the things, and seals with `_finalize` (held for Keep, `_built_from.path` `auto`). Each source's whole
attempt is one error boundary. No source builds it: the researcher gets the ask; else the declined card.

**Researcher.** The terminal tool `propose_compare({metric, unit, things: [{name, url, path}]})`: every asked thing
exactly once, every address a handed-over address with a fetched sample found by a search whose query named that
thing and no other asked thing, numbers read by code from the stored samples. `propose_card` for a compare ask is
refused. The closed-world local tier declines a question with two or more entities. A compare reads the current
value only.

## 39. The declined card

A decline is an ask the system cannot answer reliably. It ends as one terminal flow state, `declined`, carrying a
closed `reason`, one plain line and up to three links, sealed as `_decline_card {reason, line, links}` and repeated
on the flow field as `decline {reason, headline, line, links}`.

**One table.** `ni_master.ENDINGS` maps every ending that is not a card to `(flow state, decline code | None)`;
`ni_flow._end(store, item_id, ending, reason, links=None)` is the only writer of a decline. An ending declines
when nothing the user can do on this card would change the answer; it keeps its own state when the user can still
act.

| ending | lands in | code |
|---|---|---|
| `research_declined_checks` (the verifier refused the card twice) | `declined` | `verify_refused` |
| `research_declined_budget` (budget or steps spent, or the model stopped without a card) | `declined` | `research_gave_up` |
| `research_declined_no_source` (the closed-world tier found no Library source) | `declined` | `no_answering_source` |
| `no_source_research_off` (no Library source declares an answer; research off, unconsented or no route) | `declined` | `no_answering_source` |
| `no_source_rows_exhausted` (every row tried failed or did not fit; research gave nothing) | `declined` | `no_answering_source` |
| `compare_unbuildable` (no source reads the things side by side, or more than four) | `declined` | `compare_unbuildable` |
| `compare_refused_at_build` (a card showing fewer things than were asked) | `declined` | `compare_unbuildable` |
| `source_cant_answer` (the one source the user pasted or Fix re-reads does not answer) | `declined` | `source_cant_answer` |
| `ask_unsupported` (this kind of ask is not built) | `declined` | `ask_unsupported` |
| `paste_a_link` (the user reopened a decline: the paste box, the decline's links as pages) | `source` + pick marker | none |
| `needs_key_or_email` | `awaiting_access` | none |
| `model_consent_question` | `unsupported` + question | none |
| `date_question` | `unsupported` + question | none |
| `fault` (fetch, mapping, assembly, or the researcher failing to run) | `failed` | none |

**A research fault is not a verdict.** `loop.Outcome.status` is `proposed`, `declined` or `fault`. A run that could
not finish (the model route raised, a reply or tool raised) ends `fault` and becomes the failed card with Retry,
never a decline; the eval reads it as `error`, so an outage cannot earn the decline credit. Re-research records a
fault as `repair_research_fault` (no back-off streak).

**Codes.** `ni_master.DECLINE_REASONS`: `no_answering_source`, `research_gave_up`, `verify_refused`,
`compare_unbuildable`, `source_cant_answer`, `ask_unsupported`. `DECLINE_LINES` holds a default line per code (a
caller may pass a more specific one, cut to 200 characters); `DECLINE_HEADLINE` is "I couldn't build a reliable
card for this". The record's `error` is `"<code>: <line>"`. The line is built only from code copy, the intent's
`place` and `wants`, and Library-declared labels; a link title is the only fetched string, shown as text in a link
list.

**Links.** At most 3 `{title, url, host}`, https only, one per address (`ni_master.clean_links` is the only
filter). They come from what the run saw (the researcher's Library and search results; the pick's link rows). The
card lists them read-only; none builds a card.

**Never refreshes, never fetches.** A declined card is a shell (draft, not enabled): `due_items` does not list it,
`/run` and `/commission` refuse it, and the sweep skips a terminal record. A finished card whose Fix ended
declined keeps rendering its working payload (the terminal record is hidden).

**Wire shape.** `{state: "declined", error, reason (= the line), decline, reopen, actions}`. `FlowDecline`,
`DeclineLink` and `DeclineReason` are in the generated schema (`app/smartbrain_3000/ni_schema.json`, produced by
`tools/gen-ni-schema.py`; `--check` fails when it is stale). Actions: a shell `["retry", "pick_source", "delete"]`,
a finished card `["retry", "fix", "refine", "delete"]`. The client (`DeclinedCard.svelte`) shows the headline, the line, the
https links (at most three) and the buttons the page declares; the page owns the requests.

## 40. Page tables

Research may read data from an ordinary web page's `<table>` grids, never from its prose. The mechanism is
general; nothing in it names a site or an ask. Every build runs the verifier checks, a web card is always held for
Keep, and it carries the quality tier `page_reading`.

**Source.** `{"type": "http_json", "url": "https://...", "format": "table", "table": {"headers": [...],
"context": "..."}}` (`ni._validate_table_source`). The URL is https; no headers, key, email or compare. `table` is
the locator (`ni_table.validate_locator`): the table's normalised header names (2..12, lower-cased, whitespace
collapsed, at most 60 characters each) and, when it has one, the caption or heading it sits under (`context`, at
most 80). Only a spec whose `recipe.tier` is `page_reading` carries it (`ni._validate_table_owner`).

**One decode rule.** A page is decoded once, by the jail, from its raw bytes (BOM, then `<meta charset>`, then the
HTTP header charset), so research and refresh read the same text.

**Reading a page.** `ni_fetch.fetch_page_tables` fetches once through `netguard.safe_fetch_page` (per-host
budget, robots.txt, port policy, no redirect) and reads it in the jail: at most 8 tables x 500 rows x 12 columns x
120 characters, each with its caption and the latest h1-h3 heading, hidden cells excluded. `ni_table.read` turns a
grid into a data table (at least two named columns and one data row; a grid with no `<th>` row promotes its first
row only when every cell is a distinct label and two data rows follow), and `ni_table.payload` into CSV-shaped
`{"columns", "rows"}` in page order.

**Finding the table again.** `ni_table.locate(tables, locator)`: an exact header match first, else a table that
still holds every sealed header in order. Several candidates must be told apart by the sealed `context`. None, or
several it cannot tell apart, is `TableGone` (`table_gone`, `table_ambiguous`): position alone is never a name, and
a different table is never picked. `ni._fetch_http_table` maps it to `fetch_failed`: the card keeps its last good
value, the streak runs, and `ni_refresh` treats it as `source_dead`. A robots refusal is `fetch_failed:
robots_disallowed`.

**Researcher.** `fetch_sample` on an HTML body returns a fenced digest (`ni_research.digest.table_digest`): at most
four tables with `table` (index), `about`, `columns`, `rows` and three sample rows of at most 40 characters a cell,
plus feed links and JSON-LD types the page announces. The whole digest is under 3,200 characters. A page without a
data table is `not_data`. The one non-table page string used is the table's caption or nearest heading (at most 80
characters, cleaned and fenced), sealed as `context` as a name and a tie-break, never as a value. A draft names
`source.table = {"index": N}`; `tools._seal_table` replaces it with the locator. `_checks` gains `subject` from
`page_verify.verify_page_reading` (every value must be on the page; right-now asks need a freshness signal); this
gate runs at Keep only, and a refresh re-runs the constraint checks but not the freshness signal. `propose_card`
stamps `tier: page_reading`; `propose_compare` refuses a page table.

**Held for Keep.** The landed card's `_built_from.path` is `research`; `awaits_yes` holds it, `due_items` does not
list it, and the awaiting block's `from` is `page`.

**Bounds.** Everything over fetched bytes is capped before it is scanned: `ni_table` cuts every cell at 200 and every
header at 60, uses no regex, and `tests/test_ni_regex_budget.py` sweeps `ni_table`, `page_verify` and the digest on
hostile shapes.
