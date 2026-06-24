"""Editable catalog for the local LLM shootout: which models to test, the prompt
suite, and the tool-calling probe. Edit this file to change what gets benchmarked.

Sizes are approximate 4-bit/quantized on-disk footprints (GB) to sanity-check the
"fits comfortably in <= 20 GB" rule and the host's free disk before pulling.
"""

from __future__ import annotations

# --- Ollama models (pulled automatically with `ollama pull <tag>`) ------------
# tag -> approx size GB (4-bit). With pull-test-reap (default), models pulled by this run
# are deleted after testing, so the catalog can be broad — only ~one model + the
# pre-installed ones occupy disk at a time. Every entry is <= 20 GB. Curated toward
# instruct models with real tool-calling, spanning 2-20 GB.
OLLAMA_MODELS: dict[str, float] = {
    "llama3.2:3b": 2.0,
    "qwen3:4b": 2.6,
    "qwen3:8b": 5.2,
    "qwen3:14b": 9.3,
    "qwen2.5:7b-instruct": 4.7,
    "qwen2.5:14b-instruct": 9.0,
    "qwen2.5:32b-instruct": 19.9,        # ceiling test (~20 GB)
    "qwen2.5-coder:7b-instruct": 4.7,
    "qwen2.5-coder:14b-instruct": 9.0,
    "llama3.1:8b": 4.9,
    "gemma2:9b-instruct-q4_K_M": 5.8,
    "mistral-nemo:12b": 7.1,
    "mistral-small:22b": 13.0,
    "phi4:14b": 9.1,
}

# --- oMLX models -------------------------------------------------------------
# oMLX has no scripted pull here (models are loaded in oMLX itself), so by default
# the runner benchmarks whatever chat models /v1/models reports. To pin a subset,
# list exact ids here; empty == auto-discover from the server.
OMLX_MODELS: list[str] = []

# --- Prompt suite (robust, diverse; each run REPEATS times, median taken) -----
# Kept short and deterministic (temperature 0) so throughput/latency are comparable.
PROMPTS: list[dict] = [
    {"key": "factual", "text": "In one sentence, what is the capital of Australia?"},
    {"key": "reasoning",
     "text": "A bat and a ball cost $1.10 total. The bat costs $1.00 more than the ball. "
             "How much does the ball cost? Answer with just the amount."},
    {"key": "instruction",
     "text": "List exactly three primary colors as a comma-separated line, nothing else."},
    {"key": "summarize",
     "text": "Summarize in one sentence: A transformer is a neural network architecture that "
             "uses self-attention to weigh the influence of different input tokens when producing "
             "each output token, enabling parallel training and strong long-range modeling."},
]

# A simple, unambiguous correctness check per prompt (substring, lowercased). None = skip.
PROMPT_EXPECT: dict[str, str | None] = {
    "factual": "canberra",
    "reasoning": "0.05",       # the classic answer is 5 cents, not 10
    "instruction": None,        # graded loosely (3 comma-separated items)
    "summarize": None,
}

# --- Tool-calling probe (THE key metric for an agentic assistant) ------------
# Does the model emit a STRUCTURED tool_call (not text JSON) for an obvious request?
TOOL_SPEC: list[dict] = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"location": {"type": "string", "description": "City name"}},
            "required": ["location"],
        },
    },
}]
TOOL_PROMPT = "What is the current weather in Paris? Use the get_weather tool."
EXPECTED_TOOL = "get_weather"
