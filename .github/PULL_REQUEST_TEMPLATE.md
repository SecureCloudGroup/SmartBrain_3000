<!--
By submitting this PR you agree your contribution is licensed under the
project's Elastic License 2.0 (see CONTRIBUTING.md).
-->

## What & why

What does this change, and why?

## How verified

- [ ] `ruff check` clean (zero warnings)
- [ ] `pytest -q` passes
- [ ] `svelte-check` clean and the SPA was rebuilt (if frontend changed)
- [ ] Exercised against the running stack where relevant

## Checklist

- [ ] The diff is surgical — every changed line traces to the stated goal.
- [ ] Follows `CLAUDE.md` (simplicity, NASA P-of-10, no new linter warnings).
- [ ] No secrets, real user data, or operator identity added anywhere.
- [ ] Security-sensitive paths keep the credential firewall intact.
