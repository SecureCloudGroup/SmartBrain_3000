"""Compare cards (plan S7): one card that shows the same metric for two to four named things side by side.

An ask that compares ("weather in A vs B", "downloads of X vs Y") used to land on a card for ONE of them, because a
card reads one source and one source answers for one thing. A compare card is a multi-source spec: its http_json
source carries ``compare = {entities: [{name, url, path}, ...]}``, one sealed address per compared thing. There are
no free parameters: each address is exact at Keep, so a refresh can only fetch what the user kept. ``source.url`` is
the first thing's address, so everything that reads one source address (robots, ports, the cadence floor, the host
shown to the user) keeps working; all addresses share that host. Things are told apart by their (address, path)
pair: one address may serve several things at different paths (a response that holds every rate), and ``fetch``
reads each distinct address once.

Each refresh fetches the addresses one by one through the same politeness path as any card (``ni_fetch``,
``ni_polite``), reads one number at each thing's ``path`` and hands the pipeline ``{"rows": [{"name", "value"}]}``.
The name is the one sealed from the user's question (``asked``), never text from a model or a response. A thing
whose address fails, or whose number is missing, fails the run as ``compare_partial`` naming it (the last good card
keeps showing); a card is never refreshed with fewer things than it was kept with. The card is the existing
``compare`` form over those rows.

``refusal`` is the build check that every build path runs (``ni_flow._finalize``): an ask that names N things must
land on a card showing all N, each with its own value. ``drift`` re-runs it on every refresh. This module imports
``ni`` only inside functions: ``ni`` imports it to validate the sealed ``compare`` key.
"""
from __future__ import annotations

import copy
import math
import re
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urlsplit

MIN_ENTITIES = 2
MAX_ENTITIES = 4
ROWS = "rows"
PARTIAL = "compare_partial"        # the run status of a card that could not read every compared thing
MAX_NAME = 80
MAX_PATH = 200
MAX_LABEL = 40
MAX_UNIT = 20
_ENTITY_KEYS = frozenset({"name", "url", "path"})
_WORD_RE = re.compile(r"[a-z0-9]+")
# a piece of a "X vs Y" ask that names a moment, not a thing ("now versus a year ago"): the ask compares a thing
# with itself over time, which one source can answer, so it is not a compare of named things
_TIME_PIECE_RE = re.compile(r"\b(?:ago|yesterday|previous|prior|earlier|later|(?:last|next|this) (?:year|month|week|night|quarter))\b")
_LEAD_WORDS = ("compare", "comparison of", "difference between", "differences between")
_UNIT_RE = re.compile(r"[^\W_][\w .%/$€£°²³-]{0,19}")


def _fold(text: str) -> list[str]:
    """The lower-case words of ``text`` (letters and digits), for whole-word matching without a backtracking regex."""
    assert isinstance(text, str), "text must be a string"
    assert len(text) < 10**6, "a bounded text"
    return _WORD_RE.findall(text.lower())


def names(entity: str, text: str) -> bool:
    """True when ``entity`` is named in ``text`` as whole words (any case, any spacing between the words)."""
    assert isinstance(entity, str) and isinstance(text, str), "strings required"
    want, have = _fold(entity), _fold(text)
    if not want or len(want) > len(have):
        return False
    return any(have[i:i + len(want)] == want for i in range(len(have) - len(want) + 1))


MAX_SCAN = 2000    # the characters of a query, name or sample string the provenance matcher reads (input cap)
MIN_HEAD = 4       # the shortest leading part of a name that may stand for it (a short all-caps token also may)


def _stem(token: str) -> str:
    """A word without a plural or possessive ``s`` ("Lakers" and "Laker's" read the same)."""
    return token[:-1] if len(token) > 3 and token.endswith("s") and not token.endswith("ss") else token


def _words(text: str) -> list[str]:
    """The normalised words of ``text`` (case, possessives and plurals folded), input-capped: one linear pass."""
    assert isinstance(text, str), "text must be a string"
    flat = text[:MAX_SCAN].replace("'s", " ").replace("\u2019s", " ")
    return [_stem(w) for w in _fold(flat)]


def _spans(want: list[str], have: list[str]) -> list[tuple[int, int]]:
    """Every span of ``have`` that reads as ``want`` on word boundaries, where the words may be joined or split
    ("iPhone 15" is "iphone15"; "Salt Lake" is "saltlake"); ``[]`` when it is not there."""
    assert isinstance(want, list) and isinstance(have, list), "word lists"
    joined = "".join(want)
    out: list[tuple[int, int]] = []
    if not joined:
        return out
    for i in range(len(have)):  # bounded by the capped text
        pos = 0   # how much of ``joined`` the words from i have covered (a running length: no growing string)
        for j in range(i, len(have)):
            if not joined.startswith(have[j], pos):
                break
            pos += len(have[j])
            if pos == len(joined):
                out.append((i, j + 1))
                break
    return out


def _find(want: list[str], have: list[str]) -> tuple[int, int] | None:
    """The first span ``_spans`` finds, else None."""
    found = _spans(want, have)
    return found[0] if found else None


_CAPS_RE = re.compile(r"[A-Za-z0-9]+")
MAX_RUN_WORDS = 12   # the words of a name the leading/trailing-part search reads (a name is at most MAX_NAME characters)


def _head(entity: str, others: list[str], have: list[str], text: str = "") -> tuple[int, int] | None:
    """A name that carries words of the ask's own ("Ford market cap") is also named by a part of it: its leading part
    ("Ford"), or the run that follows the words every other asked thing shares with it ("New York Yankees" against "New
    York Mets" is "Yankees"). The part must be longer than a stub and none of its words may belong to another asked
    thing. A two-letter all-caps stub ("LA") stands for the name only when ``text`` also writes it in capitals, as the
    ask did; a name typed in lower case has no stub. The longest part found wins."""
    want = _words(entity)[:MAX_RUN_WORDS]
    raw = entity[:MAX_SCAN].split()
    if len(want) < 2 or not raw:
        return None
    taken = {w for o in others for w in _words(o)}
    caps = {t for t in _CAPS_RE.findall(text[:MAX_SCAN]) if t.isupper()}
    stub = raw[0].isalpha() and raw[0].isupper() and len(raw[0]) >= 2 and raw[0] in caps
    for size in range(len(want) - 1, 0, -1):  # bounded: the words of a name
        for at in range(len(want) - size + 1):
            part = want[at:at + size]
            if any(w in taken for w in part) or (at > 0 and want[at - 1] not in taken):
                continue   # a run owned by another thing, or not following the words the things share
            if len("".join(part)) < MIN_HEAD and not (at == 0 and size == 1 and stub):
                continue
            hit = _find(part, have)
            if hit is not None:
                return hit
    return None


def search_names(entity: str, text: str, others: list[str] | tuple[str, ...] = ()) -> bool:
    """True when ``text`` (a search query, a name a model wrote) names ``entity`` among the asked things ``others``:
    whole words with case, spacing, plurals and possessives folded, or its leading part when the name carries the ask's
    trailing words. A name that sits inside a longer asked thing's name that ``text`` also names ("Denver" in "Denver
    Broncos") is not named on its own. Pure and input-capped; no word lists."""
    assert isinstance(entity, str) and isinstance(text, str), "strings required"
    have, want = _words(text), _words(entity)
    if not want or not have:
        return False
    rest = [o for o in others if o != entity]
    mine = _spans(want, have) or ([h] if (h := _head(entity, rest, have, text)) else [])
    longer = [_spans(w, have) for o in rest if len(w := _words(o)) > len(want) and _find(want, w) is not None]
    for span in mine:  # one place in the text that no longer asked thing's name covers is enough
        if not any(c[0] <= span[0] and span[1] <= c[1] for spans in longer for c in spans):
            return True
    return False


def named_in(entities: list[str] | tuple[str, ...], text: str) -> list[str]:
    """The asked things ``text`` names (``search_names``), in the ask's order."""
    assert isinstance(text, str), "text must be a string"
    return [e for e in entities if search_names(e, text, [o for o in entities if o != e])]


def mentions(parsed: object, entity: str, others: list[str] | tuple[str, ...] = ()) -> bool:
    """True when the parsed sample names ``entity`` in a key or a text value (bounded walk), by the rule
    ``search_names`` applies to a query (``others``: the other asked things): the evidence that a response which
    several things share is about this one."""
    assert isinstance(entity, str), "entity must be a string"
    stack: list[tuple[object, int]] = [(parsed, 0)]
    for _ in range(4000):  # bounded: nodes visited
        if not stack:
            return False
        node, depth = stack.pop()
        if isinstance(node, str):
            if search_names(entity, node, others):
                return True
        elif isinstance(node, dict) and depth < 8:
            for k, v in list(node.items())[:200]:
                if isinstance(k, str) and search_names(entity, k, others):
                    return True
                stack.append((v, depth + 1))
        elif isinstance(node, list) and depth < 8:
            stack.extend((v, depth + 1) for v in node[:200])
    return False


def _original(request: str, lowered: str) -> str:
    """``lowered`` as the user typed it in ``request`` (the first whole-word occurrence), else title-cased."""
    assert isinstance(request, str) and isinstance(lowered, str), "strings required"
    flat = " ".join(request.split())
    at = flat.lower().find(lowered)
    return flat[at:at + len(lowered)].strip(" ?!.,") if at >= 0 else lowered.title()


def asked(request: str) -> tuple[str, ...]:
    """The things a compare ask names, spelled as the user wrote them: two or more, or () when the words are not a
    comparison of named things (one thing, a moment against a moment, or a head-to-head result or schedule)."""
    assert isinstance(request, str), "request must be a string"
    from .ni_verify.constraints import parse_entities
    found = parse_entities(request)
    if len(found) < MIN_ENTITIES or any(_TIME_PIECE_RE.search(e) for e in found):
        return ()
    out = tuple(_original(request, e)[:MAX_NAME] for e in found)
    assert len(out) == len(found), "one spelling per thing"
    return out


def entity_ask(request: str, entities: tuple[str, ...] | list[str], index: int) -> str:
    """The ask for ONE of the compared things: everything from the first named thing to the last (the things and
    the words between them) becomes just that thing; a leading "compare" goes. "weather in Denver vs Salt Lake
    City" gives "weather in Salt Lake City" for index 1."""
    assert 0 <= index < len(entities), "an index into the things"
    assert isinstance(request, str), "request must be a string"
    flat = " ".join(request.split())
    low = flat.lower()
    spans = [(low.find(e.lower()), e) for e in entities]
    if any(at < 0 for at, _e in spans):
        return flat
    start = min(at for at, _e in spans)
    end = max(at + len(e) for at, e in spans)
    head = flat[:start].rstrip()
    for lead in _LEAD_WORDS:  # bounded: the lead-in table
        if head.lower().endswith(lead):
            head = head[: len(head) - len(lead)].rstrip()
    return " ".join(f"{head} {entities[index]} {flat[end:]}".split())


# ---- the sealed ``source.compare`` block -----------------------------------------------------------------

def is_compare(spec: object) -> bool:
    """True when the spec's source is a compare source."""
    source = spec.get("source") if isinstance(spec, dict) else None
    return isinstance(source, dict) and isinstance(source.get("compare"), dict)


def sealed(spec: dict) -> list[dict]:
    """The sealed ``[{name, url, path}]`` of a compare spec (``[]`` for any other card)."""
    assert isinstance(spec, dict), "spec must be a dict"
    block = (spec.get("source") or {}).get("compare")
    got = block.get("entities") if isinstance(block, dict) else None
    return [e for e in got if isinstance(e, dict)] if isinstance(got, list) else []


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    return parts.scheme.lower(), (parts.hostname or "").lower(), parts.port


def validate_source(source: dict) -> None:
    """Shape-check ``source.compare`` (raises ValueError): 2..4 distinct things (distinct names and distinct
    (address, path) readings), each with a printable name, a literal
    http(s) address with no parameter slot and a readable path; every address on the first one's scheme, host and
    port, and ``source.url`` is the first address. A compare source carries no key, header or contact email."""
    from . import ni
    assert isinstance(source, dict), "source must be a dict"
    block = source.get("compare")
    if not isinstance(block, dict) or set(block) != {"entities"}:
        raise ValueError("spec.source.compare keys are closed to ['entities']")
    items = block["entities"]
    if not isinstance(items, list) or not MIN_ENTITIES <= len(items) <= MAX_ENTITIES:
        raise ValueError(f"spec.source.compare.entities must hold {MIN_ENTITIES}..{MAX_ENTITIES} things")
    if source.get("headers") or source.get("secret_query") or source.get("contact_ua"):
        raise ValueError("spec.source.compare reads plain addresses: no headers, key or contact email")
    seen_names: set[str] = set()
    seen_reads: set[tuple[str, str]] = set()
    for i, item in enumerate(items):  # bounded: at most MAX_ENTITIES
        where = f"spec.source.compare.entities[{i}]"
        if not isinstance(item, dict) or set(item) != _ENTITY_KEYS:
            raise ValueError(f"{where} keys are closed to {sorted(_ENTITY_KEYS)}")
        name, url, path = item["name"], item["url"], item["path"]
        if not isinstance(name, str) or not 1 <= len(name) <= MAX_NAME or not name.isprintable() or not name.strip():
            raise ValueError(f"{where}.name must be one printable line of 1..{MAX_NAME} characters")
        if not isinstance(url, str) or len(url) > ni._MAX_URL or "{{" in url or any(c.isspace() for c in url):
            raise ValueError(f"{where}.url must be one literal address (no parameter slot)")
        ni._validate_http_json_url_shape(url)
        if not isinstance(path, str) or not path or len(path) > MAX_PATH:
            raise ValueError(f"{where}.path must be a path of 1..{MAX_PATH} characters")
        try:
            ni.parse_path(path)
        except (ni.NIError, ValueError):
            raise ValueError(f"{where}.path is not a readable path") from None
        key = " ".join(name.casefold().split())
        if key in seen_names or (url, path) in seen_reads:
            raise ValueError("spec.source.compare things and (address, path) readings must be distinct")
        seen_names.add(key)
        seen_reads.add((url, path))
    if items[0]["url"] != source.get("url"):
        raise ValueError("spec.source.url must be the first compared address")
    first = _origin(items[0]["url"])
    if any(_origin(e["url"]) != first for e in items):
        raise ValueError("spec.source.compare addresses must share one scheme, host and port")


# ---- the engine's fetch -------------------------------------------------------------------------------

def number_at(parsed: object, path: str) -> float | None:
    """The finite number at ``path`` in a parsed response (a numeric string counts), else None."""
    from . import ni
    assert isinstance(path, str), "path must be text"
    payload = {"items": parsed} if isinstance(parsed, list) else parsed
    try:
        raw = ni._resolve_path(payload, ni.parse_path(path))
    except (ni.NIError, ValueError, TypeError):
        return None
    if isinstance(raw, str):
        try:
            raw = float(raw.strip().replace(",", ""))
        except ValueError:
            return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        return None
    return float(raw) if not isinstance(raw, int) else raw


def payload_of(entities: list[dict], values: list[object]) -> dict:
    """The pipeline payload: the sealed names, in the order the user's question gave them, with their numbers."""
    assert len(entities) == len(values), "one value per thing"
    assert entities, "things required"
    return {ROWS: [{"name": e["name"], "value": v} for e, v in zip(entities, values, strict=True)]}


def fetch(spec_source: dict, get: Callable[[str], object]) -> dict:
    """Read every distinct compared address ONCE with ``get(url)`` and return the payload. A capacity refusal from
    ``get`` (a NIError of a capacity kind: the host is busy) defers the whole card at once; any other fetch failure
    (a NIError), or a response with no number at the thing's path, leaves that thing out and the run fails as
    ``compare_partial`` naming every one. Anything that is not a NIError is a bug and propagates."""
    from . import ni
    assert isinstance(spec_source, dict), "source must be a dict"
    assert callable(get), "a fetcher"
    items = [e for e in (spec_source.get("compare") or {}).get("entities") or [] if isinstance(e, dict)]
    responses: dict[str, tuple[object]] = {}   # url -> (parsed,), or () for an address that failed
    values: list[object] = []
    lost: list[str] = []
    for item in items[:MAX_ENTITIES]:  # bounded
        if item["url"] not in responses:
            try:
                responses[item["url"]] = (get(item["url"]),)
            except ni.NIError as exc:
                if exc.kind in ni.CAPACITY_KINDS:
                    raise
                responses[item["url"]] = ()   # a dead address is the absence of every thing it serves, named below
        got = number_at(responses[item["url"]][0], item["path"]) if responses[item["url"]] else None
        values.append(got)
        if got is None:
            lost.append(item["name"])
    if lost or not items:
        raise ni.NIError(PARTIAL, ", ".join(lost)[:200] or "no things")
    return payload_of(items, values)


# ---- the card ---------------------------------------------------------------------------------------

def pipeline() -> list[dict]:
    """The sealed stages: read the rows, make each value a number."""
    return [{"op": "extract", "paths": {ROWS: ROWS}},
            {"op": "transform", "apply": [{"fn": "number", "field": ROWS, "key": "value"}]}]


def record_spec(label: str, unit: str | None) -> dict:
    """The sealed ``form.record`` over the rows: the thing is the name, the number the value. Set by code, so no
    heuristic turns two rows into a hero value."""
    from .ni_forms.record import _number_field_type
    assert isinstance(label, str) and label, "a metric label"
    ftype, currency, unit_id = _number_field_type(unit)
    value = {"name": "value", "label": label[:MAX_LABEL], "path": "value", "type": ftype, "role": "value"}
    if currency:
        value["currency"] = currency
    if unit_id:
        value["unit"] = unit_id
    return {"kind": "records", "fields": [{"name": "name", "label": "Name", "path": "name", "type": "text",
                                           "role": "name"}, value], "rows": ROWS}


def clean_unit(unit: object) -> str | None:
    """A short unit label ("°F", "USD", "mph"), else None: one printable line, no markup."""
    if not isinstance(unit, str) or not unit.strip():
        return None
    text = " ".join(unit.split())
    return text if len(text) <= MAX_UNIT and _UNIT_RE.fullmatch(text) else None


def measure_label(request: str, entities: tuple[str, ...] | list[str]) -> str:
    """The value label of a researcher's compare, from the question's own words: the closed measure name the ask says
    ("humidity", "wind") when it names one, else "Value". Never text a model or a source returned."""
    assert isinstance(request, str), "request must be a string"
    from . import ni_compare_flow, ni_flow
    said = frozenset(w for e in entities for w in ni_compare_flow._words(e))
    named, _unreported = ni_flow._unbound_measures(request, [], said)
    return named[0].replace("_", " ").capitalize() if named else "Value"


def title_of(names_: list[str], label: str) -> str:
    """The card title: the metric and the things, in the user's words."""
    assert names_, "things required"
    assert label, "a metric label"
    return f"{label[:MAX_LABEL]}: {' vs '.join(names_)}"[:200]


def build(entities: list[dict], values: list[object], *, label: str, unit: str | None, title: str, ask: str,
          now: datetime, cadence_s: int, viewer_tz: str) -> dict | None:
    """The compare card for these things and numbers: ``{pipeline, scene, preview_payload}``, or None when the
    rows cannot be laid out as the ``compare`` form."""
    from . import ni
    from .ni_forms import record
    from .ni_forms.form_scene import design_record, frame_of
    assert len(entities) == len(values) >= MIN_ENTITIES, "a number per thing"
    assert now.tzinfo is not None, "now must be aware"
    stages = pipeline()
    try:
        outputs = ni.run_pipeline(copy.deepcopy(stages), payload_of(entities, values))
        frame = frame_of("compare", ["compare"])
        context = {"source_url": entities[0]["url"], "viewer_tz": viewer_tz, "title": title, "ask": ask,
                   "cadence_s": cadence_s, "fetched_at": now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "question_kind": "compare", "wants": ["compare"]}
        rec, inp = record.from_spec(record_spec(label, unit), outputs, history=None, context=context)
        scene = design_record(rec, inp, now=now, rows_output_name=ROWS, frame=frame, prefer_form="compare").node
    except (ni.NIError, ValueError, KeyError, TypeError, AssertionError):
        return None
    if scene.get("form") != "compare":
        return None
    return {"pipeline": stages, "scene": scene, "preview_payload": outputs}


# ---- the check: every asked thing shown with its own value ---------------------------------------------

def _has_number(row: dict) -> bool:
    for v in list(row.values())[:60]:  # bounded: one row's cells
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)) and math.isfinite(v):
            return True
        if isinstance(v, str) and len(v) <= 40:
            try:
                float(v.strip().replace(",", ""))
                return True
            except ValueError:
                continue
    return False


def _rows_of(outputs: object) -> list[dict]:
    """Every row (an object in a list) among the outputs, bounded."""
    out: list[dict] = []
    if not isinstance(outputs, dict):
        return out
    for value in list(outputs.values())[:40]:  # bounded: the outputs
        if isinstance(value, list):
            out.extend(r for r in value[:500] if isinstance(r, dict))
    return out[:1000]


def _cells(row: dict) -> list[str]:
    return [v for v in list(row.values())[:60] if isinstance(v, str) and len(v) <= 400]


def shown(entities: list[str] | tuple[str, ...], outputs: object) -> tuple[list[str], list[str]]:
    """``(shown, missing)`` of the asked things on a card's outputs. A thing is shown when a row names it in a text
    cell and has a number of its own, or when one row names every asked thing (a head-to-head). Rows only: a card
    that names the things in its title but holds one reading shows one thing."""
    assert isinstance(outputs, (dict, type(None))), "outputs must be a dict"
    assert all(isinstance(e, str) and e for e in entities), "named things"
    rows = _rows_of(outputs)
    together = any(_has_number(r) and all(any(names(e, c) for c in _cells(r)) for e in entities) for r in rows)
    with_value = [e for e in entities
                  if together or any(_has_number(r) and any(names(e, c) for c in _cells(r)) for r in rows)]
    return with_value, [e for e in entities if e not in with_value]


def reasons(entities: list[str] | tuple[str, ...], outputs: object) -> list[str]:
    """Why the outputs do not show every asked thing with its own value ([] when they do or nothing is asked)."""
    assert isinstance(entities, (list, tuple)), "things required"
    if len(entities) < MIN_ENTITIES:
        return []
    if len(entities) > MAX_ENTITIES:
        return [f"it compares {len(entities)} things; a card compares up to {MAX_ENTITIES}"]
    _have, missing = shown(entities, outputs)
    if not missing:
        return []
    return [f"it shows {len(entities) - len(missing)} of {len(entities)} compared ({', '.join(missing)} missing)"]


def refusal(spec: dict, outputs: object) -> str | None:
    """Why a card must not be built for this ask, else None. The build check every path runs: an ask that compares N
    things lands on a card that shows all N, each with its own value (a one-thing card is a wrong card)."""
    assert isinstance(spec, dict), "spec must be a dict"
    wanted = asked(str(spec.get("goal") or ""))
    if not wanted:
        return None
    if len(wanted) > MAX_ENTITIES:
        return f"can't compare these yet: that is {len(wanted)} things and a card compares up to {MAX_ENTITIES}"
    found = reasons(wanted, outputs)
    return f"can't compare these yet: {found[0]}" if found else None


def drift(spec: dict, outputs: object) -> list[str]:
    """The refresh re-check of a compare card (any build path): its fresh outputs still show every thing it was
    kept with, each with its own value, and every thing the ask names. [] when they do or the card is not one."""
    assert isinstance(spec, dict), "spec must be a dict"
    if not is_compare(spec):
        return []
    kept = [e["name"] for e in sealed(spec)]
    found = reasons(kept, outputs)
    wanted = asked(str(spec.get("goal") or ""))
    if wanted:
        found += [r for r in reasons(wanted, outputs) if r not in found]
    return [f"compare: {r}" for r in found]
