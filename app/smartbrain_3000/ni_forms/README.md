# ni_forms

NI card presentation engine, ported from the P0 prototype (Phase 1a-1).

The server computes `record -> profile -> enumerate -> present -> layout -> lint -> CLIR`;
the client paints the CLIR later (not part of this package). The Python vector painter is
kept for galleries and docshots; the raster painter was not ported.

## What the product wiring (Phase 1a-2) needs

- The record adapter must build a `DataRecord` with:
  - `Field.name`, `Field.path` (unique), `Field.type` (one of `types.FIELD_TYPES`),
    `Field.role` (one of `types.ROLES`), `Field.unit`, `Field.currency`,
    `Field.precision`, `Field.scale`, `Field.wallclock`, `Field.derived`,
    `Field.unrounded`, `Field.label_src` where meaningful (see `types.Field`).
  - `Context.fetched_at` (ISO 8601 Z), `Context.as_of`, `Context.source_host`,
    `Context.card_tz` (IANA), `Context.card_tz_src`.
  - `CardInput.ask`, `.title`, `.source_url`, `.source_kind`, `.source_format`,
    `.fetched_at`, `.cadence_s`, `.viewer_tz` (default `America/New_York`), `.c2_answers`.
  - `check_record()` validates the shape before layout.
- The llm shim's transport contract is `call(messages) -> str`. The product passes a
  function that posts `messages` to the SB gateway and returns the completion text:
  `obj, meta = llm.chat_json("present", msgs, schema, call=my_call, max_tokens=500)`.
- To get the CLIR for a record at a given span:
  ```python
  from smartbrain_3000.ni_forms import profile as pf
  from smartbrain_3000.ni_forms.enumerate import enumerate as enumerate_cands
  from smartbrain_3000.ni_forms.layout import layout_span
  from smartbrain_3000.ni_forms.spans import Span

  prof = pf.profile(record, card_input, now)
  cands = enumerate_cands(record, prof, card_input, now)
  # pick a candidate (via PRESENT, with or without an injected `call`):
  from smartbrain_3000.ni_forms.present import present
  res = present(cands, record, prof, card_input, call=None)   # or call=gateway_call
  cand = next(c for c in cands if c.id == res.used)
  out = layout_span(cand, record, prof, card_input, Span.parse(cand.default_span), now)
  clir = out.clir       # out.hash is the deterministic CLIR hash
  ```

## Defects carried from the proto

Fixed in the port:
- `TIME_FMTS` was missing `MMM yyyy` and `MMM d, yyyy`; `fmt.coarse_fmt` emits them and
  `check_clir` would otherwise reject them. Added to `types.TIME_FMTS`.
- `WANTS_ALL` was stale (15 of 21 ids); the authoritative list is now in
  `present.WANTS_ALL`. It is wider than the on-disk `assets/lexicon/wants.json`.

Added in this port (2026-10-06, second pass):
- `clir_budget` AMBER lint: `layout_span` canonicalises the CLIR with the same
  rounding used by `clir_hash`; a blob > 16 KB appends an amber `LintIssue`.
  Never red yet. See `test_ni_forms_clir_budget.py`.
- `proof.py`: `python -m smartbrain_3000.ni_forms.proof OUT.html [--now=ISO]`
  renders the 7 live cards (desktop row then phone row, both themes) through
  the ported `vector.page()` so the port can be judged by eye.
- Hybrid glyph policy (Phase 1a-2, 2026-10-06): the browser paints CLIR text,
  so `glyph_missing` is AMBER (not red). `text.width()` already measures an
  unknown codepoint with the font's .notdef advance (tofu fallback) and
  `text.coverage()` reports which chars missed every face. Red can be
  re-enabled when a server-side PNG painter rejoins the shipping path.
- Product wiring (Phase 1a-2, 2026-10-06; docs/internal/ni-format.md §34):
  `record.py` turns the flow's answers + pipeline outputs into a `DataRecord`
  (`from_answers` at build, types/roles derived once; `from_spec` at every bind,
  the sealed field specs re-read by path) and `form_scene.py` seals the §34 node
  (`form_scene`, `swap_to_second`, `history_track_for`, `display_size_for_span`).
  Three engine adjustments came with the real data shapes: `layout_span` treats
  a record form holding ≤3 rows (naturally, after a refresh shrank it, or under
  a count override) as its designed empty/one/few state (hollow amber, not red —
  the same rule enumerate's forced-state check applies, so build and bind agree);
  the stat's label under the hero is left out when it only repeats the card
  title (`title_echo`), and a lone hero under its title counts as the designed
  calm card; and the pipeline's time/date texts carry their instant
  (`ni._TimeText.moment`, `local_date`) so the record keeps ISO cells.

Investigation of the stat NVDA amber: there isn't one. The port survey had
flagged "regressed one amber lint on the W2 bucket"; a direct probe at every
accepted span of the stat NVDA candidate shows ZERO amber and ZERO red issues
(default_span d1x1, variant number, with plans {d1x1, d2x1, p1x1}). The
fontTools measurer wrapped close enough to HarfBuzz that the 4% margin covered
every string in both buckets.

Noted here, not fixed (behaviour preserved):
- `event_curve` W2 acceptance differs from CONTRACTS.md 5.3's table (the proto's
  table rejects W2 as "sibling_better"; the code accepts it in some rows); carried.
- `stat.extrapolate` variant is declared but never produced by `stat.match()`; carried.
- `stat.count_up` emits no `LiveBinding` for the ticking count; carried.
- `lint` needs `prov_map` passed in; only `layout_span` has it, so an external lint
  caller gets `provenance` as red for every text prim. This is the client contract:
  clients never lint, the server already did.
- `shell(ctx, cv) -> dict` differs from the CONTRACTS.md 5.1 signature
  (`shell(ctx) -> (header+footer prims, body box)`); carried.
- `profile`'s "long titles" branch reads stems of the ask against field-path tails
  (profile.py around the `ask_st` block). Behaviour kept; flagged in the module
  docstring.

## Port manifest, condensed

- `canon`, `spans`, `tokens.py`, `tokens.json`: verbatim from the proto.
- `types.py`: trimmed; `RoleResult`/`RoleQuestion`/`Assignment`/`CardState`/`TickResult`/
  `RawFetch`/`BuildOut`/`Critique` are not re-exported. `TIME_FMTS` widened.
- `text.py`: rewritten to use fontTools alone (hmtx advances + GSUB `tnum` glyph
  substitution). The port survey measured p95 1.37% wider than HarfBuzz; the existing
  4% margin in `fits()`/`break_lines()` absorbs the gap.
- `llm.py`: a thin shim with an injected `call(messages) -> str` transport, an in-house
  validator for the schema subset the proto emitted (enum, required,
  additionalProperties:false, minItems/maxItems, type, anyOf-with-null), one retry on
  invalid output, and the thread-local `no_model()` guard.
- `profile.py`: `pipeline/data/profile.py` + `wants.py` + `lexicon.humanise` + the two
  helpers `timeparse.zone` and `timeparse.parse_iso_z` inlined.
- `forms/`: `base`, `ctx`, `rec`, `fmt`, `prov`, `templates` (minus the `pg_*`
  pipeline/program entries), `shell`, `layout`, `ladder`, `lint`, `enumerate`
  (`MAX_CANDS` 6 -> 4), `events`, `present` (model call becomes `call=`), `registry`
  (dynamic import path updated).
- `catalog/`: all 18 forms imported cleanly, so all are shipped.
- `paint/`: `live.py`, `vector.py`, and a new `paint/shared.py` holding the five
  raster helpers vector uses (`text_style`, `_tri_pts`, `blob_path`, `land`, `icons`).
  Pillow is only imported lazily inside `vector._image_uri`.
- `assets/`: Inter 400/500/600/700, lexicons/few-shots/icons/world110m. The Inter
  SIL OFL license is included in `assets/fonts/LICENSE.txt`.
- `fmt.py`: non-Latin-1 literals `₿` and `₂` replaced with ASCII so the
  bundled Inter subset covers them.
