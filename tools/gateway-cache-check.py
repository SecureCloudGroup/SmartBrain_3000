#!/usr/bin/env python3
"""MANUAL check: does Bifrost forward Anthropic prompt caching (cache_control)?

Run against a real Anthropic key configured in a running Bifrost:

    PYTHONPATH=app python tools/gateway-cache-check.py \
        [--url http://127.0.0.1:38080] [--model anthropic/claude-sonnet-4-5]

It sends the SAME request twice through the app's real gateway code, with a long
static system block marked as a cache breakpoint. PASS means the second response
reports ``cache_read_input_tokens > 0``. FAIL (exit 1) with a first-call
``cache_creation_input_tokens`` of 0 means Bifrost dropped cache_control (or the
prefix was under the model's minimum cacheable size, 512-4096 tokens by model).
Costs a few cents. Never run by the test suite.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from smartbrain_3000 import gateway

# ~6k tokens of stable filler: above the highest per-model minimum (4096).
_STATIC = "\n".join(f"Rule {i}: always answer concisely and cite the rule number {i}." for i in range(900))


def _cache_fields(resp: dict) -> dict:
    usage = resp.get("usage") or {}
    return {k: usage.get(k) for k in ("input_tokens", "prompt_tokens", "cache_creation_input_tokens",
                                      "cache_read_input_tokens", "completion_tokens")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default=os.environ.get("SMARTBRAIN_LLM_GATEWAY_URL", gateway.DEFAULT_GATEWAY_URL))
    ap.add_argument("--model", default="anthropic/claude-sonnet-4-5")
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
    read = seen[1].get("cache_read_input_tokens") or 0
    print("PASS: cache hit on second request" if read > 0 else
          "FAIL: no cache_read_input_tokens on the second request (cache_control not forwarded?)")
    return 0 if read > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
