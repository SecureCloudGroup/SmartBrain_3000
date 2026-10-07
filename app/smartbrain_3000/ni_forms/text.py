"""Text measurement with fontTools advances + GSUB tnum (adapted from the proto).

The proto used uharfbuzz for shape() so kerning and tnum substitutions matched the
browser exactly. The port survey measured the fontTools path at p95 1.37% wider
than HarfBuzz across the corpus, never overflowing: the existing 4% margin in
fits()/break_lines() absorbs the gap. We keep the margin and use fontTools alone
so the package imports with stdlib + fontTools only.

Numeral roles get tnum through GSUB (table 'tnum' feature) substituted glyph ids
before the advance lookup, mirroring the font's own tabular-figure substitution.
The vertical metrics and glyph coverage already came from fontTools in the proto.

Fallback chain and the macOS system fonts are preserved so tests that measure
non-Latin glyphs can locate those faces; measurement falls back to Inter's
advances when a face is missing.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from functools import cache, lru_cache
from pathlib import Path

from fontTools.ttLib import TTFont

ASSETS = Path(__file__).with_name("assets")
FONT_DIR = ASSETS / "fonts"
WEIGHTS = (400, 500, 600, 700)
MARGIN = 0.04

# Fallback chain after Inter. (path, face index). Missing files are skipped.
# Kept to match the proto so coverage() still reports the macOS faces where present.
FALLBACK = [
    ("/System/Library/Fonts/SFNS.ttf", 0),
    ("/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc", 0),
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0),
    ("/System/Library/Fonts/SFArabic.ttf", 0),
    ("/System/Library/Fonts/SFHebrew.ttf", 0),
    ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", 0),
    ("/System/Library/Fonts/Apple Color Emoji.ttc", 0),
]
# CSS stack the vector painter MUST use (same order as above).
CSS_FONT_STACK = ('"NI Inter", "SF Pro Text", -apple-system, system-ui, "Hiragino Sans", '
                  '"Hiragino Sans GB", "SF Arabic", "Geeza Pro", "Arial Unicode MS", '
                  '"Apple Color Emoji", sans-serif')

_STRIP = {0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF} | set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A))


@dataclass(frozen=True)
class _Face:
    path: str
    index: int
    tt: object               # fontTools TTFont (lazy)
    upem: int
    cmap: frozenset
    ascent: float            # fraction of em
    descent: float           # fraction of em (positive)
    cap: float               # cap height fraction of em
    tnum_sub: dict           # digit gid -> tnum gid, when the font ships a 'tnum' GSUB feature


def _tnum_substitution(tt: TTFont) -> dict:
    """Resolve the 'tnum' GSUB single-substitution for digits so measurements use
    tabular-figure advances (matching the painter's tnum feature in the browser)."""
    gsub = tt.get("GSUB")
    if gsub is None or gsub.table is None:
        return {}
    assert gsub.table.FeatureList is not None, "GSUB without FeatureList"
    lookups = []
    for feat_rec in gsub.table.FeatureList.FeatureRecord:
        if feat_rec.FeatureTag != "tnum":
            continue
        lookups.extend(feat_rec.Feature.LookupListIndex)
    if not lookups:
        return {}
    sub: dict = {}
    lu_list = gsub.table.LookupList.Lookup
    for li in lookups:
        for st in lu_list[li].SubTable:
            st = getattr(st, "ExtSubTable", st)  # LookupType 7 wraps the single substitution
            mapping = getattr(st, "mapping", None)
            if mapping:
                sub.update(mapping)
    name_to_gid = tt.getReverseGlyphMap()
    out: dict = {}
    for src, dst in sub.items():
        if src in name_to_gid and dst in name_to_gid:
            out[name_to_gid[src]] = name_to_gid[dst]
    return out


@cache
def _load(path: str, index: int) -> _Face:
    assert path, "empty font path"
    tt = TTFont(path, fontNumber=index, lazy=True)
    cmap = frozenset(tt.getBestCmap() or {})
    head = tt["head"]
    upem = head.unitsPerEm
    hhea = tt["hhea"]
    cap = getattr(tt["OS/2"], "sCapHeight", 0) or int(0.7 * upem)
    return _Face(path, index, tt, upem, cmap,
                 hhea.ascent / upem, -hhea.descent / upem, cap / upem, _tnum_substitution(tt))


def inter(weight: int) -> _Face:
    w = min(WEIGHTS, key=lambda x: abs(x - weight))
    return _load(str(FONT_DIR / f"Inter-{w}.ttf"), 0)


@cache
def _chain(weight: int) -> tuple:
    out = [inter(weight)]
    for p, i in FALLBACK:
        if Path(p).exists():
            try:
                out.append(_load(p, i))
            except Exception:
                # Missing TTC member or unreadable table: skip and keep going (same
                # behaviour as the proto's _chain, which also swallowed load errors).
                continue
    return tuple(out)


def clean(s: str) -> str:
    """Display hygiene: NFC, strip bidi/zero-width/C0/C1 controls."""
    assert isinstance(s, str), "clean takes a str"
    s = unicodedata.normalize("NFC", s)
    return "".join(ch for ch in s if ord(ch) not in _STRIP and
                   not (unicodedata.category(ch) == "Cc" and ch not in "\n\t"))


def _face_for(ch: str, weight: int) -> int:
    cp = ord(ch)
    for i, f in enumerate(_chain(weight)):
        if cp in f.cmap:
            return i
    return 0


def _advance_for(face: _Face, ch: str, tnum: bool) -> float:
    """Advance width in em-fractions for one codepoint. Falls back to the font's
    .notdef glyph (0) advance when the codepoint is absent, mirroring the proto's
    tofu behaviour. tnum swaps digit gids via the loaded 'tnum' lookup."""
    cmap = face.tt.getBestCmap() or {}
    gid_name = cmap.get(ord(ch))
    hmtx = face.tt["hmtx"]
    if gid_name is None:
        # .notdef is always glyph 0 in order; use its advance for tofu width.
        order = face.tt.getGlyphOrder()
        return hmtx.metrics[order[0]][0] / face.upem
    if tnum and face.tnum_sub:
        name_to_gid = face.tt.getReverseGlyphMap()
        src = name_to_gid.get(gid_name)
        if src is not None and src in face.tnum_sub:
            order = face.tt.getGlyphOrder()
            gid_name = order[face.tnum_sub[src]]
    return hmtx.metrics[gid_name][0] / face.upem


@lru_cache(maxsize=65536)
def width(s: str, px: float, weight: int = 400, tnum: bool = False) -> float:
    """Width of `s` in CSS px, summing per-glyph advances across the fallback chain.
    No shaping/kerning: the 4% margin in fits() absorbs the small p95 gap vs HarfBuzz."""
    assert isinstance(s, str), "width takes a str"
    assert px > 0, "px must be > 0"
    if not s:
        return 0.0
    chain = _chain(weight)
    total = 0.0
    for ch in s:
        fi = _face_for(ch, weight)
        f = chain[fi]
        total += _advance_for(f, ch, tnum and fi == 0) * px
    return total


def fits(s: str, px: float, weight: int, max_w: float, tnum: bool = False) -> bool:
    """True when the string fits max_w WITH the 4% safety margin."""
    # The proto accepted negative max_w (callers compute it as `W - other.x1 - 8`
    # and treat a negative result as "no room"); keep that behaviour, don't assert.
    assert isinstance(s, str), "fits takes a str"
    assert isinstance(max_w, (int, float)), "max_w must be numeric"
    if max_w <= 0:
        return not s
    return width(s, px, weight, tnum) * (1 + MARGIN) <= max_w


def ellipsize(s: str, px: float, weight: int, max_w: float, tnum: bool = False) -> tuple[str, bool]:
    """Shorten `s` to a prefix that fits max_w, with a trailing ellipsis character."""
    assert isinstance(s, str), "ellipsize takes a str"
    assert isinstance(max_w, (int, float)), "max_w must be numeric"
    if fits(s, px, weight, max_w, tnum):
        return s, False
    k = 32
    cap = max(len(s), 1)
    while k < cap and fits(s[:k], px, weight, max_w, tnum):
        k *= 2
    if k + 8 < len(s):
        j = s.find(" ", k + 8)
        s = s[:j] if j != -1 else s[:k + 8]
    words = s.split(" ")
    for _ in range(len(words) + 1):
        if len(words) <= 1 or fits(" ".join(words) + "…", px, weight, max_w, tnum):
            break
        words.pop()
    t = " ".join(words)
    for _ in range(len(t) + 1):
        if not t or fits(t + "…", px, weight, max_w, tnum):
            break
        t = t[:-1]
    return (t.rstrip(" ,;:·-") + "…") if t else "…", True


def break_lines(s: str, px: float, weight: int, max_w: float, max_lines: int,
                tnum: bool = False) -> tuple[list[str], bool]:
    """Greedy word wrap (spaces; CJK breaks between any two CJK chars). The last
    allowed line is ellipsized. Returns (lines, truncated)."""
    assert max_lines >= 1, "max_lines must be >= 1"
    assert isinstance(s, str), "break_lines takes a str"
    tokens: list[str] = []
    cur = ""
    for ch in s:
        if ch == " ":
            tokens.append(cur + " ")
            cur = ""
        elif unicodedata.east_asian_width(ch) in ("W", "F"):
            if cur:
                tokens.append(cur)
            tokens.append(ch)
            cur = ""
        else:
            cur += ch
    if cur:
        tokens.append(cur)
    lines: list[str] = []
    line = ""
    i = 0
    cap = len(tokens) + 1
    for _ in range(cap):
        if i >= len(tokens):
            break
        cand = line + tokens[i]
        if fits(cand.rstrip(), px, weight, max_w, tnum) or not line:
            line = cand
            i += 1
            continue
        lines.append(line.rstrip())
        line = ""
        if len(lines) == max_lines:
            break
    rest = "".join(tokens[i:]) if len(lines) == max_lines else ""
    if len(lines) < max_lines and line:
        lines.append(line.rstrip())
        line = ""
    truncated = bool(rest.strip())
    last_overflows = lines and not fits(lines[-1], px, weight, max_w, tnum)
    if truncated or last_overflows:
        lines[-1], _ = ellipsize((lines[-1] + " " + rest).strip() if truncated else lines[-1] + " …",
                                 px, weight, max_w, tnum)
        truncated = True
    return lines, truncated


def metrics(px: float, weight: int = 400) -> dict:
    """Inter vertical metrics in px: ascent, descent, cap (for boxes and baselines)."""
    assert px > 0, "px must be > 0"
    assert weight in (400, 500, 600, 700, 300, 800), "unexpected weight"
    f = inter(weight)
    return {"ascent": f.ascent * px, "descent": f.descent * px, "cap": f.cap * px}


def coverage(s: str, weight: int = 400) -> dict:
    """{'inter': n, 'fallback': {font_file: n}, 'missing': [chars]} for the glyph lint."""
    assert isinstance(s, str), "coverage takes a str"
    assert weight in (400, 500, 600, 700, 300, 800), "unexpected weight"
    chain = _chain(weight)
    res: dict = {"inter": 0, "fallback": {}, "missing": []}
    for ch in s:
        if ch.isspace():
            continue
        cp = ord(ch)
        hit = next((i for i, f in enumerate(chain) if cp in f.cmap), None)
        if hit is None:
            res["missing"].append(ch)
        elif hit == 0:
            res["inter"] += 1
        else:
            name = Path(chain[hit].path).name
            res["fallback"][name] = res["fallback"].get(name, 0) + 1
    return res


def direction(s: str) -> str:
    """First strong character decides: 'rtl' for R/AL, else 'ltr'."""
    assert isinstance(s, str), "direction takes a str"
    for ch in s:
        b = unicodedata.bidirectional(ch)
        if b in ("R", "AL"):
            return "rtl"
        if b == "L":
            return "ltr"
    return "ltr"


def chain(weight: int) -> tuple:
    """Faces available for coverage reporting."""
    assert weight in (400, 500, 600, 700, 300, 800), "unexpected weight"
    return _chain(weight)
