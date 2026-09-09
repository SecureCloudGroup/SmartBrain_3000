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
  (host-free), `consecutive_failures INTEGER`, `position INTEGER`, `spec_rev
  INTEGER`, `created_at TIMESTAMP`, `updated_at TIMESTAMP`; sealed body (AAD
  `ni_item:<id>`): the spec (§2).
- `ni_snapshots` — `item_id TEXT`, `slot TEXT` (`latest` | `last_good` | `preview`),
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

## 3. Sources (v1: three types, closed set)

`http_json`:
```json
{"type": "http_json", "url": "https://api.example.com/quote?sym={{param:symbol}}",
 "headers": {"X-Api-Key": {"$secret": "ni:<item_id>:api_key"}}}
```
- Fetched via `netguard.safe_fetch_json` only. `validate_public_url` runs at every
  create/update. The URL (with params substituted) is frozen by user consent; no
  model output may ever alter `url` or `headers`.
- `$secret` values resolve at fetch time by the engine; the resolved secret is
  host-bound: it attaches only when the request host equals the host recorded when
  the credential was entered. Any other host refuses the fetch outright.
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

Rules:
- **Text is text.** Bound strings render as plain text — no markdown, no HTML, no
  links. (Injection point P3: a lying string can render, but it cannot become UI.)
- Tones map to design tokens only; no raw colors anywhere in the grammar.
- Caps: ≤ 100 nodes after repeat expansion, depth ≤ 8, text ≤ 2000 chars, repeat
  `max` ≤ 50. Enforced at bind time and again by the renderer.
- Reserved for later phases (validators must REJECT in v1, so old apps refuse new
  scenes rather than mis-render them): `spark`, `gauge`, `image`, `when`
  (conditions), `on_tap` (behaviors).

## 6. Lifecycle state machine (fully enumerated)

States (plaintext `ni_items.state`):
`draft → commissioning → live ⇄ degraded ⇄ failing → broken`, plus `paused`
(user, from any post-draft state; resume returns to `commissioning` if the item
was never live, else `live`).

- `draft` — spec + `preview` snapshot (dummy data, agent-invented, validated
  against the same grammar). Never fetches. Never expires in v1.
- `commissioning` — entered when the user approves the create/activation card.
  - **C1 (immediate)**: one real run of the full pipeline. Failure → stays
    `commissioning`, error surfaced for redrafting.
  - **C2 (user)**: the real result renders on /ni with "Looks right" /
    "Something's wrong". Wrong → back to draft with the user's note.
  - **C3 (automatic)**: the next engine-cadence run must pass and satisfy the
    contract captured at C1/C2. Pass → `live`.
- `live` — engine runs at cadence; every run contract-checked.
- `degraded` — latest run failed or violated contract; `last_good` renders dimmed.
- `failing` — ≥ 3 consecutive failures; effective interval doubles per failure
  beyond 3 (cap 24h).
- `broken` — 8 consecutive failures over ≥ 7 days (vault_sync escalation rule), or
  a permanent refusal (e.g. credential host mismatch). Engine stops scheduling it;
  carrier-row alert fires; only user/agent action (edit → re-commission) leaves it.

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

`ni.tick(app, pass_budget_seconds)` called from `scheduler.tick` after the feeds
pass, feeds contract verbatim: locked → return; own cursor; due =
`enabled AND state NOT IN ('draft','paused','broken') AND (last_checked IS NULL OR
age > effective_interval)`, `NULLS FIRST`, oldest first; `_MAX_ITEMS_PER_PASS = 3`;
per-item try/except; wall-clock budget between items; `mark_checked` on every
attempt; host-free `last_status`. `model` sources additionally require
`gateway.local_available()` when the route is local, and respect the scheduler
breaker.

## 9. Tools and tiers (v1)

| tool | tier | notes |
|---|---|---|
| `list_ni_items` | OBSERVE | plaintext state + titles |
| `read_ni_item` | OBSERVE | spec + health + latest bound payload (untrusted-data provenance line first, KB-tool precedent) |
| `create_ni_item` | REVIEWED, egress | full spec + preview payload; validates everything; lands in `draft` |
| `update_ni_item` | REVIEWED | partial; source/url/header changes force state back to `draft` (re-consent) |
| `set_ni_item_enabled` | REVIEWED | pause/resume |
| `run_ni_item_now` | REVIEWED, egress | clears `last_checked` |
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
- `POST /api/ni/items/{id}/run` — manual refresh (engine path, same guards).
- `PATCH /api/ni/items/{id}` — position/display/enabled (UI edits).
- `DELETE /api/ni/items/{id}`.
- `PUT  /api/ni/items/{id}/credential` — enter a secret param value
  (desktop-local, body `{name, value, host}`); stores under `ni:<id>:<name>`,
  host-bound. Secrets never travel through chat or tool args.
- All handlers 423 → the standard locked contract; literal paths before `{id}`
  (schedule_routes ordering comment).
