# FIT labeled set (plan B3, Phase 3a)

172 records: one per live card of the four blind-50 runs (`a01`..`a49` = the
2026-10-07 set, `b01`..`b49` = the 1a-6 fresh set, `c01`..`c50` = the SET C
fresh set, `d01`..`d49` = the SET D fresh set — 86 total; the first 63 built
by `build_fit_fixtures.py` from the already-sealed `tests/fixtures/ni_forms/
live_2026-10-07*` case files, the 23 SET D records by `$SP/build_fit_fixtures_d.py`
from `$SP/asrecorded_d/` — the SET D live cases exactly AS RECORDED pre-fix,
built by `$SP/build_asrecorded_d.py`, since FIT labels what was actually
reviewed, not the datalayer-r2 round's corrected cards) plus 86 synthetic
negatives (`neg00`..`neg62`, `negd00`..`negd22`: a recorded ask's frame paired
with ANOTHER recorded source's chosen answers + preview, 7 slots apart in run
order — never a real match, always labeled `no`).

Each record: `{id, ask, frame: {kind, subject}, chosen, rows_output_name,
preview, missing_menu, label: {answers_ask, missing, wrong}}` — the exact
shape `ni_forms.verdict.fit_verdict` reads, plus the label `tools/ni-fit-eval.py`
scores against.

## Labels

`answers_ask` comes straight from the by-eye review verdict already recorded
for that card: `right -> yes`, `partial(ly) -> partly`, any `WRONG* -> no`.
One live card (`a40`, the ISS map) read "unshippable on the client" — a
client-rendering gap, not an answer-correctness one — labeled `yes` (the
chosen answer IS right; FIT's scope is semantic fit, not presentation). The
`a09` "new moon" row is a REGRESSION that went to links, not live, and is not
one of the 63. The SET D records (`d07`, `d12`, `d18`, `d37`) label the FOUR
WRONG classes datalayer-r2 fixed (W1 snow/temperature, W2 next-high-for-low-
tide, W3 the unfiltered scoreboard, W4 the one-row quake count) `no`, over
the AS-RECORDED pre-fix chosen/preview — FIT measures whether the model
catches what the by-eye review actually caught, not whether the fix works
(that is `test_ni_forms_oracle.py` + the datalayer-r2 probes, over the
corrected cards in `tests/fixtures/ni_forms/live_2026-10-08d/`).

`missing` / `wrong` are best-effort: populated only where the review's prose
plainly names the chosen answer at fault (a cell/answer label appearing in the
note). Most records carry `missing: []`, `wrong: []` even when `answers_ask`
is `no` or `partly` — the review named the CLASS of problem, not always a
specific answer id. This is a known gap, not a bug: `tools/ni-fit-eval.py`'s
primary numbers are precision/recall on `answers_ask`.

`missing_menu` is the per-record closed menu `fit_verdict` validates `missing`
against. Production builds it from `_verify_frame`'s already-computed
`unanswered` components (the taxonomy `expects` the Library already checked)
plus the ask's own want words (plan B3: "taxonomy COMPONENTS ∪ the frame's
wants ids"). This sandbox has no live Library connection (`_resolve_library()`
returns `None` here — confirmed), so these fixtures approximate the menu from
the ask's own content words instead. The approximation only affects `missing`
scoring, not `answers_ask`.

## Overlap-gate exemption

The 86 live-card asks are the SAME asks as `blind50.json` / `blind50b.json` /
`blind50c.json` / `blind50d.json` (the four blind sets this round's
`asks-overlap`-style check would normally flag against every other eval/sealed
set). This is **allowed here, not a contamination bug**: per the task ruling,
all four sets are now regression sets (each already used once, by eye, for
its own round's gate — set D by the datalayer-r2 round), and FIT is explicitly
built FROM their recorded, reviewed outcomes — there is no held-out set this
labels against. Do not reuse the 86 asks (or the 86 by-eye verdicts) to tune
anything upstream of FIT (`select_answers`, `_verify_frame`, the forms
engine); FIT is read-only over their already-sealed output. The 86 synthetic
negatives are NEW records (a cross-pairing of two already-recorded builds)
but are built from the same 86 asks, so they carry the same exemption.
