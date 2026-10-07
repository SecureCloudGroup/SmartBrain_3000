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
from datetime import UTC, datetime

from . import profile
from .enumerate import enumerate as enumerate_cands
from .layout import layout_span
from .present import present
from .record import from_answers, history_key
from .spans import Span
from .types import Candidate, DataRecord, PresentResult

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


def form_scene(chosen: list[dict], outputs: dict, *, title: str, ask: str,
               now: datetime, source_url: str | None = None, cadence_s: int = 0,
               rows_output_name: str | None = None,
               call_model: Callable[[str], str] | None = None,
               viewer_tz: str = "UTC") -> dict:
    """Return a sealed §34 form scene node from chosen Library answers + outputs.

    The caller passes the SAME ``chosen`` the sealed pipeline consumed and the
    SAME ``outputs`` the pipeline produced; the engine designs the card from them.

    ``now`` is injected (the flow stamps it when it fetched); forms code never
    reads the clock itself. ``rows_output_name`` names the top-level outputs
    key holding the list rows (``"rows"`` for the Library-answers path); None
    = value-answer path. ``viewer_tz`` is the user's IANA zone (the card zone
    when the source names none).
    """
    assert isinstance(chosen, list) and chosen, "chosen required"
    assert isinstance(outputs, dict), "outputs required"
    assert isinstance(now, datetime) and now.tzinfo is not None, "now must be an aware datetime"
    context = {"source_url": source_url, "viewer_tz": viewer_tz,
               "fetched_at": now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
    rec, inp = from_answers(chosen, outputs, history=None, context=context, ask=ask,
                            title=title, cadence_s=cadence_s, rows_output_name=rows_output_name)
    prof = profile.profile(rec, inp, now)
    cands = enumerate_cands(rec, prof, inp, now)
    if not cands:
        raise ValueError("form_scene: no candidate forms for this record")
    res = present(cands, rec, prof, inp, call=_flow_model_adapter(call_model))
    chosen_cand, second_cand = _pick_candidate(res, cands)
    pick = _candidate_seal(chosen_cand)
    _layout_once(chosen_cand, rec, prof, inp, pick["spans"]["desktop"], now)  # lint fires at build
    return {
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
            "designer": "model" if res.designer == "model" else "rules",
            "pick": pick["id"],
            "second": _candidate_seal(second_cand) if second_cand is not None else None,
        },
    }


def _layout_once(cand: Candidate, rec: DataRecord, prof, inp, span_key: str, now: datetime) -> None:
    """Smoke-layout the pick at its default span so a surprise lint failure fires
    at build time rather than first refresh. Discards the output."""
    assert isinstance(span_key, str) and span_key, "span_key required"
    assert isinstance(now, datetime), "now must be a datetime"
    layout_span(cand, rec, prof, inp, Span.parse(span_key), now)


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
    return out


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
