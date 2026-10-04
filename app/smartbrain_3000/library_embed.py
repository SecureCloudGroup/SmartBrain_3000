"""Locate v2's vectors: the Library's reviewed sources and example asks, embedded by the user's own embedder.

The Library pack ships TEXT (each source's record, ``library_source_asks`` and ``library_route_asks``); this
module embeds it with the same model the Knowledge base uses (the routed ``embedding`` capability through
``gateway.embed``, its task prefixes and its ``#tp1`` storage identity) and keeps the vectors in a plaintext
sidecar next to the installed pack (public Library text, no user data). The sidecar is keyed by the pack's
sha256 and the embedder's storage identity: a new pack or a new embedder rebuilds it in the background, as
the Knowledge base re-embeds on a model change. Until it is ready, or when no embedder is wired or one
fails, locate ranks exactly as it did before (``ready`` returns None).

Three deterministic outputs per ask (one ask embed, then in-process math):
- ``route``: nearest per-subcategory centroid of the route asks -> {route, runner_up, gap, confident}; the
  gap threshold is fitted on the route asks themselves (leave-one-out), never on an eval set;
- ``dense``: sources ranked by max(cos(ask, capability card), max cos(ask, the source's example asks));
- ``fuse``: reciprocal-rank fusion of the curated keyword ranking and ``dense``, plus the route's boost.
"""

from __future__ import annotations

import io
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import duckdb
import numpy as np

from . import gateway

log = logging.getLogger(__name__)

SIDECAR = "locate-vectors.npz"
FORMAT = 1                # bump to rebuild every sidecar under a new layout or card recipe
RRF_K = 60                # reciprocal-rank fusion constant (the spike's)
RRF_DEPTH = 100           # each ranking contributes its top 100
ROUTE_ACCURACY = 0.95     # a route is confident above the smallest gap whose leave-one-out accuracy reaches this
# the route's boost, in first-place RRF votes, for the top route's members, scaled by the route's confidence
# (gap / threshold, capped at 1). Fitted on the route asks (leave-one-out routed; target: a source of their own
# subcategory in the fused top 3): 0.25 / 0.5 / 1 / 2 / 4 votes -> .912 / .909 / .915 / .907 / .902 (none: .903);
# a top-2 boost (runner-up scaled by doubt, or flat) measured within .003 of this and isn't confidence-weighted
ROUTE_BOOST = 1.0
MAX_CARD_CHARS = 1500     # a source's declared coverage is cut here (the spike's card A)
# a source this similar to the ask (B-max) is about it whatever the route says. Fitted leave-one-out on the
# Library's route asks and source asks (all their (ask, source) pairs, the ask removed from its owner's asks): the
# smallest similarity at which a source is filed under the ask's own subcategory 95% of the time. Measured on the
# live embedder (modernbert-embed): .90 -> 0.760, .95 -> 0.828, .98 -> 0.892
FLOOR = 0.828
RETRY_SECONDS = 600.0     # after a failed build, wait this long before trying again
QUERY_TIMEOUT = 15.0      # one ask embed; a failure falls back to the keyword ranking
YIELD_SLEEP, YIELD_POLLS = 0.05, 2400   # a build waits up to 2 minutes per text for a busy local model


@dataclass(frozen=True)
class Embedder:
    """What locate needs from an embedder: the routed model id and a one-text embed (task 'document'/'query')."""
    model: str
    embed_one: Callable[[str, str], list[float]]

    @property
    def scheme(self) -> str:
        """The storage identity of its vectors (the Knowledge base's: model id plus the prefix marker)."""
        return gateway.embedding_scheme(self.model)


def gateway_embedder(model: str) -> Embedder:
    """The Knowledge base's embedder: ``gateway.embed`` with the model's task prefixes."""
    assert model and "/" in model, "embed model must be 'provider/model'"

    def embed_one(text: str, task: str) -> list[float]:
        return gateway.embed(text, model, task=task, timeout=QUERY_TIMEOUT)
    return Embedder(model, embed_one)


_PROVIDER: Callable[[], Embedder | None] | None = None


def set_provider(provider: Callable[[], Embedder | None] | None) -> None:
    """Install the embedder factory (app startup: the routed Knowledge embedder; tests: a stub)."""
    global _PROVIDER
    assert provider is None or callable(provider), "provider must be callable"
    _PROVIDER = provider


def current_embedder() -> Embedder | None:
    """The wired embedder, or None (unwired, or the factory failed)."""
    if _PROVIDER is None:
        return None
    try:
        return _PROVIDER()
    except Exception as exc:  # a broken factory means no vectors, never a broken locate
        log.info("library_embed: no embedder (%s)", type(exc).__name__)
        return None


# --- the text that gets embedded ------------------------------------------------------------------------

def card(record: dict, labels: dict[str, str]) -> str:
    """A source's capability card (the spike's card A): name and description, examples, declared answers,
    kinds, category labels and declared coverage."""
    parts = [f"{record.get('name', '')}. {str(record.get('description') or '').strip()}"]
    ex = [e for e in record.get("examples") or [] if isinstance(e, str)]
    if ex:
        parts.append("Examples: " + "; ".join(ex))
    ans = []
    for a in record.get("answers") or []:  # bounded by the Library's answers limit
        if isinstance(a, dict):
            words = ", ".join(str(w) for w in a.get("words") or [])
            ans.append(f"{a.get('label', '')}" + (f" ({words})" if words else ""))
    if ans:
        parts.append("Answers: " + "; ".join(ans))
    kinds = [k.replace("_", " ") for k in record.get("kinds") or []]
    if kinds:
        parts.append("Kinds: " + ", ".join(kinds))
    cats = [labels.get(c, c).replace(" › ", " - ") for c in record.get("categories") or []]
    if cats:
        parts.append("Categories: " + "; ".join(cats))
    ent = str((record.get("coverage") or {}).get("entity") or "").strip()
    if ent:
        parts.append("Covers: " + ent[:MAX_CARD_CHARS])
    return "\n".join(parts)


def _rows(con, sql: str) -> list[tuple]:
    try:
        return con.execute(sql).fetchall()
    except duckdb.CatalogException:  # a pack from before locate v2 has no example asks
        return []


def texts(con) -> dict:
    """Everything a sidecar embeds, in a fixed order: {ids, cats, cards, asks: [(owner, ask)], routes: [(route,
    ask)]}. The pool is the reviewed, offerable sources (not harvested, a helper, failed or refused)."""
    labels = {f"{c}/{s}": lab for c, s, lab in con.execute(
        "SELECT category, subcategory, label FROM library_taxonomy").fetchall()}
    rows = con.execute("SELECT id, record FROM library_sources WHERE tier <> 'harvested' AND role <> 'helper' "
                       "AND validation_status NOT IN ('failed', 'refused') ORDER BY id").fetchall()
    ids, cats, cards = [], [], []
    for sid, raw in rows:  # bounded by the pack
        rec = json.loads(raw)
        ids.append(sid)
        cats.append("|".join(rec.get("categories") or []))
        cards.append(card(rec, labels))
    pos = {sid: i for i, sid in enumerate(ids)}
    asks = [(pos[s], a) for s, a in _rows(con, "SELECT source_id, ask FROM library_source_asks ORDER BY source_id, ask")
            if s in pos and a]
    routes = [(f"{c}/{s}", a) for c, s, a in _rows(
        con, "SELECT category, subcategory, ask FROM library_route_asks ORDER BY category, subcategory, ask") if a]
    return {"ids": ids, "cats": cats, "cards": cards, "asks": asks, "routes": routes}


# --- the vectors ----------------------------------------------------------------------------------------

def _unit(m: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(m, axis=-1, keepdims=True)
    return m / np.maximum(n, 1e-12)


def gap_threshold(vecs: np.ndarray, labels: list[str], target: float = ROUTE_ACCURACY) -> float:
    """Leave-one-out over the route asks: each ask is routed against centroids built without it; the threshold
    is the smallest gap whose asks at or above it are routed right at least ``target`` of the time (inf when
    none is)."""
    names = sorted(set(labels))
    idx = np.array([names.index(x) for x in labels])
    sums = np.stack([vecs[idx == j].sum(0) for j in range(len(names))])
    counts = np.bincount(idx, minlength=len(names))
    full = _unit(sums)
    rows = []
    for i, v in enumerate(vecs):  # bounded by the route asks
        s = full @ v
        own = idx[i]
        if counts[own] > 1:
            s[own] = float(_unit(sums[own] - v) @ v)
        else:
            s[own] = -np.inf  # a route of one ask can't be predicted without it
        o = np.argsort(-s)
        rows.append((float(s[o[0]] - s[o[1]]), bool(o[0] == own)))
    rows.sort()
    ok = np.array([r[1] for r in rows], dtype=float)
    tail = np.cumsum(ok[::-1])[::-1] / np.arange(len(ok), 0, -1)  # accuracy of the asks at or above each gap
    hit = np.nonzero(tail >= target)[0]
    return rows[int(hit[0])][0] if len(hit) else float("inf")


@dataclass
class Vectors:
    key: dict                 # {"format", "pack", "model"}: what these vectors were built from
    ids: list[str]            # the source pool, in card order
    cats: list[set[str]]      # each source's subcategories
    cards: np.ndarray         # (sources, dim) unit vectors, embedded as documents
    asks: np.ndarray          # (asks, dim) unit vectors, embedded as queries
    owner: np.ndarray         # (asks,) index into ids
    routes: list[str]         # "category/subcategory" per centroid
    centroids: np.ndarray     # (routes, dim) unit vectors
    threshold: float          # the confident-route gap (leave-one-out on the route asks)

    def route(self, q: np.ndarray) -> dict | None:
        """{route, runner_up, gap, confident, behind} for a unit ask vector, or None without route asks.
        ``behind`` is how far each route's centroid trails the top one; a route ``threshold`` or more behind is
        confidently not the ask's."""
        if len(self.routes) < 2:
            return None
        s = self.centroids @ q
        o = np.argsort(-s)
        gap = float(s[o[0]] - s[o[1]])
        return {"route": self.routes[o[0]], "runner_up": self.routes[o[1]], "gap": gap,
                "confident": gap >= self.threshold, "threshold": self.threshold,
                "behind": {r: float(s[o[0]] - x) for r, x in zip(self.routes, s, strict=True)}}

    def bmax(self, q: np.ndarray) -> np.ndarray:
        """Each pool source's B-max similarity: max(cos(ask, card), max cos(ask, the source's example asks))."""
        m = self.cards @ q
        if len(self.owner):
            np.maximum.at(m, self.owner, self.asks @ q)
        return m

    def dense(self, q: np.ndarray) -> list[str]:
        """The pool ranked by B-max."""
        return [self.ids[i] for i in np.argsort(-self.bmax(q), kind="stable")]

    def nearest(self, q: np.ndarray, sub: str) -> str | None:
        """The source filed under ``sub`` with the highest B-max, or None when none is."""
        m = self.bmax(q)
        members = [i for i, c in enumerate(self.cats) if sub in c]
        return self.ids[max(members, key=lambda i: m[i])] if members else None

    def similar(self, q: np.ndarray) -> set[str]:
        """The sources whose B-max reaches ``FLOOR``: near enough to the ask to be about it."""
        return {self.ids[i] for i in np.nonzero(self.bmax(q) >= FLOOR)[0]}

    def fuse(self, keyword: list[str], dense: list[str], route: dict | None) -> list[str]:
        """RRF of the two rankings (top ``RRF_DEPTH`` each), plus the route's boost: the top route's members get
        ``ROUTE_BOOST`` first-place votes scaled by the route's confidence (a near tie adds next to nothing). A
        boost never removes anything."""
        score: dict[str, float] = {}
        for ranking in (keyword, dense):
            for pos, sid in enumerate(ranking[:RRF_DEPTH]):
                score[sid] = score.get(sid, 0.0) + 1.0 / (RRF_K + pos + 1)
        if route is not None:
            vote = ROUTE_BOOST / (RRF_K + 1)
            sure = 1.0 if route["confident"] or self.threshold <= 0 else max(0.0, route["gap"] / self.threshold)
            cats = dict(zip(self.ids, self.cats, strict=True))
            for sid in score:  # bounded by 2 * RRF_DEPTH
                if route["route"] in cats.get(sid, set()):
                    score[sid] += vote * sure
        order = {sid: i for i, sid in enumerate(keyword[:RRF_DEPTH])}
        return sorted(score, key=lambda s: (-score[s], order.get(s, RRF_DEPTH)))


def _embed_all(emb: Embedder, items: list[str], task: str) -> np.ndarray:
    out = []
    for text in items:  # bounded by the pack
        # a build gives way to a foreground call on the local model between texts (its semaphore has no
        # queue fairness: a waiting chat could keep losing the race to the next embed)
        for _ in range(YIELD_POLLS):
            if gateway.local_available():
                break
            time.sleep(YIELD_SLEEP)
        v = emb.embed_one(text, task)
        if out and len(v) != len(out[0]):
            raise ValueError("the embedder returned vectors of different sizes")
        out.append(v)
    return _unit(np.asarray(out, dtype=np.float32)) if out else np.zeros((0, 0), dtype=np.float32)


def build(con, key: dict, emb: Embedder) -> Vectors:
    """Embed the pack's cards, source asks and route asks (one text at a time, as the Knowledge base does) and
    fit the route threshold. Raises on any embed failure: a sidecar is whole or absent."""
    t = texts(con)
    assert t["ids"], "the pack has no offerable reviewed sources"
    cards = _embed_all(emb, t["cards"], "document")
    dim = cards.shape[1]
    asks = _embed_all(emb, [a for _, a in t["asks"]], "query") if t["asks"] else np.zeros((0, dim), np.float32)
    owner = np.array([o for o, _ in t["asks"]], dtype=np.int32)
    routes, centroids, threshold = [], np.zeros((0, dim), np.float32), float("inf")
    if t["routes"]:
        rv = _embed_all(emb, [a for _, a in t["routes"]], "query")
        labels = [r for r, _ in t["routes"]]
        if asks.shape[1:] != rv.shape[1:] or rv.shape[1] != dim:
            raise ValueError("the embedder returned vectors of different sizes")
        routes = sorted(set(labels))
        centroids = _unit(np.stack([rv[[i for i, x in enumerate(labels) if x == r]].sum(0) for r in routes]))
        threshold = gap_threshold(rv, labels) if len(routes) > 1 else float("inf")
    return Vectors(key=key, ids=t["ids"], cats=[set(c.split("|")) - {""} for c in t["cats"]], cards=cards,
                   asks=asks, owner=owner, routes=routes, centroids=centroids.astype(np.float32),
                   threshold=threshold)


def save(path: Path, v: Vectors) -> None:
    """Write the sidecar atomically (a reader sees the old file or the whole new one)."""
    meta = {**v.key, "threshold": v.threshold if np.isfinite(v.threshold) else None}
    buf = io.BytesIO()
    np.savez(buf, meta=np.array(json.dumps(meta)), ids=np.array(v.ids), cats=np.array(["|".join(sorted(c))
             for c in v.cats]), cards=v.cards, asks=v.asks, owner=v.owner, routes=np.array(v.routes, dtype=str),
             centroids=v.centroids)
    tmp = path.with_name(f".{path.name}-{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_bytes(buf.getvalue())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _meta(z) -> dict:
    return json.loads(str(z["meta"]))


def load(path: Path, key: dict) -> Vectors | None:
    """The sidecar if it was built from ``key`` (this pack, this embedder, this format), else None."""
    try:
        with np.load(path, allow_pickle=False) as z:
            meta = _meta(z)
            if {k: meta.get(k) for k in key} != key:
                return None
            ids = [str(x) for x in z["ids"]]
            cards, asks, owner = z["cards"], z["asks"], z["owner"]
            routes, centroids = [str(x) for x in z["routes"]], z["centroids"]
            cats = [set(str(c).split("|")) - {""} for c in z["cats"]]
    except (OSError, ValueError, KeyError) as exc:
        if not isinstance(exc, FileNotFoundError):
            log.info("library_embed: unreadable sidecar (%s)", type(exc).__name__)
        return None
    if len(ids) != len(cards) or len(cats) != len(ids) or len(owner) != len(asks) or len(routes) != len(centroids):
        return None
    th = meta.get("threshold")
    return Vectors(key=key, ids=ids, cats=cats, cards=cards, asks=asks, owner=owner, routes=routes,
                   centroids=centroids, threshold=float("inf") if th is None else float(th))


# --- readiness: load, or build in the background ----------------------------------------------------------

_LOCK = threading.Lock()
_STATE: dict[str, dict] = {}   # sidecar path -> {"vectors", "building", "failed_at"}


def _key(pack_sha: str, emb: Embedder) -> dict:
    return {"format": FORMAT, "pack": pack_sha, "model": emb.scheme}


def _build_into(path: Path, key: dict, emb: Embedder, open_con: Callable[[], object]) -> Vectors | None:
    st = _STATE[str(path)]
    t0 = time.monotonic()
    try:
        with open_con() as con:
            v = build(con, key, emb)
        save(path, v)
        log.info("library_embed: %d sources, %d asks, %d routes embedded in %.0fs", len(v.ids), len(v.asks),
                 len(v.routes), time.monotonic() - t0)
        with _LOCK:
            st["vectors"], st["failed_at"] = v, None
        return v
    except Exception as exc:  # embeddings are optional: locate keeps its keyword ranking
        log.warning("library_embed: building the Library's vectors failed (%s)", type(exc).__name__)
        with _LOCK:
            st["failed_at"] = time.monotonic()
        return None
    finally:
        with _LOCK:
            st["building"] = False


def ready(lib_dir: Path, pack_sha: str, open_con: Callable[[], object], *,
          wait: bool = False) -> tuple[Vectors, Embedder] | None:
    """The vectors for this pack and the wired embedder, plus that embedder; None until they exist. A missing or
    stale sidecar starts one background build (``wait``: build in the caller's thread instead)."""
    emb = current_embedder()
    if emb is None or not pack_sha:
        return None
    path = Path(lib_dir) / SIDECAR
    key = _key(pack_sha, emb)
    with _LOCK:
        st = _STATE.setdefault(str(path), {"vectors": None, "building": False, "failed_at": None})
        v = st["vectors"]
        if v is None or v.key != key:
            v = st["vectors"] = load(path, key)
        if v is not None:
            return v, emb
        failed = st["failed_at"] is not None and time.monotonic() - st["failed_at"] < RETRY_SECONDS
        if st["building"] or (failed and not wait):
            return None
        st["building"] = True
    if wait:
        v = _build_into(path, key, emb, open_con)
        return (v, emb) if v is not None else None
    threading.Thread(target=_build_into, args=(path, key, emb, open_con), name="library-embed",
                     daemon=True).start()
    return None


def embed_query(emb: Embedder, ask: str) -> np.ndarray | None:
    """The ask as a unit query vector, or None when the embedder fails (locate then ranks by keywords)."""
    try:
        v = np.asarray(emb.embed_one(ask, "query"), dtype=np.float32)
    except Exception as exc:
        log.info("library_embed: ask embed failed (%s)", type(exc).__name__)
        return None
    return v / n if v.ndim == 1 and (n := float(np.linalg.norm(v))) > 0 else None


def forget() -> None:
    """Drop the in-memory vectors (tests; a reinstall)."""
    with _LOCK:
        _STATE.clear()
