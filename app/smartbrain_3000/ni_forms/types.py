"""Frozen artifact types ported from the proto. Owner: architect.

Every artifact is a plain dataclass that serialises with `to_json()` (asdict) and
is checked with the matching `check_*()` (raises ContractError). Enums are closed.

Trimmed vs the proto: RoleResult / RoleQuestion / Assignment, CardState / TickResult /
RawFetch, BuildOut and Critique are not re-exported here. They belong to pipelines
(role-binding, refresh, orchestration) that are not part of this Phase 1a port.
Added "MMM yyyy" and "MMM d, yyyy" to TIME_FMTS so fmt.coarse_fmt output passes
check_clir (the proto emitted them from forms/fmt.py but never widened the enum).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

# --------------------------------------------------------------------------- enums
KINDS = frozenset({"measure", "records", "series", "events", "text", "image"})
FIELD_TYPES = frozenset({"number", "quantity", "currency", "percent", "time", "date", "datetime",
                         "duration", "category", "text", "url", "bool", "lat", "lon", "identifier"})
ROLES = frozenset({
    # single-fact roles
    "measure", "reference", "delta", "delta_pct", "range_lo", "range_hi", "open", "goal", "progress",
    # time roles
    "as_of", "time", "time_end", "date",
    # record roles
    "name", "kind", "status", "severity", "rank", "value", "secondary", "share", "count",
    "series_id", "group", "link", "link_secondary", "meta", "text_body", "image_url",
    "lat", "lon", "score_a", "score_b", "name_b",
    # no display
    "ignore", "unknown",
})
PRODUCERS = frozenset({"json_profile", "table_records", "text_scan", "feed_records", "csv_records",
                       "ics_records", "columnar_records", "image_meta"})
INFERENCE_CODES = frozenset({
    "day_groups_positional", "month_from_heading", "extremum_kind_by_neighbors", "meridiem_from_same_page",
    "zone_rederived", "family_melted", "family_reduced_quantiles", "rows_filtered_by_param",
    "rows_window_nearest_now", "unit_from_units_key", "unit_from_param", "unit_from_header",
    "decimal_comma", "bom_stripped", "epoch_as_of", "columnar_zipped", "fixed_width_block",
})
ERROR_CODES = frozenset({
    "http_status", "not_ready", "upstream_error_envelope", "parse_failed", "no_producer",
    "all_null", "unit_conflict", "weekday_mismatch", "duplicate_path", "roles_no_longer_hold",
    "empty_body",
})
SIGNATURES = frozenset({
    "single_measure", "measure_with_range", "measure_with_reference", "multi_measure",
    "time_series_regular", "time_series_irregular", "multi_series", "series_with_band", "matrix",
    "alternating_extrema", "events_with_kind", "dated_rows", "ranked_records", "status_records",
    "records_with_links", "progress_to_goal", "percent_of_whole", "hierarchical_records",
    "state_timeline", "geo_points", "text_passage", "image", "empty",
})
INTENTS = frozenset({"now", "today", "plan", "monitor", "compare", "read", "trend", "locate"})
FORMS = frozenset({"stat", "conditions", "kv_grid", "compare", "bars", "series_line", "heatmap",
                   "event_curve", "next_event", "agenda", "day_table", "entity_list", "ranked_list",
                   "table", "text_brief", "image", "progress", "map_lite"})
TEXT_ROLES = frozenset({"hero", "headline", "display", "name", "row", "row-strong", "title", "body",
                        "item", "sub", "delta", "index", "meta", "badge", "footer", "label", "tick"})
TEXT_SRC = frozenset({"data", "lexicon", "ask", "title", "key", "code"})   # never "model"
TIME_FMTS = frozenset({"h:mm a", "h a", "ha_short", "EEE", "EEE d", "MMM d", "EEE MMM d", "yyyy", "MMM yyyy", "MMM d, yyyy",
                       "MMM d, h:mm a", "HH:mm"})
ICONS = frozenset({"sun", "moon", "cloud", "cloud_sun", "cloud_moon", "rain", "drizzle", "snow",
                   "storm", "fog", "wind", "alert", "check", "cross", "clock", "link_out", "lock",
                   "unlock", "pin", "up", "down"})
CRITIQUE_CODES = frozenset({"ok", "overlap", "clipped_text", "unreadable_small", "empty_region",
                            "plot_too_small", "misleading_scale", "wrong_emphasis", "clutter",
                            "colour_only_meaning", "inconsistent_style", "hard_to_read_value"})

REJECT_CODES = frozenset({"too_narrow", "too_short", "needs_series", "needs_parts", "multi_series_identity",
                          "aspect", "sibling_better", "lint_red", "cell_too_small", "shrinks"})
OUT_OF_SCOPE = frozenset({"interactive", "composite_sources", "video", "audio", "push_event", "document",
                          "rule_needs_anchor"})

Cell = str | int | float | bool | None


class ContractError(ValueError):
    pass


def _need(errs: list, cond: bool, msg: str):
    if not cond:
        errs.append(msg)


def to_json(obj) -> dict:
    return asdict(obj)


# --------------------------------------------------------------------------- input
@dataclass
class CardInput:
    """What the pipeline receives for one card. NOTHING card-specific beyond this.
    `c2_answers` simulates the user's answers to C2 questions (user words, counted
    and reported separately); it is empty unless the oracle supplies it."""
    card_id: str                     # corpus id (tests only; the pipeline must never branch on it)
    ask: str                         # the user's words, verbatim
    title: str                       # the user's card title
    source_url: str | None
    source_kind: str                 # http_json|http_page|internal|image|mcp|model
    source_format: str               # json|csv|xml|rss|text|html|image|pagegraph|ics|svg
    raw_path: str | None          # absolute path of the fetched bytes (or pagegraph / synthetic JSON)
    http_status: int | None
    content_type: str | None
    fetched_at: str                  # ISO8601Z
    cadence_s: int                   # 0 = clock-only / rule-only card
    viewer_tz: str = "America/New_York"
    c2_answers: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- DataRecord v1
@dataclass
class Field:
    name: str                        # slug, unique within the record
    label: str                       # humanised source key (code-made; display only via LabelRef rules)
    path: str                        # source locator of the column, e.g. "current.temperature_2m"
    type: str                        # FIELD_TYPES
    role: str = "unknown"            # ROLES; set by ROLE-BIND (producers leave "unknown" unless structural)
    unit: str | None = None       # UnitId from assets/lexicon/units.json
    unit_src: str | None = None   # "data:<path>" | "param:<name>" | "header:<text>"
    currency: str | None = None   # ISO 4217
    scale: str | None = None      # percent only: "0..1" | "0..100"
    precision: int | None = None  # decimals as PUBLISHED by the source (published_precision lint)
    agg: str | None = None        # "max_over_rows"|"min_over_rows"|"sum"|"count"|"per_interval"
    ordinal: list | None = None   # <= 12 ordered category values
    cumulative: bool = False         # ordinal categories that contain later ones (drought d0 >= d1 ...)
    relative_to: str | None = None  # duration only: "row0" | None
    wallclock: bool = False          # source time had no zone; interpreted in card_tz
    derived: str | None = None    # set when code computed the column (never the model)
    unrounded: bool = False          # JSON floats with no recoverable published precision (G3): shown by magnitude
    label_src: str | None = None  # "lexicon" | "key" | "ask": where the display label came from (ROLE-BIND)


@dataclass
class RowMeta:
    aggregate: bool = False          # row is a sum of other rows (World, Arab World ...)
    group: str | None = None      # parent label for hierarchical records
    depth: int = 0


@dataclass
class Context:
    fetched_at: str
    as_of: str | None             # the data's own time, ISO8601Z
    source_host: str                 # from CardInput.source_url, never from data
    card_tz: str                     # IANA
    card_tz_src: str                 # "data" | "c2_answer" | "c2_default"
    period: str | None = None     # "YYYY-MM"
    place: str | None = None
    station: str | None = None
    params: dict = field(default_factory=dict)   # sealed params parsed from the consented URL query


@dataclass
class ImageRef:
    sha256: str                      # blob at out/blobs/<sha256>.<ext>
    mime: str
    w: int
    h: int
    frames: int = 1
    src_host: str = ""


@dataclass
class DataRecord:
    v: int
    kind: str                                        # KINDS
    fields: list                                     # list[Field], 1..24
    rows: list                                       # list[list[Cell]]; measure = 1 row; <= 500
    context: Context
    producer: str                                    # PRODUCERS
    locators: dict = field(default_factory=dict)     # "r,c" -> human locator ("table 0 · row 24 · Low Tide›PM›ft")
    inferred: list = field(default_factory=list)     # INFERENCE_CODES
    flags: dict = field(default_factory=dict)        # date_confidence, lost_columns, ambiguous, rows_truncated,
                                                     # truncated_cells, missing_items, sample, stale_as_of
    row_meta: list | None = None                  # list[RowMeta] parallel to rows, or None
    long_text: dict = field(default_factory=dict)    # "r,c" -> full text (<= 4000) when a cell was capped at 120
    image: ImageRef | None = None                 # kind == "image" or a record with one hero image
    parts: dict = field(default_factory=dict)        # name -> DataRecord (multi-part responses; one level only)
    error: dict | None = None                     # {code: ERROR_CODES, detail: str}
    fingerprint: str = ""                            # sha256(kind, [(name,type,unit,role)], row_count_class)
    data_hash: str = ""                              # sha256 of canonical(fields, rows, context minus fetched_at)


def check_record(r: DataRecord) -> None:
    e: list = []
    _need(e, r.v == 1, "v must be 1")
    _need(e, r.kind in KINDS, f"kind {r.kind!r}")
    _need(e, r.producer in PRODUCERS, f"producer {r.producer!r}")
    if r.error:
        _need(e, r.error.get("code") in ERROR_CODES, f"error code {r.error.get('code')!r}")
    else:
        _need(e, 1 <= len(r.fields) <= 24 or r.kind == "image", f"fields {len(r.fields)} not in 1..24")
    names = [f.name for f in r.fields]
    _need(e, len(set(names)) == len(names), "duplicate field names")
    paths = [f.path for f in r.fields]
    _need(e, len(set(paths)) == len(paths), "duplicate column paths")
    for f in r.fields:
        _need(e, f.type in FIELD_TYPES, f"{f.name}: type {f.type!r}")
        _need(e, f.role in ROLES, f"{f.name}: role {f.role!r}")
        _need(e, f.scale in (None, "0..1", "0..100"), f"{f.name}: scale")
    _need(e, len(r.rows) <= 500, "rows > 500")
    if r.kind == "measure":
        _need(e, len(r.rows) <= 1, "measure must have <= 1 row")
    for i, row in enumerate(r.rows[:500]):
        if len(row) != len(r.fields):
            e.append(f"row {i} width {len(row)} != {len(r.fields)}")
            break
        for c in row:
            if isinstance(c, str) and len(c) > 120 and r.kind != "text":
                e.append(f"row {i}: cell > 120 chars (cap it and use long_text)")
                break
    for code in r.inferred:
        _need(e, code in INFERENCE_CODES, f"inferred {code!r}")
    if r.row_meta is not None:
        _need(e, len(r.row_meta) == len(r.rows), "row_meta length")
    _need(e, bool(r.fingerprint) or bool(r.error), "fingerprint missing")
    if e:
        raise ContractError("DataRecord: " + "; ".join(e))


# --------------------------------------------------------------------------- PROFILE
@dataclass
class FieldProfile:
    name: str
    type: str
    unit: str | None
    role: str
    distinct: int
    nulls: int
    min: float | None
    max: float | None
    sorted: str | None            # "asc" | "desc" | None
    max_len: int
    p50_len: int


@dataclass
class TimeProfile:
    field: str | None
    grain: str | None             # "second"|"minute"|"quarter_hour"|"hour"|"day"|"week"|"month"|"year"|"irregular"
    span_h: float | None
    covers_today: bool               # in card_tz
    future_events: int
    past_events: int
    days: int                        # distinct card-tz dates covered


@dataclass
class Profile:
    sig: str                         # sha256 of the profile (for few-shot retrieval)
    n_rows: int
    fields: list                     # list[FieldProfile]
    signatures: list                 # SIGNATURES, most specific first
    time: TimeProfile
    wants: list                      # WantWord ids extracted from the ask by the generic want lexicon
    wants_coverage: dict             # want -> field name | None
    history_points: int              # card's own history (0 in P0)


# --------------------------------------------------------------------------- ENUMERATE / PRESENT
@dataclass
class Candidate:
    id: str                          # "c0".."c5", stable order = floor rank
    form: str                        # FORMS
    variant: str                     # form-declared variant id
    bindings: dict                   # slot -> field name (form-declared slots)
    params: dict                     # form-declared closed params (window, label_policy, sort, top_n, mark ...)
    describes: str                   # code-written one-liner for the model menu and the "why" line
    covers: list                     # wants this candidate shows at its default span
    intent: str                      # INTENTS the candidate serves best (code)
    plans: dict                      # span key -> plan id, ONLY spans that are valid (lint-clean at floor+ceiling)
    rejected_spans: dict             # span key -> reason code
    default_span: str                # desktop span key
    phone_span: str | None        # phone span key (spans.phone_default)
    floor_score: float               # code rank score (never shown to the model)
    shows: dict = field(default_factory=dict)   # span key -> {fields, rows, plot, title_cut} (resize checks)


@dataclass
class DesignChoice:
    intent: str
    fits: list                       # [{"cand": id, "answers_ask": "yes"|"partly"|"no"}] every candidate
    pick: str
    second: dict | None           # {"cand": id, "intent": INTENT} | None
    primary_field: str | None
    labels: dict                     # field -> lexicon id | "key" | "ask"
    uncovered_wants: list
    none_fits: bool


@dataclass
class PresentResult:
    choice: DesignChoice | None   # None when the model was unavailable
    designer: str                    # "model" | "rules"
    used: str                        # candidate id actually used (after hard gates)
    second: str | None            # second option shown at C2, or None
    gates: list                      # hard-gate events, e.g. ["pick_failed_lint:c2", "L-ASK:c1"]
    model_call: dict | None = None


# --------------------------------------------------------------------------- CLIR v1
# X anchor: ["l", px] from content-left | ["r", px] from content-right |
#           ["c", px] from centre | ["f", frac] fraction of content width.
# y is absolute px from the content-box top (heights never stretch).
X = list


@dataclass
class TextPrim:
    id: int
    x: X
    y: float                         # baseline of the first line
    max_w: float                     # width available at the bucket FLOOR (lint + client ellipsis)
    lines: list                      # pre-broken with HarfBuzz; <= role max lines
    role: str                        # TEXT_ROLES (sets px/wt/lh/tnum via tokens.TYPE)
    px: int                          # actual px (hero/headline ladders may step down)
    tok: str
    anchor: str = "start"            # "start" | "middle" | "end"
    dir: str = "ltr"                 # "ltr" | "rtl"
    src: str = "data"                # TEXT_SRC
    k: str = "text"


@dataclass
class TimePrim:
    id: int
    x: X
    y: float
    max_w: float                     # laid out for the widest value of fmt
    t: str                           # ISO8601Z
    fmt: str                         # TIME_FMTS
    tz: str                          # "card" | "viewer"
    zone: str                        # IANA of card_tz (used when tz == "card")
    role: str
    px: int
    tok: str
    anchor: str = "start"
    show_zone: bool = False          # append zone abbreviation when card_tz != viewer tz
    k: str = "time"


@dataclass
class RectPrim:
    id: int
    x0: X
    x1: X
    y0: float
    y1: float
    tok: str
    r: float = 0
    stroke: str | None = None
    k: str = "rect"


@dataclass
class LinePrim:
    id: int
    x0: X
    y0: float
    x1: X
    y1: float
    tok: str
    w: float = 1
    dash: list | None = None
    k: str = "line"


@dataclass
class TriPrim:
    id: int
    x: X
    y: float                         # centre
    size: float
    dir: str                         # "up"|"down"|"left"|"right"
    tok: str
    k: str = "tri"


@dataclass
class DotPrim:
    id: int
    x: X
    y: float
    r: float
    tok: str
    ring: str | None = None
    ring_w: float = 0
    k: str = "dot"


@dataclass
class PathPrim:
    id: int
    box: dict                        # {"x0": X, "x1": X, "y0": float, "y1": float}
    pts: list                        # [[fx, fy], ...] fractions of box, fy 0 = top; <= 400 points
    tok: str
    w: float = 2
    style: str = "solid"             # "solid" | "interp"
    fill: str | None = None       # token; fill to box bottom
    k: str = "path"


@dataclass
class CellsPrim:
    id: int
    box: dict
    cols: int
    rows: int
    v: list                          # cols*rows ints: 0..steps-1, -1 = no data
    ramp: str                        # "seq" | "div"
    steps: int                       # 5
    gap: float = 1
    k: str = "cells"


@dataclass
class ImagePrim:
    id: int
    box: dict
    ref: str                         # ImageRef.sha256
    fit: str                         # "cover" | "contain"
    crop: list | None = None      # [fx0, fy0, fx1, fy1] of the source for cover
    k: str = "image"


@dataclass
class BasemapPrim:
    id: int
    box: dict
    asset: str                       # "world110m"
    bbox: list                       # [lon0, lat0, lon1, lat1]
    land: str = "map-land"
    stroke: str = "map-stroke"
    k: str = "basemap"


@dataclass
class IconPrim:
    id: int
    x: X
    y: float                         # centre
    size: float
    name: str                        # ICONS
    tok: str
    k: str = "icon"


PRIM_KINDS = ("text", "time", "rect", "line", "tri", "dot", "path", "cells", "image", "basemap", "icon")


@dataclass
class LiveBinding:
    """k in: now_marker{prims, box, t0, t1, path?} | countdown{prim, t, fmt} | count_up{prim, t0, unit}
    | timed_variants{slot, variants:[{t_from, t_to, prims}]} | past_dim{prims, t}
    | extrapolate{prim, v0, rate_per_s, t0, fmt} | age{prim, t}"""
    k: str
    args: dict


@dataclass
class CLIR:
    v: int
    form: str
    variant: str
    cand: str
    plan: str                        # plan id used for this span
    span: str                        # span key, e.g. "d2x1"
    bucket: dict                     # {"min_w", "max_w", "h"}
    state: str                       # "ok"|"empty"|"sparse"|"stale"|"error_last_good"|"sample"
    prims: list                      # prims as dicts (kinds above), <= 2000
    reading_order: list              # prim ids, complete over text/time prims
    summary: str                     # code-generated accessible summary
    live: list                       # list[LiveBinding] as dicts
    hitmap: list                     # [{"prim": id, "label": str}]
    dropped: list = field(default_factory=list)   # content ids dropped by the plan/ladder at this span
    ladder: list = field(default_factory=list)    # ladder rungs applied, in order
    # NOT part of the CLIR hash: timings, lint - kept in LayoutOut


def check_clir(c: dict) -> None:
    """Validate a CLIR dict (the form painters and the client consume)."""
    from . import tokens as T
    e: list = []
    _need(e, c.get("v") == 1, "v")
    _need(e, c.get("form") in FORMS, f"form {c.get('form')!r}")
    prims = c.get("prims", [])
    _need(e, len(prims) <= 2000, "prims > 2000")
    ids = set()
    for p in prims:
        k = p.get("k")
        _need(e, k in PRIM_KINDS, f"prim kind {k!r}")
        _need(e, p["id"] not in ids, f"dup prim id {p['id']}")
        ids.add(p["id"])
        for tk in ("tok", "stroke", "ring", "fill", "land"):
            if p.get(tk) is not None:
                _need(e, p[tk] in T.COLOR_NAMES, f"prim {p['id']}: unknown token {p[tk]!r}")
        if k in ("text", "time"):
            _need(e, p.get("role") in TEXT_ROLES, f"prim {p['id']}: role {p.get('role')!r}")
            _need(e, p.get("px", 0) >= 11, f"prim {p['id']}: px < 11")
        if k == "text":
            _need(e, p.get("src") in TEXT_SRC, f"prim {p['id']}: src {p.get('src')!r}")
        if k == "time":
            _need(e, p.get("fmt") in TIME_FMTS, f"prim {p['id']}: fmt {p.get('fmt')!r}")
        if k == "path":
            _need(e, len(p.get("pts", [])) <= 400, f"prim {p['id']}: > 400 pts")
            _need(e, p.get("style") in ("solid", "interp"), f"prim {p['id']}: style")
        if k == "icon":
            _need(e, p.get("name") in ICONS, f"prim {p['id']}: icon {p.get('name')!r}")
    texty = {p["id"] for p in prims if p.get("k") in ("text", "time")}
    _need(e, texty <= set(c.get("reading_order", [])), "reading_order incomplete")
    _need(e, bool(c.get("summary")), "summary missing")
    if e:
        raise ContractError("CLIR: " + "; ".join(e))


# --------------------------------------------------------------------------- LINT
@dataclass
class LintIssue:
    code: str                        # see CONTRACTS.md section 6.4 (closed list)
    sev: str                         # "red" (blocks) | "amber" (reported)
    prim: int | None = None
    width: int | None = None      # which bucket end (min_w or max_w) or None
    at: str | None = None         # simulated time for clock-relative lint
    detail: str = ""


@dataclass
class LintResult:
    ok: bool                         # no red issue at any checked width/time
    issues: list                     # list[LintIssue]
    widths: list                     # widths checked (always [min_w, max_w])
    times: list                      # simulated instants checked (ISO8601Z); [] if not clock-relative
    ms: float


@dataclass
class LayoutOut:
    clir: dict                       # canonical CLIR dict
    hash: str                        # sha256 of canonical CLIR
    lint: LintResult
    ms_layout: float
    ms_lint: float
    content: dict = field(default_factory=dict)


@dataclass
class Reject:
    code: str                        # REJECT_CODES
    detail: str = ""
