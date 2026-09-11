# Neural Interface item format (v1)

Internal contract for the Neural Interface (NI) subsystem: the item spec, the scene
grammar, the pipeline, the data contract, and the lifecycle state machine. The Python
validators (`app/smartbrain_3000/ni.py`) and the TypeScript renderer
(`web/src/lib/ni/`) both implement THIS document; when they disagree, this document
wins and both are bugs.

Design laws (decided 2026-09-08, full rationale in the operator's design record):

- **Deterministic core, AI at the edges.** Models run at design time, commissioning,
  and repair time only. The runtime path — fetch → extract → transform → contract
  check → bind → render — is pure deterministic code. The same payload always
  produces the same scene.
- **Authorization is not trust.** The user's choice of a source authorizes the
  connection; the fetched content stays untrusted forever. Every model output that
  was derived from fetched bytes must be a closed-schema, egress-inert artifact
  (scene trees, extract paths, transform ops — never URLs, headers, schedules, or
  free-form markup).
- **Template-native.** Every item is an instantiated template. `params` declare the
  slots; local items simply have all slots filled. Export-as-template is a strip of
  filled values back to slots, never a schema change.
- **Nothing renders that did not validate.** The scene validator and the payload
  binder run server-side on every snapshot write; the client renderer additionally
  refuses unknown node types. Malformed data can never reach the page.

## 1. Storage (migration 40)

Follows the house sealed-body + plaintext-operational-columns convention
(schedules/feeds):

- `ni_items` — plaintext: `id TEXT PRIMARY KEY`, `enabled BOOLEAN`, `state TEXT`
  (§6), `interval_minutes INTEGER`, `last_checked TIMESTAMP`, `last_status TEXT`
  (host-free), `consecutive_failures INTEGER`, `first_failure_at TIMESTAMP`
  (streak marker; §6), `position INTEGER`, `spec_rev INTEGER`,
  `created_at TIMESTAMP`, `updated_at TIMESTAMP`; sealed body (AAD
  `ni_item:<id>`): the spec (§2).
- `ni_snapshots` — `item_id TEXT`, `slot TEXT` (`latest` | `last_good` | `preview`;
  v2 adds `history` (§11) and `alert_state` (§12) — the slot set is app-level, no
  schema change),
  `nonce BLOB`, `ciphertext BLOB` (AAD `ni_snapshot:<item_id>:<slot>`), `ok BOOLEAN`,
  `created_at TIMESTAMP`, `PRIMARY KEY (item_id, slot)`. Sealed body: the bound
  payload (§4.3) — the data, not the scene.
- `ni_revisions` — `item_id TEXT`, `rev INTEGER`, `nonce BLOB`, `ciphertext BLOB`
  (AAD `ni_revision:<item_id>:<rev>`), `origin TEXT` (`user` | `agent` |
  `repair_l1` | `repair_l2` | `template`), `created_at TIMESTAMP`, `PRIMARY KEY
  (item_id, rev)`. Keep the last 10 per item; pruning in code.
- `ni_runs` — `item_id TEXT`, `ts TIMESTAMP`, `status TEXT`, `duration_ms INTEGER`,
  `error TEXT` (host-free class only), `contract_ok BOOLEAN`. Keep the last 50 per
  item; pruning in code. Plaintext throughout — operational telemetry, no content.

No foreign keys; `NIStore.delete` cascades in code (feeds precedent).

## 2. Item spec (sealed body of `ni_items`)

```json
{
  "version": 1,
  "title": "Spending this week",
  "goal": "<the user's words, verbatim — never rewritten>",
  "params": {
    "symbol": {"label": "Ticker symbol", "kind": "string", "value": "ACME"}
  },
  "source": { ... §3 ... },
  "pipeline": [ ... §4 ... ],
  "scene": { ... §5 ... },
  "display": {"size": "small"},
  "contract": null,
  "repair_policy": {"l1": true, "l2_frontier": false},
  "model": null
}
```

- `params.*.kind` ∈ `string | number | secret`. A `secret` param's `value` is always
  a SecretStore key name in the `ni:<item_id>:` namespace, never the secret itself.
  `{{param:NAME}}` placeholders may appear only where a field's schema says so.
- `display.size` ∈ `small | wide` (wide spans two grid columns).
- `contract` is system-written at commissioning (§7); the agent may never set it.
- `model` optionally overrides the `ni` route for `model` sources (schedules.model
  precedent).
- Interval floor: `interval_minutes >= 1`, clamped (never an error). One-shot items
  do not exist in v1; `enabled=false` is the pause.
- v2 optional keys: `"history"` (§11) and `"alerts"` (§12). A v1 app's validator
  rejects specs carrying them (closed-key law) — that is the intended forward
  refusal.

## 3. Sources (v1: three types, closed set)

`http_json`:
```json
{"type": "http_json", "url": "https://api.example.com/quote?sym={{param:symbol}}",
 "headers": {"X-Api-Key": {"$secret": "ni:<item_id>:api_key"}}}
```
- Fetched via `netguard.safe_fetch_json` only. `validate_public_url` runs on the
  param-substituted URL at every create / update / commission (skipped when a
  referenced string param is still empty; commission re-checks). The URL's
  **scheme + authority is frozen literal**: `{{param:...}}` may appear only inside
  the path or query, never in the scheme/userinfo/host/port — validators refuse
  a placeholder there. Param values that DO land in the URL are percent-encoded
  at substitution time (`urllib.parse.quote(safe="")`) so a value can never
  rewrite URL structure.
- Header values are literal strings or `{"$secret": "ni:..."}` refs — nothing
  else. `{{param:...}}` is forbidden inside plain header values (a param cannot
  smuggle auth-shaped bytes into a header). Auth-shaped literal header names
  (`authorization`, `proxy-authorization`, `cookie`, any name containing
  `token`/`secret`/`key`) are refused unless the value is a `$secret` ref.
  `$secret` refs must start with `ni:` at spec validation; the loader further
  enforces the full `ni:{item_id}:` scoping (a spec cannot borrow another
  item's credentials).
- `$secret` values resolve at fetch time by the engine; the resolved secret is
  host-bound: it attaches only when the request host equals the host recorded
  when the credential was entered (`put_credential` IDNA-lowercases the host so
  the compare matches `urlparse().hostname`). Any other host refuses the fetch
  outright (`secret_host_mismatch` → permanent broken). Secrets never ride an
  http request (`secret_requires_https` refusal).
- **Redirect discipline** (credential exfiltration guard): whenever the fetch
  carries ANY header (secret or literal), the engine passes
  `allow_redirects=False` through `safe_fetch_json` / `_guarded_get`, so a 3xx
  raises `FetchError("redirect refused")` before the header re-sends to a
  rewritten host. Header-free requests keep the default redirect behavior.
- Response caps: 2 MB, `application/json`/`text/` content types, 8s per-read
  timeout (netguard defaults).

`model`:
```json
{"type": "model", "instruction": "Write a one-paragraph morning briefing …"}
```
- Runs through `gateway` on the `ni` route (new capability, default local), or the
  item's `model` override. Output is plain text handed to the pipeline as
  `{"text": "<output>"}`. The instruction is user-visible spec content.

`internal.schedule`:
```json
{"type": "internal.schedule", "schedule_id": "<id>"}
```
- Reads the newest `schedule_runs` row for that schedule (message only). Zero
  egress. Payload to the pipeline: `{"message": str, "status": str, "ts": str}`.

## 4. Pipeline

An ordered list of stages; each consumes and produces a JSON value. The implicit
final stage is always bind + render-validate (§4.3).

### 4.1 `extract`

```json
{"op": "extract", "paths": {"price": "quote.latest", "rows": "items[0:5]"}}
```
Path grammar (shared with scene bindings, one implementation):

```
path     = segment ("." segment)*
segment  = key | key "[" index "]" | key "[" slice "]"
key      = [A-Za-z_][A-Za-z0-9_-]*
index    = "-"? digits
slice    = digits? ":" digits?
```
A path that fails to resolve is a stage failure (→ run fails, §6). Output is the
object of named extracts; downstream stages and the scene see only these names.

### 4.2 `transform`

```json
{"op": "transform", "apply": [
  {"fn": "round", "field": "price", "digits": 2},
  {"fn": "sort_by", "field": "rows", "key": "amount", "dir": "desc"},
  {"fn": "top_n", "field": "rows", "n": 5}
]}
```
Closed function set v1: `round(field, digits)`, `scale(field, factor)`,
`rename(field, to)`, `pick(field, keys)` (for lists of objects: keep only these
keys), `sort_by(field, key?, dir)`, `top_n(field, n<=50)`. All pure; type
mismatches are stage failures, never coercion guesses.

Added in v2 (same closed-set discipline):
- Aggregates over a list-of-objects field: `sum(field, key, as)`, `avg(field,
  key, as)`, `min(field, key, as)`, `max(field, key, as)`, `count(field, as)` —
  each writes a NEW numeric output named `as` (the list is untouched; `as` must
  not collide with an existing output; numeric key values required, mismatch =
  stage failure; empty list ⇒ count 0, others = stage failure `empty_aggregate`).
- `delta_prev(field, series, as)` — current numeric `field` minus the LAST point
  of history series `series` (§11); writes `{value, direction: "up"|"down"|
  "flat"}` to `as`. When the series is empty (first run) it writes
  `{value: 0, direction: "flat"}` — never a failure, so commissioning passes.

### 4.3 Bind + render-validate (implicit, always last)

Binding walks the scene, resolves every `{"$bind": path}` and `{{path}}`
interpolation against the pipeline output, enforces §5 caps, and produces the
**bound payload**: the scene with data inlined — this is what `ni_snapshots`
stores and what the client renders. Any unresolved binding, type mismatch, or cap
violation fails the run; `last_good` keeps rendering.

## 5. Scene grammar (v1)

A scene is a tree of nodes. Closed node set; unknown `type` = validation failure
server-side and refusal client-side.

Layout:
- `{"type": "stack", "dir": "v" | "h", "gap": "sm"|"md", "children": [...]}`
- `{"type": "grid", "cols": 2..4, "children": [...]}`
- `{"type": "divider"}`

Content (each `value`-like prop accepts a JSON literal or `{"$bind": "<path>"}`):
- `{"type": "text", "value": "…{{path}}…", "role": "title"|"label"|"value"|"caption",
   "tone": "default"|"muted"|"accent"|"ok"|"warn"|"danger", "size": "sm"|"md"|"lg"}`
- `{"type": "number", "value": …, "format": "plain"|"compact"|"percent"|"currency",
   "unit": "…", "tone": …, "size": …}`
- `{"type": "chip", "value": …, "kind": ""|"accent"|"ok"|"warn"|"danger"}`
- `{"type": "bar", "value": …, "max": …, "tone": …}`  (rendered 0..1 of max)
- `{"type": "icon", "name": "<lucide subset name>", "tone": …}`
- `{"type": "repeat", "items": {"$bind": "<path to list>"}, "max": 20,
   "template": <node>}` — inside `template`, paths beginning `item.` resolve
   against the current list element.

Added in v2:
- `{"type": "spark", "points": {"$bind": "history.<name>"}, "kind": "line"|"bars",
   "tone": …}` — a sparkline over a history series (§11) or any bound list of
   numbers / `{t, v}` points; ≤ 500 points (excess = bind failure); rendered as
   hand-rolled inline SVG (polyline / rects), stroke/fill from the tone token,
   fixed viewBox, width 100%, no axes. Non-numeric point = bind failure.
- `{"type": "gauge", "value": …, "min": 0, "max": …, "tone": …, "label": "…"}` —
  an arc gauge; `max > min` required, value clamped visually to [min, max]
  (clamping is presentation; the raw value still binds for conditions).

Conditions (v2, **bind-time — the client never sees them**): any CONTENT node may
carry `"when": [rule…]` (≤ 5 rules):

```json
{"when": [{"left": {"$bind": "delta.value"}, "op": "lt", "right": 0,
           "set": {"tone": "danger"}}]}
```
- `left`/`right`: `{"$bind": path}` or a JSON scalar. `op` ∈ `lt|le|gt|ge|eq|ne`
  (ordering ops require numbers on both sides — mismatch = bind failure `when_type`;
  eq/ne compare scalars strictly, no coercion).
- `set`: `{"tone": <tone enum>}` and/or `{"hidden": true}`. Rules evaluate in
  order at BIND time; later tone wins; `hidden` drops the node from the bound
  payload entirely. The stored/bound payload carries only the RESULT — `when`
  never survives binding, and the client validator continues to REJECT it in a
  bound payload. Determinism is preserved: conditions are pure functions of the
  run's data.

Rules:
- **Text is text.** Bound strings render as plain text — no markdown, no HTML, no
  links. (Injection point P3: a lying string can render, but it cannot become UI.)
- Tones map to design tokens only; no raw colors anywhere in the grammar.
- Caps: ≤ 100 nodes after repeat expansion, depth ≤ 8, text ≤ 2000 chars, repeat
  `max` ≤ 50. Enforced at bind time and again by the renderer.
- Reserved for later phases (validators must REJECT, so old apps refuse new
  scenes rather than mis-render them): `image`, `on_tap` (behaviors).

## 6. Lifecycle state machine (fully enumerated)

States (plaintext `ni_items.state`):
`draft → commissioning → live ⇄ degraded ⇄ failing → broken`, plus `paused`
(user, from any post-draft state; resume returns to `commissioning` if the item
was never live, else `live`).

Approval IS consent. NI write tools are REVIEWED + egress + non-rememberable, so
the create/update handler only ever runs after explicit human approval. The
default landing state for a fresh `create_ni_item` is therefore
**`commissioning`** (not `draft`) — the C1 tick happens on the next scheduler
pass. Two exceptions land it in `draft` instead: (a) any `secret`-kind param
whose value is empty (the credential must be entered via
`PUT /credential` before the engine tries), and (b) an explicit `draft: true`
in the create args (the agent's "show me first" affordance).

An `update_ni_item` whose source effectively changed sends the item straight to
`commissioning` too — the approved update card IS the re-consent. Every
`update_spec` also strips `_c2_ok` and `contract` from the sealed body and
resets `consecutive_failures` + `first_failure_at` (both attestations describe a
specific spec and are meaningless once the spec moves).

- `draft` — spec + `preview` snapshot (dummy data, agent-invented, validated
  against the same grammar). Never fetches. Never expires in v1. Left by the
  Activate button (`POST /api/ni/items/{id}/commission` — refuses 409 unless
  state is `draft` and every secret param has a value).
- `commissioning` — entered on create (default), on activate, or on the
  re-consent path after an update whose source effectively changed.
  - **C1 (immediate)**: one real run of the full pipeline. Failure → stays
    `commissioning`, error surfaced for redrafting.
  - **C2 (user)**: the real result renders on /ni with "Looks right" /
    "Something's wrong". Wrong → back to draft with the user's note.
    `POST /api/ni/items/{id}/validate` refuses with 409 unless the item is
    currently `commissioning` (integrity: a verdict outside commissioning has
    no C1 output to endorse).
  - **C3 (automatic)**: the next engine-cadence run must pass and satisfy the
    contract captured at C1. **If `_c2_ok` is set but no contract was captured
    yet, the first clean run captures the contract and STAYS commissioning**
    (one more clean run required to promote to `live`). If a contract IS
    present when `_c2_ok` fires, the pre-bind check runs on every subsequent
    run; a violation is a run failure that keeps the item at `commissioning`.
- `live` — engine runs at cadence; every run contract-checked.
- `degraded` — latest run failed or violated contract; `last_good` renders dimmed.
  `latest` becomes an `ok=false` marker with an empty payload so the board's
  latest-if-ok-else-last_good fallback picks up last_good on its own.
- `failing` — ≥ 3 consecutive failures; effective interval doubles per failure
  beyond 3 (cap 24h).
- `broken` — 8 consecutive failures over ≥ 7 days **measured from
  `first_failure_at`** (the streak start), NOT `created_at`. A long-lived
  healthy item that only starts failing today therefore can't be classed
  broken from its birthday. `first_failure_at` is set on the FIRST failure of
  a streak, cleared on success, on `update_spec`, and on `commission()`. A
  permanent refusal (e.g. credential host mismatch) escalates straight to
  `broken` without waiting on the counter. Engine stops scheduling broken
  items; carrier-row alert fires; only user/agent action (edit → re-commission)
  leaves it.

Every failure records an `ni_runs` row with a host-free error class. `mark_checked`
on every attempt is the backoff (feeds law).

## 7. Data contract

Captured by the system at commissioning (C1 result, confirmed by C2):

```json
{"shape": {"price": "number", "rows": "list", "rows[].amount": "number"},
 "bounds": {"price": {"min": 0}}}
```
- `shape` — type fingerprint of every pipeline output the scene binds.
- `bounds` — optional plausibility ranges (system-suggested, user-editable).
Checked on every run after binding; a violation is a contract failure (run fails,
item degrades) even when everything parsed. Repair (later phases) targets contract
satisfaction, not mere parsing.

## 8. Engine pass

`ni.tick(app, pass_budget_seconds, breaker_open=None)` called from
`scheduler.tick` after the feeds pass, feeds contract verbatim: locked →
return; own cursor; due =
`enabled AND state NOT IN ('draft','paused','broken') AND (last_checked IS NULL OR
age > effective_interval)`, `NULLS FIRST`, oldest first; `_MAX_ITEMS_PER_PASS = 3`;
per-item try/except; wall-clock budget between items; `mark_checked` on every
attempt; host-free `last_status`. `model` sources additionally require
`gateway.local_available()` when the route is local, and respect the scheduler
breaker: `_auto_update_ni` passes the scheduler's `_breaker_open` to `tick`, and
while it returns True, model-source items are SKIPPED (they stay due — no
`mark_checked`, so the next tick with a healthy gateway picks them up).
`http_json` and `internal.schedule` items are unaffected by the breaker (they
don't touch the gateway).

Bound-payload guards on every write: `bind_scene` results are re-checked against
per-prop expectations (text/chip string ≤ 2000 chars; number/bar numeric+finite;
repeat items list) — a mismatch is `bind_type`. The JSON-serialized bound
payload is capped at 256 KiB before `write_snapshot`; exceeding = run failure
`payload_too_large`. Any exception on the whole finalize path (contract check,
bind, snapshot write) is wrapped as `NIError` so mark_checked + `ni_runs` row +
failure bump + `latest` ok=false snapshot fire on every failure path (a raw
`ValueError` used to skip the run row entirely).

## 9. Tools and tiers (v1)

| tool | tier | notes |
|---|---|---|
| `list_ni_items` | OBSERVE | plaintext state + titles |
| `read_ni_item` | OBSERVE | spec + health + latest bound payload (untrusted-data provenance line first, KB-tool precedent) |
| `create_ni_item` | REVIEWED, egress | full spec + preview payload; validates everything; lands in `commissioning` (approval == consent) unless a secret param is unfilled or `draft: true` is passed |
| `update_ni_item` | REVIEWED | partial; a source change (URL/headers/type/instruction OR the value of any param referenced by `source.url` via `{{param:}}`) sends the item to `commissioning` (re-consent). Optional `preview_payload` rewrites the preview snapshot alongside the spec — note the preview is stale until this is passed |
| `set_ni_item_enabled` | REVIEWED | pause/resume |
| `run_ni_item_now` | REVIEWED, egress | clears `last_checked`; refuses draft/paused/broken |
| `delete_ni_item` | IRREVERSIBLE | typed confirm |

`NI_WRITE_TOOLS = {create, update, set_enabled, run_now}` joins
`UNATTENDED_NEVER_AUTO`. None of the NI write tools are ever rememberable
(consent.py returns `None` for unlisted egress tools by default — leave them
unlisted on purpose).

The creation consent law: the ActionCard for `create_ni_item` must show the source
URL host + path unmissably and the user must have picked the source in
conversation before the card is parked. Approving the card is what moves
`draft → commissioning`.

## 10. HTTP surface (v1)

- `GET  /api/ni/board` — all items: plaintext state/freshness + decrypted bound
  payload (`preview` for drafts, else `latest` ok / `last_good`).
- `GET  /api/ni/items/{id}` — spec (secrets as names), health, run history.
- `POST /api/ni/items/{id}/validate` — C2 verdict `{ok: bool, note?: str}`.
  Refuses 409 unless the item is currently `commissioning`.
- `POST /api/ni/items/{id}/commission` — the Activate button on the drafted
  card: `draft` → `commissioning`. Refuses 409 unless state is `draft`, and
  409 with a clear detail when any secret param is still unfilled. Response
  `{"state": "commissioning"}`.
- `POST /api/ni/items/{id}/run` — manual refresh (engine path, same guards).
  Refuses 409 for `draft` and `broken`, and 409 when paused (`enabled=false`).
- `PATCH /api/ni/items/{id}` — position/display/enabled (UI edits).
- `DELETE /api/ni/items/{id}`.
- `PUT  /api/ni/items/{id}/credential` — enter a secret param value
  (desktop-local, body `{name, value, host}`); stores under `ni:<id>:<name>`,
  host-bound (host IDNA-lowercased at store time so it matches
  `urlparse().hostname`). Secrets never travel through chat or tool args.
- All handlers 423 → the standard locked contract; literal paths before `{id}`
  (schedule_routes ordering comment).

## 11. History (v2)

Optional spec field:

```json
{"history": {"track": {"priceLog": "price"}, "max_points": 100}}
```

- `track`: name → pipeline-output path (§4.1 grammar). ≤ 4 series; values must be
  numeric at run time (non-numeric = run failure `history_type`). Series names
  share the output namespace rules (no `item`, no collisions with pipeline
  outputs).
- After every SUCCESSFUL run (commissioning included), the engine appends
  `{"t": "<iso8601 UTC>", "v": <number>}` per tracked series and trims to
  `max_points` (≤ 500, default 100), storing all series in the sealed
  `ni_snapshots` slot `history` (AAD `ni_snapshot:<id>:history`). Failed runs
  append nothing.
- The binder exposes each series as `history.<name>` (a list of `{t, v}`) to
  `$bind`, `spark.points`, and `delta_prev`. `history.*` paths are read-only
  inputs — extract/transform outputs cannot be named `history`. A series' `name`
  also must not collide with any pipeline output (the tracked PATH points at an
  output; the NAME is a separate namespace entry).
- Tracked series with no points yet are **seeded as empty lists** for the binder,
  so a spark over `history.<name>` binds to `[]` on the first run (C1) and
  renders as an empty placeholder — an item charting its own history must be
  able to commission. `delta_prev` likewise never fails on an empty series (§4.2).
- Deleting the item deletes the slot (cascade); a source-change rewind KEEPS
  history (same subject, new plumbing) unless the user deletes the item.
- Renaming or removing a tracked series drops the old series on the next successful
  append — retention guarantees the SAME names across rewinds, not renamed ones.

## 12. Alerts (v2)

Optional spec field — the same closed condition grammar as §5 `when`, promoted:

```json
{"alerts": [{"name": "price-drop", "left": {"$bind": "delta.value"}, "op": "lt",
             "right": 0, "message": "{{title}}: price fell to {{price}}",
             "cooldown_minutes": 60}]}
```

- ≤ 5 rules; `name` = short slug (charset `[a-z0-9-]{1,40}`, unique per item);
  `left`/`op`/`right` exactly as §5 conditions; `message` is a template
  interpolated against the run's outputs plus `{{title}}` (plain text, ≤ 500
  chars bound); `cooldown_minutes` clamped to ≥ 5, default 60.
- Evaluated by the engine after every successful bind, on LIVE items only.
  **Edge-triggered**: a rule fires only on the false→true transition of its
  condition, and never again until it has been false at least once — and never
  within its cooldown. Per-rule state (`active: bool`, `last_fired: ts`) lives in
  the sealed `alert_state` snapshot slot; plaintext columns carry nothing about
  alert rules or their names.
- A fired alert (and every `broken` transition) posts to the **NI carrier row**:
  a third reserved schedule id (`_CARRIER_IDS` pattern, id `neural-interface`,
  title "Neural Interface") via `schedules.record_run`-style writes — riding the
  existing unseen badge, the chat `### Scheduled Item ###` injection, and the
  /info archive with zero new notification plumbing. Alert body = the bound
  `message`; broken body = "<title> is broken — open Neural Interface, or ask me
  to fix it." Host-free, as always.
- The evaluation is deterministic (pure function of run outputs + stored state);
  a rule whose binds cannot resolve marks the RUN failed (`alert_bind`), exactly
  like a scene bind failure — alerts are part of the contract surface, not
  best-effort.
- Renaming an alert rule (changing its `name` slug) resets its per-rule
  `alert_state` — the new name has no prior `active`/`last_fired` record, so a rule
  whose condition is already true at the next run may fire immediately. Accepted
  behavior: the operator saw the rename in the approved update card.

## 13. `llm` pipeline stage (v2b — the ONE deterministic-core exception)

```json
{"op": "llm", "instruction": "Summarize the headlines in one sentence each.",
 "output": {"summary": "string", "count": "number"}}
```

- Position: anywhere in `pipeline`, executed in order like any stage. At most ONE
  llm stage per item.
- `instruction`: user-visible spec text, ≤ 2000 chars, `{{param:}}`-free.
- `output`: flat closed map, ≤ 6 fields, name rules as other outputs (no
  collisions, no reserved names); types ∈ `string | number | boolean`
  (strings bound ≤ 2000 chars). The stage's outputs join the pipeline namespace
  like extract outputs.
- Execution: the current pipeline value is serialized (≤ 8 KB excerpt, truncation
  marked) and sent WITH the instruction to the `ni` route (or the item's `model`
  override) — **local models only**: if the resolved model is not local
  (`gateway.is_local` false), the run fails with class `llm_requires_local` —
  never a cloud fallback, the selfreview precedent. The model call carries NO
  tools; the data excerpt is fenced and heading-forgery-neutralized (claudecli
  precedent) — injection containment is the output channel: the reply must be a
  single JSON object matching `output` exactly (strict parse, exact keys, type
  check; **`number` values must be finite** — `NaN`/`±Infinity` are refused so a
  non-finite value never reaches the contract check or the sealed snapshot,
  D4 audit 2026-09-09); one retry appends a FIXED generic sentence asking again
  for the exact schema (never the raw parse error — safer: no payload fragments
  can re-enter the prompt via the error message, D9 audit 2026-09-09); a second
  failure raises `llm_output` and `last_good` keeps rendering.
- Engine discipline (§8 additions): items with an llm stage clamp
  `interval_minutes` to ≥ 5; at most `_MAX_LLM_ITEMS_PER_PASS = 1` such item per
  pass; the pass requires `gateway.local_available()` for them (busy slot ⇒ the
  item simply stays due — not a failure) and respects the scheduler breaker.
- Honesty: the board row for such an item carries `"interpreted": true` and the
  card shows an "Interpreted" chip — the user can always see which cards contain
  a model's reading of the data rather than pure arithmetic.
- Contract note: llm outputs are contract-checked like any output (shape/types),
  which bounds drift; their VALUES are inherently non-deterministic — bounds in
  the contract apply if present.

## 14. L1 self-repair (v2b — local-model spec repair, egress-inert by construction)

Trigger: an item reaches `failing` (≥ 3 consecutive failures) with a
**spec-shape** failure class — `extract_miss`, `transform_type`,
`contract_violation`, `bind_type`, `when_type`, `history_type`, `alert_bind`,
`llm_output` — and `repair_policy.l1` is true. Never for transport classes
(`fetch_failed`, `redirect refused`, `secret_*`, `llm_requires_local`): those are
L0's domain (backoff) or the user's (credentials).

- **One attempt per failure streak**: sealed spec field `_l1_last_attempt`
  (system-written, like `_c2_ok`); an attempt is allowed only when it predates
  the current `first_failure_at`. `_l1_last_attempt` is KEPT across a revert
  (§14 trial-failure), a manual `_c2_ok` reset, and even a `_l1_trial` clear —
  the streak marker itself (`first_failure_at`) resets on success/update/commission,
  and that reset is what lets the next streak get a fresh attempt (D1 audit
  2026-09-09). Requires a local `ni` route + `local_available()` (else silently
  waits — the streak persists, so the attempt fires on a later pass). Also
  requires the item's `contract` to be captured (non-null): with no contract
  there is nothing to repair against, so `_maybe_repair_l1` bails silently and
  the streak continues toward `broken` (D7 audit 2026-09-09).
- **Sealed system-only keys** (`_l1_trial`, `_l1_last_attempt`): `validate_spec`
  shape-checks them so an agent-authored spec cannot smuggle arbitrary shapes
  through the sealed body — `_l1_trial` must be `{"rev_before": int>=1}` and
  `_l1_last_attempt` must be an ISO-8601 string (D1 audit 2026-09-09).
- **`update_spec` semantics for trial markers**: any user/agent update strips
  `_l1_trial` (a user edit supersedes any in-flight trial — the trial is void;
  carrying it forward would let the NEXT failure's revert restore a PRE-UPDATE
  revision, silently undoing the user's edit and — worse — re-instating the old
  source URL + old `_c2_ok` + old `contract`, bypassing re-consent). D1 audit
  2026-09-09. `_l1_last_attempt` stays (see above).
- **Model input** (fenced + neutralized, NO tools): the item's `goal`, the
  current `extract`/`transform` stages, the failure class + detail, the captured
  `contract`, and a ≤ 4 KB excerpt of the failing run's RAW payload. All four
  fenced blocks (stages / contract / raw payload) ride the same
  heading-forgery + triple-backtick neutralizer as §13 (D8 audit 2026-09-09) —
  contract keys are payload-derived, so a fetched string could otherwise
  forge a `### ...` section boundary or close the surrounding fence through
  the sealed contract.
- **Model output — the entire repair surface**: a JSON object with optional
  `"extract"` (full replacement paths map, §4.1 grammar-validated) and/or
  `"transform"` (full replacement apply-list, closed ops only). Nothing else:
  never `source`, `url`, `headers`, `schedule`, `scene`, `llm.instruction`,
  `alerts`, `params`. Strict parse + full spec re-validation; a reply that
  touches anything else, fails validation, or isn't JSON = repair attempt failed
  (recorded, no spec change).
- **TOCTOU guard on apply** (D3 audit 2026-09-09): `apply_repair(item_id,
  candidate, expected_rev=...)` reads `spec_rev` at the START of the attempt
  and re-checks under `_SPEC_LOCK` at write time; a mismatch (a concurrent
  user update landed while the multi-second model call was in flight) aborts
  the apply and records `repair_failed` with error `spec_changed`. The trial
  is NOT stamped, so the (already-superseded) failure ladder keeps its own
  bookkeeping intact.
- **Engine discipline for repair fires** (D5 audit 2026-09-09): under the tick
  path, a repair attempt is subject to the same local-model discipline as an
  llm-stage item — the scheduler breaker must be closed, the pass's shared
  llm slot must be free, and at most ONE repair may fire per tick. A fired
  repair consumes the pass's llm slot; two failing items in the same pass
  therefore see only the first repair (the second stays failing and picks up
  the next pass with a free slot). Manual `POST /api/ni/items/{id}/run`
  bypasses this discipline — it is user-invoked and singular; the user's
  click is the consent that a single local model call is welcome.
- **Trial semantics** (improvements.py discipline): the repaired stages are
  applied as a revision with origin `repair_l1` — via a dedicated store path that
  bumps `spec_rev` and records the revision but **keeps `contract`, `_c2_ok`,
  state, and failure counters** (the contract IS the repair target; a repair
  must NOT trigger re-consent — it cannot touch consent-bearing fields by
  construction). The next engine run is the trial: success **and** contract
  satisfied ⇒ repair sticks, counters clear, a carrier notice posts
  ("<title> repaired itself — data mapping updated."); failure ⇒ automatic
  revert to the pre-repair revision, `_l1_last_attempt` stands (no second
  attempt this streak), the ladder continues toward `broken`. The revert
  path holds `_SPEC_LOCK` and strips `_l1_trial` INLINE — never re-entering
  a lock-taking helper — so a pruned/malformed `rev_before` or a corrupt
  sealed revision records `repair_reverted` with a diagnostic `error`
  (`bad_rev_before` / `missing_prior` / `revert_unavailable`) and clears the
  marker rather than wedging the scheduler (D2 audit 2026-09-09).
- Repair attempts and their outcomes write `ni_runs` rows (status
  `repair_applied` / `repair_reverted` / `repair_failed`) — visible in the item's
  run history. Everything is auditable: the revision spine holds the before/after.

## 15. `http_page` and `internal.kb` sources (v2c)

`http_page`:
```json
{"type": "http_page", "url": "https://example.com/status",
 "headers": {"X-Api-Key": {"$secret": "ni:<item_id>:api_key"}}}
```
- URL/param/header/credential/redirect rules are IDENTICAL to `http_json` (§3
  — frozen scheme+authority, percent-encoded params, host-bound https-only
  secrets, `allow_redirects=False` whenever any header rides).
- Fetch via netguard with text/HTML content types, 2 MB cap. The body is then
  **extracted in a subprocess jail** (§16) — HTML parsers are historically
  vulnerable and this is the one place we parse hostile markup. Pipeline
  payload: `{"text": str, "title": str}` (text ≤ 200 KB post-extraction).

`internal.kb`:
```json
{"type": "internal.kb", "query": "quarterly spending", "limit": 5}
```
- Zero egress: runs the knowledge-base hybrid search on the user's own library.
  `limit` clamped 1–10. Payload: `{"results": [{"title", "snippet", "doc_id"}]}`
  (snippets ≤ 500 chars each). Requires the unlocked KB; locked ⇒ the engine
  isn't running anyway. Imported-vault content keeps its third-party status —
  the pipeline treats every source's bytes as untrusted, this one included.

## 16. Subprocess jail (v2c)

For parse steps over hostile input (today: `http_page` extraction). Contract,
modeled on claudecli.py's process hygiene:

- Child = `sys.executable -c` entry importing only the extractor; **stripped
  environment** (no `SMARTBRAIN_*`, no `ANTHROPIC_*`, minimal PATH — the
  claudecli `_cli_env` discipline), private cwd, `start_new_session=True`.
- Input over stdin (bytes), output = one JSON object on stdout (size-capped);
  stderr merged and discarded except for the failure class.
- Resource limits in the child via `resource.setrlimit` (CPU seconds, address
  space, no core files) — platform-guarded (the `resource` module is
  POSIX-only; on Windows rely on the watchdog alone, and note it).
- Watchdog timer kills the whole process group (`os.killpg`) at the deadline;
  always reaped. Timeout/crash/malformed output ⇒ run failure class
  `extract_jail` — never an exception past the engine's bookkeeping.

## 17. Notices endpoint + tray notifications (v2c)

- `GET /api/ni/notices?limit=N` — **desktop-local** (`x-sb-local`), unlocked
  only (423 otherwise). Returns the newest NI carrier-row entries
  `[{id, kind: "alert"|"broken"|"repaired", body, ts}]`, newest first, limit
  clamped ≤ 20. Bodies are sanitized at WRITE time: alert messages by the §12
  interpolation guard, and broken/repaired notices by the same newline-collapse
  + leading-`#` quote applied to the embedded item TITLE when the carrier row is
  posted (titles are user/agent-authored spec text and these bodies render
  inside chat's `###`-delimited notice wrapper).
- The native launcher polls the endpoint unconditionally every 60s; a 423
  (locked), connection failure, or any non-200 reads as "skip" — which covers
  healthy+unlocked in one probe. It surfaces NEW
  entries via the existing `stack.Notify` (macOS/Linux; Windows has no Notify
  implementation yet — documented gap). De-dup by highest-seen id in memory
  (the `lastNotifiedVersion` idiom); at most 3 notifications per poll, extras
  collapse to "…and N more on your Neural Interface."
- A locked vault produces no notifications at all (the endpoint 423s) — tray
  notices never leak sealed content past the unlock boundary.

## 18. Source catalog (v2c: bundled seed; remote pack rides Phase 3 trust machinery)

The curated ground for "AI suggests, user picks" (creation-flow law, §9).

- v2c ships a **bundled** catalog: `app/smartbrain_3000/data/ni_catalog.json`,
  in-repo (reviewed = trusted), loaded read-only at import. Shape:
  `{"version": 1, "sources": [{"id", "title", "host", "url_template",
  "docs_url", "auth": "none"|"key", "category", "notes"}]}` — every
  `url_template` must pass the §3 URL-shape rules (literal https authority;
  `{{param:...}}` only in path/query) and is validated by a test against the
  real validator.
- New OBSERVE tool `list_ni_catalog(category?)` — read-only, no egress —
  returning the entries so the drafting agent suggests from vetted ground
  first; live web research remains the labeled fallback. Suggested-source
  provenance in chat: catalog entries are described as "from SmartBrain's
  vetted catalog"; researched ones as "found via web search".
- The remote signed catalog PACK (seq, Ed25519, pinned key, update checks) is
  deliberately deferred to Phase 3 — it is the same trust machinery as the
  template Library and ships once, together.

## 19. Template pack format (v3 — the Global Library container)

A **template** is an item spec with its slots empty; a **pack** is a signed,
versioned collection of templates. The trust machinery is the vault subscription
model reused: Ed25519 over canonical JSON, fingerprint display law, TOFU pin,
monotonic seq, rollback refusal, KeyChanged blocking.

Envelope (canonical JSON per vault_format's `canonical()` — sorted keys, no
floats, duplicate-key rejection; signed over `b"sb-ni-pack-sig:v1\n" +
canonical(payload)`):

```json
{"sb_ni_pack": {
   "version": 1,
   "pack_id": "<uuid, stable for the pack's lifetime>",
   "seq": 3,
   "published_at": "2026-09-09",
   "publisher": {"label": "SmartBrain project", "pubkey": "<b64 Ed25519>"},
   "templates": [{
      "id": "<slug, unique in pack>",
      "title": "…", "goal": "…", "category": "…", "tags": ["…"],
      "spec_template": { <§2 spec: params present with kind+label but EMPTY
                          values; secret params carry the "ni:self:<name>"
                          placeholder; contract null; repair fields absent;
                          repair_policy absent (installer's local choice —
                          Phase 4b D2c audit 2026-09-11); no credentials,
                          no personal data> },
      "preview_payload": { <bound scene, dummy data — §5-valid> },
      "notes": "one honest sentence"
   }]
 },
 "sig": {"alg": "ed25519", "value": "<b64>"}}
```

- Verification order on every fetch (vault_sync §5 discipline): shape/bounds
  guards → `pack_id` matches the pin → signature against the PINNED key over the
  exact served bytes → `seq >` pinned = update, `==` up-to-date, `<` = rollback
  refusal. A different key = KeyChanged: reported, never applied, source blocked
  until the user re-trusts with the exact offered key (passphrase re-auth,
  vault precedent).
- Bounds: ≤ 200 templates/pack, pack ≤ 2 MB, every `spec_template` passes the
  FULL §2 validator (with empty param values allowed) and every
  `preview_payload` the §5 bound-scene validator AT INSTALL and AT PACK LOAD —
  the signature is never a validator bypass (P5).
- The publisher's label is decoration; the FINGERPRINT (`SB-XXXX-…`, same
  derivation as vaults) is the identity shown in trust UI. First contact PINS
  the pack's ``publisher.pubkey`` (TOFU, vault-manifest precedent); every later
  fetch is verified against that pin — the pubkey a future pack CLAIMS is
  never consulted for the pin.

## 20. Library source, install, and fleet healing (v3)

- **One library source in v1** (the official), stored like a vault subscription:
  sealed source record `{url, publisher_pubkey, pack_id, seq, added_at,
  last_checked, blocked}` — TOFU-pinned on connect. Connect/disconnect are
  desktop-local + explicit UI acts (feeds law). The official URL is prefilled,
  never hardcoded-trusted: the pin still happens on first fetch.
- **Install** (UI act, not chat): pick a template → the install sheet shows
  title, goal, EVERY source host+path unmissably, required params → user fills
  params (secrets via the credential path, never chat) → item created in
  `draft` with sealed provenance `_template = {pack_id, template_id, seq,
  spec_hash}` → the user's explicit **Activate** (commission) is the consent
  event, same as any draft. Installing never auto-runs anything. `repair_policy`
  is FORCED at install time to the safe default `{l1:true, l2_frontier:false}`
  regardless of what a template ships (Phase 4b D2c audit 2026-09-11 — L2
  opt-in is the installer's local `/repair-policy` act, never a pack setting).
- **Fleet healing**: when a pack update changes a template (spec_hash differs),
  every item carrying that `_template` provenance shows "Template update
  available" on its card. Applying is per-item and user-driven: the diff is
  shown (source changes highlighted), acceptance re-enters
  `draft → commissioning` (a template update is NEVER silently applied and
  NEVER inherits standing consent — the admin fixed the class; each user still
  approves their instance). Param values and credentials carry over; `_c2_ok`,
  contract, and trial markers reset (§6/§14 laws).
- **Update checking**: piggybacks the engine tick at low frequency (24h default,
  1h floor, vault_sync cadence discipline; manual "Check now" in the UI).
  Library check failures are host-free statuses; dead-host escalation as vaults.
- **Rollback vs. unreachable** (M3 audit 2026-09-09): a validly-signed OLDER pack
  is NOT unreachable — the host answered. The source's `last_checked` still
  advances (stopping the 30s refetch loop) and the status reads "host is serving
  an older pack (v{remote} < pinned v{pinned})"; the consecutive-failure counter
  and the `unreachable` escalation stay UNTOUCHED. `KeyChanged` blocks the same
  way and does not count toward unreachable either.
- **State-transition notice** (LOW#5 audit 2026-09-09): the first tick to
  transition to `blocked` (KeyChanged) or rollback (older pack) posts ONE carrier
  notice on the Neural Interface feed ("Library updates are blocked — open
  Neural Interface → Library."). Subsequent ticks under the same state post
  nothing — the pin persists the last state so restarts don't re-fire either.

## 21. Export-as-template + submission (v3)

- `GET /api/ni/items/{id}/export-template` (desktop-local): returns the §19
  template JSON for the item — sanitizer strips: secret VALUES (secret params
  reset to the `ni:self:` placeholder), string/number param values (emptied,
  label kept), `contract`, `_c2_ok`, `_l1_*`, `_l2_*` (Phase 4b D2c audit
  2026-09-11 — `_l2_proposal` / `_l2_last_attempt` are per-item engine state,
  never a template's business), `repair_policy` (installer's local choice; the
  install path forces the safe default `{l1:true, l2_frontier:false}` and the
  template ships with none), `_template`, and (H2 audit 2026-09-09) rewrites
  this item's `ni:<item_id>:<name>` refs (header `$secret` values and secret
  param values) back to `ni:self:<name>` so the emitted template installs
  cleanly for the next subscriber and never carries the original item's UUID.
  The exporter REFUSES:
  - `internal.schedule` sources — machine-local schedule id is meaningless
    elsewhere;
  - `internal.kb` sources (M6 audit 2026-09-09) — `query` is personal search
    text; recreate as a template manually;
  - any spec whose WHOLE substituted URL (path + query) OR its percent-decoded
    form OR any `llm`-stage instruction contains an entered credential VALUE
    (M5 audit 2026-09-09 — widened from "URL query only" so a credential pasted
    into a path segment or inside a model instruction is caught).
  Note: the credential-substring guard is belt-and-braces; the primary rule is
  `$secret` discipline (credentials ride `{"$secret": "ni:..."}` refs, never
  plain literals). Base64-encoded credentials are OUT OF SCOPE for the guard.
  Items created BEFORE the `preview_data` slot existed (pre-Phase-3) refuse
  export with a clear message naming the cause; recreate them in the current
  app first (LOW#7 audit 2026-09-09).
- Submission is a GitHub PR to the registry repo (file layout mirrors the pack:
  one JSON per template + CI that validates every template with the app's own
  validators). Only the operator (publisher-key holder) merges, signs, and
  publishes the pack to the landing site (vault publish workflow, second key
  `ni:publisher_ed25519` in the publisher volume — separate rotation domain
  from the vault key).

## 22. `mcp_tool` source (v4a — outbound MCP, the named philosophy extension)

SmartBrain's MCP has been inbound-only by design. This section extends it the one
way the operator approved: SmartBrain may CONSUME servers the user explicitly
configures — never ambient `.mcp.json`, never discovery, never tool listings fed
to models. The payoff: databases and the whole MCP ecosystem become card sources
while credentials stay in the USER'S OWN server process (SmartBrain never holds a
DB password).

**Server registry** (NI-scoped v1):
- Sealed store (feeds convention) of user-configured servers:
  `{id, label, transport: "stdio"|"http", command?: str, args?: [str],
  url?: str, enabled}`. CRUD is **desktop-local UI only** — no agent tool creates
  or edits servers (the feeds "explicit UI act" law; a server config is
  execution/connection authority).
- `stdio`: SmartBrain launches the user's command per fetch — stripped
  `SMARTBRAIN_*`/`ANTHROPIC_*` env (credential firewall), fresh process group,
  watchdog kill + reap (claudecli hygiene), never persistent in v1.
- `http`: connects to the URL the user typed. **This path deliberately does NOT
  ride netguard** — the entire point is the user's own loopback/LAN server, which
  netguard categorically (and rightly) blocks for anonymous fetches. The
  consent-scoped exception lives HERE ONLY: the address was typed by the user in
  a desktop-local act, is frozen thereafter, and nothing model-authored can ever
  reach this code path with a different address. Redirects are refused by
  constructing our own `httpx.AsyncClient(follow_redirects=False)` and passing it
  in (the mcp package's default client hardcodes `follow_redirects=True`), so a
  3xx surfaces as a plain transport error → `mcp_unavailable` before any request
  re-issues to a rewritten host. Bounded timeouts + progressive 200 KB result cap
  as below.

**Result posture** (no raw byte-count read guard):
- SmartBrain does NOT try to bound the raw MCP framing before parsing — the wall-
  clock timeout is the containment. A hostile server is the user's own configured
  code; making that assumption explicit is the point of the consent-scoped
  exception above.
- Per-item text cap: EACH `content` text item is truncated at 200 KB standalone
  before joining, and the running joined size is tracked as we iterate so the
  walk stops the moment the joined text hits 200 KB. A pathological many-item
  result can't grow the pipeline past the payload ceiling.

**Source spec**:
```json
{"type": "mcp_tool", "server_id": "<registry id>", "tool": "query",
 "arguments": {"sql": "SELECT count(*) FROM orders"}}
```
- `tool` + `arguments` are FROZEN literal JSON (no `{{param:}}` in v1); any
  change is a source change → re-consent. The create/install consent surface
  shows `MCP: <server label> → <tool>` plus the full frozen arguments,
  unmissably (the frozen-statement law from the design's db_query discussion:
  the string the user approved is the only thing that ever runs).
- Execution: one `tools/call` per engine run (MCP client from the already-shipped
  `mcp` package — client code only, no server ambient config). Result content
  items of type text are joined; if the joined text parses as JSON, the payload
  is `{"data": <parsed>, "text": <raw ≤200KB>}`, else `{"text": ...}` —
  deterministic either way. Errors map to host-free classes (`mcp_unavailable`,
  `mcp_tool_error`, `mcp_timeout`).
- Injection rules: server tool DESCRIPTIONS are never fetched into any prompt or
  spec; the user picks the tool by NAME. Results are untrusted data — every
  existing containment (P1-P7, output-channel law) applies unchanged.
- Engine discipline: 20s call deadline inside the pass budget accounting;
  per-run connect/spawn + teardown; failures ride the normal health ladder.

## 23. L2 frontier repair (v4b — park-only, the ladder's last rung)

When L1 has exhausted its one attempt in a failure streak (attempted and
failed/reverted) and the item still fails, a FRONTIER model may propose a fix —
under the strictest consent in the system:

- **Gates (ALL required)**: `repair_policy.l2_frontier` true on the item
  (per-item opt-in, default false; settable only via the tool chokepoint's
  approval card OR the desktop-local `POST /api/ni/items/{id}/repair-policy` —
  a template pack MAY NOT ship `repair_policy` and the export sanitizer strips
  it, so repair policy is ALWAYS the installer's local choice, Phase 4b D2c
  audit 2026-09-11); the Claude Code provider connected (its own serve-time
  consent gate — a 403 skips silently); L1 EXHAUSTED this streak — either
  `repair_policy.l1` is false (L1 will never fire, so exhausted by definition;
  Phase 4b D6 audit 2026-09-11) OR `_l1_last_attempt` predates the streak
  marker `first_failure_at`. Honest limitation: with `l1=true` and no local
  model available, L2 stays gated until L1 gets a turn (the fix is to disable
  L1 explicitly, not to widen the gate); one L2 attempt per streak
  (`_l2_last_attempt`, same predating rule as L1).
- **What is sent** (same envelope as L1, claudecode containment applies): goal,
  current extract/transform stages, real failure class + detail, the contract,
  and the ≤4KB fenced+neutralized raw-payload excerpt — all four read from the
  sealed `last_failure` snapshot the run-failure path seals for spec-shape
  failure classes (Phase 4b D5 audit 2026-09-11; absent slot degrades to
  class-only, matching the prior empty-string behavior). NEVER vault/KB content,
  never secret values, never headers/URLs beyond what the spec's own stages
  contain (they contain none).
- **What comes back**: the L1 closed schema — `{extract?, transform?}` only,
  full candidate re-validation. Anything else = attempt failed, recorded.
- **PARK-ONLY**: a valid proposal is NEVER applied. It is stored as sealed
  `_l2_proposal = {stages, created_at, model}` on the item, surfaced on the /ni
  card as "Fix proposed — review" with a stage-level diff and Apply / Dismiss,
  plus a carrier notice ("<title>: a proposed fix is ready to review."). Apply
  = the SAME trial discipline as L1 (revision origin `repair_l2`, contract as
  the pass bar, auto-revert on the next failure); Dismiss clears the proposal
  (no re-propose this streak). Apply REFUSES 409 when the item is `broken` —
  the fix path is edit → re-commission; a stale proposal is dropped on the
  broken hop (Phase 4b D4 audit 2026-09-11). A user spec edit voids a pending
  proposal (the §14 D1 law); a clean run resets the streak AND voids the
  proposal (Phase 4b D8 audit 2026-09-11 — a resolved item would otherwise
  keep the "Fix proposed" chip forever); a failed trial's revert now carries
  `_l2_last_attempt` onto the restored spec (Phase 4b D1 audit 2026-09-11 —
  the one-attempt-per-streak marker survived the revert path, closing an
  unbounded per-Apply re-fire).
- **Engine hygiene**: the frontier call takes minutes (claudecode floor 300s) —
  it must NEVER run inside the tick pass. The tick only marks eligibility; the
  call runs on a single bounded daemon worker (one in flight process-wide,
  agent_routes._spawn precedent), which writes the proposal when done. App
  shutdown abandons it harmlessly (proposal generation is idempotent per
  streak).
- Every attempt/outcome is an `ni_runs` row (`repair_l2_proposed` /
  `repair_l2_failed`); Apply/Dismiss are audited.
