"""`cand_for(form, rec, prof, i=None)` extracted from the proto's dev_sheet.py so the
suite does not import paint_debug/PIL. Returns a stub Candidate (no plans/scores)
for the i-th `match()` result; the test code recomputes plans via validate_spans."""
from __future__ import annotations

from smartbrain_3000.ni_forms.registry import FORMS
from smartbrain_3000.ni_forms.types import Candidate


def cand_for(form: str, rec, prof, i: int | None = None):
    """Build a minimal Candidate for the i-th match of `form` on (rec, prof)."""
    assert isinstance(form, str), "form must be a str"
    assert rec is not None, "rec must not be None"
    i = 0 if i is None else int(i)
    ms = FORMS[form].match(rec, prof)
    if not ms:
        return None
    m = ms[min(i, len(ms) - 1)]
    return Candidate(id="c0", form=form, variant=m["variant"], bindings=m["bindings"], params=m["params"],
                     describes="", covers=[], intent="now", plans={}, rejected_spans={}, default_span="d1x1",
                     phone_span=None, floor_score=0)
