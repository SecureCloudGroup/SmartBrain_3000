"""Build a sealed `form` scene node (§34) from Library answers + fetched outputs.

WHY: Phase 1a-2 wiring hook. Callers in ni_flow pass the chosen answers + the
preview outputs that `_build_value_answers` / `_build_rows_answer` already produce;
this factory runs profile → enumerate → present (with the flow's model transport
when supplied, else rules), picks the sealed candidate, lays out desktop + phone,
and returns the sealed node the §34 validator accepts.

No model text ever reaches the output: PRESENT returns a DesignChoice that drives
the pick, but the bound CLIR's text comes from the code templates in ni_forms.lint
(TEXT_SRC closed to data/lexicon/ask/title/key/code).

The node also seals the runner-up ("second": form, variant, params, spans) so the
C2 answer ``presentation_id: "second"`` can re-seal the card without a refetch or a
re-enumeration (``swap_to_second``) — the record stays byte-identical.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from . import profile
from .enumerate import enumerate as enumerate_cands
from .enumerate import fallback as fallback_cand
from .layout import layout_span
from .present import present
from .record import from_answers, history_key
from .spans import Span
from .types import MAX_WANT_CHARS, MAX_WANTS, Candidate, DataRecord, PresentResult

_MAX_TRACKS = 4        # ni._MAX_HISTORY_SERIES
_HISTORY_POINTS = 200  # ni clamps max_points to 500; 200 keeps a sparkline week-deep at 15 min


def display_size_for_span(span_key: str) -> str:
    """Map a desktop span key (``d1x1``, ``d2x1``, ``d2x2``...) to the three
    sizes the client knows: ``small`` (1×1), ``wide`` (2×1 or 1×2),
    ``large`` (anything bigger). Phone keys fall back to ``small``.
    """
    assert isinstance(span_key, str) and span_key, "span_key required"
    try:
        s = Span.parse(span_key)
    except (KeyError, ValueError):
        return "small"
    if s.device != "desktop":
        return "small"
    return _size_from_shape(s.cols, s.rows)


def _size_from_shape(cols: int, rows: int) -> str:
    """One lookup — small for 1×1, wide for 2×1, large for everything else."""
    assert isinstance(cols, int) and isinstance(rows, int), "cols/rows must be ints"
    if cols == 1 and rows == 1:
        return "small"
    if cols == 2 and rows == 1:
        return "wide"
    return "large"


def _flow_model_adapter(call_model: Callable[[str], str] | None
                        ) -> Callable[[list], str] | None:
    """Translate the flow's `call_model(prompt: str) -> str` seam into PRESENT's
    `call(messages) -> str`. PRESENT joins the system prompt + the user message
    into one string so the existing route / consent / timeouts apply.
    """
    assert call_model is None or callable(call_model), "call_model must be a callable or None"
    if call_model is None:
        return None

    def adapter(messages: list) -> str:
        """Collapse PRESENT messages ("system" first, "user" next) into one prompt."""
        parts: list[str] = []
        for m in messages[:8]:  # bounded: PRESENT emits <= 4 msgs
            role = m.get("role") or ""
            content = m.get("content") or ""
            if role and content:
                parts.append(f"{role}:\n{content}")
        return call_model("\n\n".join(parts))

    return adapter


def _pick_candidate(res: PresentResult, cands: list) -> tuple[Candidate, Candidate | None]:
    """`(chosen, second)` from the PresentResult, keyed by id."""
    assert cands, "cands must be non-empty"
    assert isinstance(res, PresentResult), "res must be a PresentResult"
    chosen = next((c for c in cands if c.id == res.used), cands[0])
    second = next((c for c in cands if res.second and c.id == res.second), None)
    return chosen, second


def _record_fields_for_node(record: DataRecord) -> list[dict]:
    """Serialise ni_forms Fields into the §34 record.fields leaf shape.

    Keeps only the keys the validator reads so a drift in ni_forms.Field never
    writes a wider shape through. ``name/label/path`` get strict-length clamp.
    """
    assert isinstance(record, DataRecord), "record required"
    out: list[dict] = []
    for f in record.fields[:8]:  # bounded: _FORM_MAX_FIELDS
        spec = {"name": f.name[:80], "label": f.label[:80], "path": f.path[:80],
                "type": f.type, "role": f.role}
        if f.unit:
            spec["unit"] = str(f.unit)[:80]
        if f.currency:
            spec["currency"] = str(f.currency)[:80]
        if f.scale:
            spec["scale"] = str(f.scale)[:80]
        if f.precision is not None:
            spec["precision"] = int(f.precision)
        if f.wallclock:
            spec["wallclock"] = True
        out.append(spec)
    return out


def _candidate_seal(cand: Candidate) -> dict:
    """The part of a candidate the node seals: what the refresh binder re-finds by
    (form, variant, params) plus the spans it lays out at."""
    assert isinstance(cand, Candidate), "cand must be a Candidate"
    assert cand.default_span, "a sealed candidate has a default span"
    # no lint-clean phone span: the phone shows the full-width span of the same height (the bind
    # counts its lint), never a desktop layout squeezed onto a phone
    phone = cand.phone_span or f"p2x{Span.parse(cand.default_span).rows}"
    return {"id": cand.id, "form": cand.form, "variant": cand.variant,
            "params": dict(cand.params or {}), "spans": {"desktop": cand.default_span, "phone": phone}}


@dataclass
class Design:
    """What ``design`` decided, for the flow (``node``) and for tests that read the layout the
    pick was judged by (``cand`` / ``cands`` / ``rec`` / ``prof`` / ``inp`` / ``present``)."""

    node: dict
    cand: Candidate
    cands: list
    rec: DataRecord
    prof: object
    inp: object
    present: PresentResult


def frame_of(question_kind: str | None, wants: list | None) -> dict | None:
    """The sealed ``frame`` leaf (None = legacy node, nothing to seal): the closed kind and
    at most MAX_WANTS user words of MAX_WANT_CHARS."""
    assert question_kind is None or isinstance(question_kind, str), "question_kind must be a str or None"
    assert wants is None or isinstance(wants, list), "wants must be a list or None"
    clean = [str(w)[:MAX_WANT_CHARS] for w in (wants or [])[:MAX_WANTS] if isinstance(w, str) and w.strip()]
    if not question_kind and not clean:
        return None
    return {"kind": question_kind or None, "wants": clean}


def design(chosen: list[dict], outputs: dict, *, title: str, ask: str,
           now: datetime, source_url: str | None = None, cadence_s: int = 0,
           rows_output_name: str | None = None,
           call_model: Callable[[str], str] | None = None,
           viewer_tz: str = "UTC", question_kind: str | None = None,
           wants: list | None = None) -> Design:
    """Design the card: record → profile → enumerate → present → the sealed §34 node, plus the
    engine objects behind it. ``question_kind`` / ``wants`` are the ask's frame (fix round 1a-5):
    they ride on the CardInput (the floor prior, the asked-field rule, PRESENT's menu) and are
    sealed as ``frame`` so the bind re-enumerates under the same prior."""
    assert isinstance(chosen, list) and chosen, "chosen required"
    assert isinstance(outputs, dict), "outputs required"
    assert isinstance(now, datetime) and now.tzinfo is not None, "now must be an aware datetime"
    frame = frame_of(question_kind, wants)
    context = {"source_url": source_url, "viewer_tz": viewer_tz,
               "fetched_at": now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
    rec, inp = from_answers(chosen, outputs, history=None, context=context, ask=ask,
                            title=title, cadence_s=cadence_s, rows_output_name=rows_output_name,
                            question_kind=frame["kind"] if frame else None,
                            wants=frame["wants"] if frame else None)
    prof = profile.profile(rec, inp, now)
    cands = enumerate_cands(rec, prof, inp, now)
    assert cands, "enumerate always returns a candidate (the universal fallback)"
    res = present(cands, rec, prof, inp, call=_flow_model_adapter(call_model))
    chosen_cand, second_cand = _pick_candidate(res, cands)
    chosen_cand, pick, gates = _select_clean_design(cands, chosen_cand, rec, prof, inp, now)
    if second_cand is not None and second_cand.id == chosen_cand.id:
        second_cand = None   # the runner-up is never the same design as the pick
    node = {
        "type": "form",
        "form": pick["form"],
        "variant": pick["variant"],
        "params": pick["params"],
        "record": {
            "kind": rec.kind,
            "fields": _record_fields_for_node(rec),
            "rows": rows_output_name,
        },
        "spans": pick["spans"],
        "design": {
            "designer": "model" if (res.designer == "model" and not gates) else "rules",
            "pick": pick["id"],
            "second": _candidate_seal(second_cand) if second_cand is not None else None,
        },
    }
    if chosen_cand.fallback:
        node["design"]["fallback"] = True
    if gates:
        node["design"]["gates"] = gates
    if frame is not None:
        node["frame"] = frame
    return Design(node=node, cand=chosen_cand, cands=cands, rec=rec, prof=prof, inp=inp, present=res)


def form_scene(chosen: list[dict], outputs: dict, *, title: str, ask: str,
               now: datetime, source_url: str | None = None, cadence_s: int = 0,
               rows_output_name: str | None = None,
               call_model: Callable[[str], str] | None = None,
               viewer_tz: str = "UTC", question_kind: str | None = None,
               wants: list | None = None) -> dict:
    """Return a sealed §34 form scene node from chosen Library answers + outputs.

    The caller passes the SAME ``chosen`` the sealed pipeline consumed and the
    SAME ``outputs`` the pipeline produced; the engine designs the card from them.

    ``now`` is injected (the flow stamps it when it fetched); forms code never
    reads the clock itself. ``rows_output_name`` names the top-level outputs
    key holding the list rows (``"rows"`` for the Library-answers path); None
    = value-answer path. ``viewer_tz`` is the user's IANA zone (the card zone
    when the source names none). ``question_kind`` / ``wants``: the ask's frame
    (absent = legacy: no prior beyond the data's shape).
    """
    assert isinstance(chosen, list) and chosen, "chosen required"
    assert isinstance(outputs, dict), "outputs required"
    return design(chosen, outputs, title=title, ask=ask, now=now, source_url=source_url, cadence_s=cadence_s,
                  rows_output_name=rows_output_name, call_model=call_model, viewer_tz=viewer_tz,
                  question_kind=question_kind, wants=wants).node


def _spans_red_free(cand: Candidate, rec: DataRecord, prof, inp, spans: dict, now: datetime) -> bool:
    """True when the pick's two sealed spans both lay out lint-clean (fix round 1a-6, class L):
    the build-time smoke layout now gates the pick instead of discarding its result."""
    assert isinstance(spans, dict) and {"desktop", "phone"} <= set(spans), "spans need desktop + phone"
    assert isinstance(now, datetime) and now.tzinfo is not None, "now must be an aware datetime"
    for key in ("desktop", "phone"):
        if not layout_span(cand, rec, prof, inp, Span.parse(spans[key]), now).lint.ok:
            return False
    return True


def _select_clean_design(cands: list, chosen_cand: Candidate, rec: DataRecord, prof, inp,
                         now: datetime) -> tuple[Candidate, dict, list[str]]:
    """The design PRESENT (or the floor) named, else the next candidate in floor order, else
    the universal fallback at its smallest clean span (fix round 1a-6, class L): a design is
    sealed only when BOTH its sealed spans are red-free at build. ``gates`` names what was
    skipped, for ``design.gates`` (``ni._validate_form_design`` caps it at 12 short strings).
    """
    assert isinstance(cands, list) and cands, "cands must be a non-empty list"
    assert isinstance(now, datetime) and now.tzinfo is not None, "now must be an aware datetime"
    order = [chosen_cand] + [c for c in cands if c.id != chosen_cand.id]
    gates: list[str] = []
    for cand in order[:len(cands)]:   # bounded: cands is capped at enumerate.MAX_CANDS (4)
        seal = _candidate_seal(cand)
        if _spans_red_free(cand, rec, prof, inp, seal["spans"], now):
            return cand, seal, gates
        gates.append(f"red:{cand.id}:{cand.form}")
    fb = fallback_cand(rec, prof, inp, now)
    gates.append(f"fallback:{fb.id}:{fb.form}")
    return fb, _candidate_seal(fb), gates


def swap_to_second(node: dict) -> dict:
    """The same sealed record under the runner-up design: the node's ``second`` becomes
    the pick (form, variant, params, spans) and the former pick becomes the second, so
    the user can still be offered the other one. Raises ValueError when no second was
    sealed. Never refetches, never re-enumerates (the design was fixed at build)."""
    assert isinstance(node, dict) and node.get("type") == "form", "a form node is required"
    assert isinstance(node.get("design"), dict), "form node carries a design"
    second = node["design"].get("second")
    if second is None:
        raise ValueError("this card sealed no second presentation")
    assert isinstance(second, dict), "a sealed second is a dict (validated at seal time)"
    former = {"id": node["design"]["pick"], "form": node["form"], "variant": node["variant"],
              "params": dict(node.get("params") or {}), "spans": dict(node["spans"])}
    out = dict(node)
    out["form"], out["variant"] = second["form"], second["variant"]
    out["params"], out["spans"] = dict(second.get("params") or {}), dict(second["spans"])
    out["design"] = {"designer": node["design"]["designer"], "pick": second["id"], "second": former}
    return out   # the sealed frame (when present) rides along: the same prior at every bind


def history_track_for(record_fields: list[dict], rows_name: str | None) -> dict | None:
    """§11 history track built from the sealed form.record fields: every numeric
    measure field named here is tracked under ``history_key(name)`` with its own
    output path (max 4 series), so the stat's sparkline accrues from refreshes.

    Returns ``{track: {<name>_h: path}, max_points: 200}`` or None when nothing is a
    numeric measure. A rows-shaped record never produces a track (no per-field output).
    """
    assert isinstance(record_fields, list), "record_fields must be a list"
    assert rows_name is None or isinstance(rows_name, str), "rows_name must be a str or None"
    if rows_name:
        return None
    track: dict[str, str] = {}
    for f in record_fields[:8]:
        if f.get("role") == "measure" and f.get("type") in (
                "number", "quantity", "currency", "percent"):
            track[history_key(f["name"])] = f["path"]
        if len(track) >= _MAX_TRACKS:
            break
    if not track:
        return None
    return {"track": track, "max_points": _HISTORY_POINTS}
