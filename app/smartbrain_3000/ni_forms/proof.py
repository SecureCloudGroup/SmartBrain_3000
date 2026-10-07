"""Standalone proof page: `python -m smartbrain_3000.ni_forms.proof OUT.html [--now ISO]`.

Builds the proto's 7 live-card records from the vendored test fixtures (recs.py),
runs profile -> enumerate -> present (call=None, rules floor) -> layout at the
default desktop span and the phone span, and writes ONE standalone HTML page via
paint.vector.page() with both themes and all cards (desktop row then phone row).

Nothing in this module runs on import; the CLI entry point is `python -m ...`.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path


def _render(now_iso: str | None) -> str:
    """Build the 7 cards and return the standalone HTML."""
    assert isinstance(now_iso, (str, type(None))), "now_iso must be str or None"
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tests import _ni_forms_recs as recs  # vendored test builders

    from .enumerate import enumerate as enumerate_cands
    from .layout import layout_span
    from .paint import vector as VC
    from .present import present
    from .spans import Span

    now = datetime.fromisoformat(now_iso) if now_iso else recs.NOW
    assert now.tzinfo is not None, "now must be timezone-aware"
    cards: list[dict] = []
    for fx in recs.LIVE:                              # 7 live cards in declared order
        rec, prof, inp = recs.get(fx)
        cands = enumerate_cands(rec, prof, inp, now)
        res = present(cands, rec, prof, inp, call=None)
        cand = next(c for c in cands if c.id == res.used)
        desk = Span.parse(cand.default_span)
        html = VC.card_html(layout_span(cand, rec, prof, inp, desk, now).clir,
                            width=_bucket_min(desk), theme="auto", now=now,
                            viewer_tz=inp.viewer_tz)
        cards.append({"html": html, "caption": f"{fx} · {cand.form} · {desk.key}"})
    for fx in recs.LIVE:
        rec, prof, inp = recs.get(fx)
        cands = enumerate_cands(rec, prof, inp, now)
        res = present(cands, rec, prof, inp, call=None)
        cand = next(c for c in cands if c.id == res.used)
        if not cand.phone_span:
            continue
        phone = Span.parse(cand.phone_span)
        html = VC.card_html(layout_span(cand, rec, prof, inp, phone, now).clir,
                            width=_bucket_min(phone), theme="auto", now=now,
                            viewer_tz=inp.viewer_tz)
        cards.append({"html": html, "caption": f"{fx} · {cand.form} · {phone.key}"})
    return VC.page(cards, title="NI port proof", now=now, clock_toggle=False)


def _bucket_min(span) -> int:
    """Return the floor width for a span (shortcut around spans.bucket)."""
    from .spans import bucket
    return bucket(span).min_w


def main(argv: list[str]) -> int:
    """CLI entry: proof OUT.html [--now ISO]."""
    assert isinstance(argv, list), "argv must be a list"
    args = [a for a in argv if not a.startswith("--")]
    assert args, "usage: python -m smartbrain_3000.ni_forms.proof OUT.html [--now ISO]"
    now_iso = next((a.split("=", 1)[1] for a in argv if a.startswith("--now=")), None)
    out = Path(args[0]).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_render(now_iso), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
