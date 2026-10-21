#!/usr/bin/env python3
"""MANUAL check: does Bifrost forward Anthropic prompt caching (cache_control)?

Run against a real Anthropic key configured in a running Bifrost:

    PYTHONPATH=app python tools/gateway-cache-check.py \
        [--url http://127.0.0.1:38080] [--model anthropic/claude-sonnet-5-5]

It sends the SAME request twice through the app's real gateway code, with a long
static system block marked as a cache breakpoint. PASS means the second response
reports cache-read tokens > 0. Bifrost's OpenAI-compatible endpoint reports Anthropic's
cache counts in ``usage.prompt_tokens_details`` (``cached_read_tokens`` /
``cached_write_tokens``), not as top-level ``cache_*_input_tokens``; both shapes are read
through ``usage.cache_tokens``, the same code the cost view uses. FAIL (exit 1) with no
cache write on the first call means Bifrost dropped cache_control (or the prefix was under
the model's minimum cacheable size, 1024-4096 tokens by model). ``prompt_tokens`` is the
total INCLUDING cached tokens, so it is the same on both calls either way.
Costs a few cents. Never run by the test suite.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from smartbrain_3000 import gateway, usage

# ~7k tokens of stable filler (about 23 tokens a line): above the highest per-model minimum (4096).
_STATIC = "\n".join(f"Rule {i}: always answer concisely and cite the rule number {i}." for i in range(300))


def _cache_fields(resp: dict) -> dict:
    block = resp.get("usage") or {}
    created, read = usage.cache_tokens(block)
    return {"prompt_tokens": block.get("prompt_tokens"), "cache_write": created, "cache_read": read,
            "completion_tokens": block.get("completion_tokens"),
            "prompt_tokens_details": block.get("prompt_tokens_details")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default=os.environ.get("SMARTBRAIN_LLM_GATEWAY_URL", gateway.DEFAULT_GATEWAY_URL))
    ap.add_argument("--model", default="anthropic/claude-sonnet-5-5")
    args = ap.parse_args()
    assert args.model.startswith("anthropic/"), "this check is for anthropic/* models"
    os.environ["SMARTBRAIN_LLM_GATEWAY_URL"] = args.url
    messages = [
        {"role": "system", "content": [{"type": "text", "text": _STATIC, "cache": True}]},
        {"role": "user", "content": "Reply with the single word: ok"},
    ]
    seen = []
    for label in ("first", "second"):
        resp = gateway.chat(messages, args.model, timeout=120)
        seen.append(_cache_fields(resp))
        print(label, json.dumps(seen[-1]))
    read = seen[1]["cache_read"]
    print("PASS: cache hit on second request" if read > 0 else
          "FAIL: no cache read on the second request" +
          (" and no cache write on the first (cache_control not forwarded?)" if not seen[0]["cache_write"] else ""))
    return 0 if read > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
