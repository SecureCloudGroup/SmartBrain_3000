# NI cases

The case taxonomy that used to live here is replaced by the gold set.

- The asks, their constraints, acceptable sources and human verdicts: `app/tests/ni_eval/gold/` (`gold.json` for
  tuning, `ci.json` for the offline CI cassette).
- How the set is built, scored and gated, and how to record and replay it: `docs/internal/ni-eval.md`.
- The engine-state registry the flow tests and `tools/ni-flow-eval.py` read: `app/tests/fixtures/ni_flow/cases.json`.
  An honest refusal (`failed`, `source`) is a passing outcome for a by-design row, so the safety edges stay
  regression-tested.
- The lifecycle matrix: `app/tests/test_ni_lifecycle.py`.

Fix rounds add a class to the gold, never a named example.
