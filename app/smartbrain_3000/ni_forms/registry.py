"""The form registry: FORMS == types.FORMS (every form the corpus needs)."""
from __future__ import annotations

import importlib

from .types import FORMS as FORM_NAMES

FORMS: dict = {}
_PKG = "smartbrain_3000.ni_forms.catalog"
for _n in sorted(FORM_NAMES):
    try:
        _m = importlib.import_module(f"{_PKG}.{_n}")
    except ModuleNotFoundError as ex:          # pragma: no cover - development only
        if ex.name != f"{_PKG}.{_n}":
            raise
        continue
    FORMS[_n] = _m.FORM


def check_registry() -> list[str]:
    """Every form declares PLANS for every variant x all 18 span classes."""
    from .spans import ALL_SCLASSES
    from .types import REJECT_CODES
    errs = []
    missing = FORM_NAMES - set(FORMS)
    if missing:
        errs.append(f"missing forms {sorted(missing)}")
    for n, f in FORMS.items():
        if f.name != n:
            errs.append(f"{n}: name {f.name}")
        for v in f.variants:
            plans = f.PLANS.get(v)
            if plans is None or set(plans) != set(ALL_SCLASSES):
                errs.append(f"{n}.{v}: PLANS must cover all 18 span classes")
                continue
            for sc, p in plans.items():
                if p is None:
                    code = f.REJECTS.get(v, {}).get(sc, "too_narrow")
                    if code not in REJECT_CODES:
                        errs.append(f"{n}.{v}.{sc}: reject code {code}")
        if f.record_form and not {"empty", "one", "few", "many"} <= set(f.states):
            errs.append(f"{n}: record form states")
    return errs
