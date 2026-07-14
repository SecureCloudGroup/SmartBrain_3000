"""Opt-in real embedding-model validation for Knowledge semantic search.

The deterministic suite (test_knowledge_pdf_e2e) fakes embeddings, so it proves the index
plumbing but not that a real model does meaning-based retrieval. This one drives the app's
real pipeline (ingest.from_file -> kb.add -> ingest.embed_doc -> kb.semantic_search) with a
LIVE embeddings endpoint and queries that are paraphrases sharing NO content words with the
documents — so only genuine embeddings can answer them, not keyword search.

Skipped unless a real OpenAI-compatible endpoint is configured via env:
  SMARTBRAIN_TEST_EMBED_URL    e.g. http://localhost:8888  (host.docker.internal:8888 in Docker)
  SMARTBRAIN_TEST_EMBED_MODEL  e.g. bge-m3-mlx-fp16
  SMARTBRAIN_TEST_EMBED_KEY    optional Bearer key

Run on demand (MLX bge-m3 on the host; from the repo root):
  KEY=$(cat "$SCRATCH/mlx_key")
  docker run --rm -e SMARTBRAIN_TEST_EMBED_URL=http://host.docker.internal:8888 \
    -e SMARTBRAIN_TEST_EMBED_MODEL=bge-m3-mlx-fp16 -e SMARTBRAIN_TEST_EMBED_KEY="$KEY" \
    -v "$PWD/app:/app" --entrypoint /bin/sh smartbrain_3000:dev \
    -c 'pip install -q pytest httpx; cd /app && python -m pytest -q tests/test_knowledge_real_embed.py -s'
"""

from __future__ import annotations

import os

import duckdb
import httpx
import pytest
from _pdfgen import make_pdf

from smartbrain_3000 import db as dbmod
from smartbrain_3000 import gateway, ingest
from smartbrain_3000.kb import KnowledgeBase
from smartbrain_3000.secrets import gen_master_key

_URL = os.environ.get("SMARTBRAIN_TEST_EMBED_URL")
_MODEL = os.environ.get("SMARTBRAIN_TEST_EMBED_MODEL")
_KEY = os.environ.get("SMARTBRAIN_TEST_EMBED_KEY", "")

pytestmark = pytest.mark.skipif(
    not (_URL and _MODEL),
    reason="set SMARTBRAIN_TEST_EMBED_URL + SMARTBRAIN_TEST_EMBED_MODEL (+ _KEY) to run the real-embed test",
)

# Distinct topics; each QUERY is a paraphrase of its DOC that shares no content words with it,
# so retrieving the right passage requires real semantic understanding.
_DOCS = {
    "maritime": "The cargo vessel departed the harbor at dawn, its hold laden with grain, "
                "and set course across the open sea toward a distant continent.",
    "medicine": "The physician prescribed a course of antibiotics to treat the patient's "
                "stubborn bacterial infection and advised several days of bed rest.",
    "finance": "Quarterly revenue rose sharply this period as the corporation expanded "
               "aggressively into a handful of new overseas markets.",
    "cooking": "Gently simmer the sliced onions in olive oil until golden, then stir in "
               "minced garlic and a generous handful of fresh chopped herbs.",
    "astronomy": "The telescope captured faint light from a galaxy billions of light years "
                 "away, revealing ancient stars that formed soon after the universe began.",
}
_QUERIES = {
    "maritime": "at what time of day did the ship leave port for its ocean voyage",
    "medicine": "which drug did the doctor recommend for the sick person's illness",
    "finance": "how did the firm's earnings increase over the last three months",
    "cooking": "how should I prepare the vegetables before adding them to the dish",
    "astronomy": "what did the observatory detect from a very old distant star cluster",
}


def _real_embed(text, model=None, *, client=None, timeout=180.0):
    """Call the configured OpenAI-compatible /v1/embeddings endpoint. Signature-compatible with
    gateway.embed so it can stand in for it."""
    headers = {"Authorization": f"Bearer {_KEY}"} if _KEY else {}
    with httpx.Client(timeout=timeout) as client_:
        resp = client_.post(
            f"{_URL.rstrip('/')}/v1/embeddings",
            headers=headers,
            json={"model": _MODEL, "input": text},
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]


def test_real_model_paraphrase_semantic_retrieval(monkeypatch) -> None:
    try:
        probe = _real_embed("reachability probe")
    except Exception as exc:  # endpoint configured but down — skip, don't fail the run
        pytest.skip(f"embed endpoint {_URL} unreachable: {exc}")
    assert len(probe) >= 8, "embedding vector looks too small to be a real model"
    monkeypatch.setattr(gateway, "embed", _real_embed)  # real vectors flow through the app pipeline

    conn = duckdb.connect(":memory:")
    dbmod.run_migrations(conn)
    kb = KnowledgeBase(conn, gen_master_key())
    for topic, passage in _DOCS.items():
        _, text, _meta = ingest.from_file(f"{topic}.pdf", make_pdf([passage]))
        did = kb.add(topic, text)
        ingest.embed_doc(kb, did, topic, text, _MODEL)  # chunk + embed + store (real code)

    misses = []
    print(f"\nreal embed model: {_MODEL}  (dim {len(probe)})")
    for topic, query in _QUERIES.items():
        hits = kb.semantic_search(_real_embed(query), _MODEL, limit=len(_DOCS))
        assert hits, f"no semantic hits for the {topic!r} paraphrase"
        top = hits[0]["title"]
        margin = hits[0]["score"] - (hits[1]["score"] if len(hits) > 1 else 0.0)
        print(f"  paraphrase→{topic:10s} top={top:10s} score={hits[0]['score']:.3f} margin={margin:+.3f}"
              f"  {'OK' if top == topic else 'WRONG'}")
        if top != topic:
            misses.append((topic, top))
    assert not misses, f"paraphrase retrieval ranked the wrong passage first: {misses}"
