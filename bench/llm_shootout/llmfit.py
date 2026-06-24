"""llmfit integration: hardware-aware model selection + predicted-vs-empirical annotation.

Wraps the optional `llmfit` CLI ("right-size LLM models to your hardware") to (a) select
which models are worth testing (fit + tool-use), and (b) show llmfit's predicted score /
tok-s / fit next to each empirically-measured result.

Pure stdlib; degrades to empty results if llmfit isn't installed.
"""

from __future__ import annotations

import json
import re
import subprocess


def available() -> bool:
    """True if the `llmfit` CLI is installed and runnable on this host."""
    try:
        subprocess.run(["llmfit", "--help"], capture_output=True, timeout=10, check=False)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def predictions(ram_gb: int = 36, max_context: int = 16384) -> list[dict]:
    """All models llmfit says FIT this hardware (global flags precede the subcommand)."""
    assert ram_gb > 0 and max_context > 0, "ram + context must be positive"
    try:
        out = subprocess.run(
            ["llmfit", "--memory", f"{ram_gb}G", "--ram", f"{ram_gb}G",
             "--max-context", str(max_context), "fit", "--json"],
            capture_output=True, text=True, timeout=120, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    try:
        data = json.loads(out.stdout)
    except ValueError:
        return []
    return data if isinstance(data, list) else data.get("models", [])


def _norm(name: str) -> str:
    """Reduce a model id to its core identity for cross-naming matches.

    Ollama `qwen2.5:7b-instruct`, HF `Qwen/Qwen2.5-7B-Instruct`, and oMLX
    `Qwen2.5-7B-Instruct-MLX-4bit` all collapse to `qwen2.57binstruct`.
    """
    assert isinstance(name, str), "name must be a string"
    core = name.lower().split("/")[-1]
    for suffix in ("-mlx", "-gguf", "-4bit", "-8bit", "-fp16", "-fp8", "-q4_k_m",
                   "-qat", "-int4", "-autoround", "-instruct", ":instruct"):
        core = core.replace(suffix, "")
    return re.sub(r"[^a-z0-9]", "", core)


def match(model_id: str, preds: list[dict]) -> dict | None:
    """Best llmfit entry for a tested model id (longest core-name overlap), or None."""
    assert isinstance(model_id, str), "model_id must be a string"
    target = _norm(model_id)
    if not target:
        return None
    best, best_len = None, 0
    for p in preds:  # bounded by the fit list
        pn = _norm(p.get("name", ""))
        if pn and (pn in target or target in pn) and len(pn) > best_len:
            best, best_len = p, len(pn)
    return best


def top_tool_use(preds: list[dict], limit: int = 8) -> list[dict]:
    """Highest-scored tool-use-capable fitting models (for selection guidance)."""
    rows = [p for p in preds if "tool_use" in (p.get("capability_ids") or [])]
    return sorted(rows, key=lambda x: -x.get("score", 0))[:limit]
