"""Adaptive input-token budget — ported from odysseus (src/context_budget.py).

TheAgent0's `MemoryEngine` (core/memory.py) compacts the conversation once it
crosses `max_tokens` (default 6000). That fixed default is right for a 7B local
model with a 4-8K window, but it silently throttles a long-context model: a
32K/128K model gets compacted at 6000 input tokens even though it can hold far
more, throwing away context it didn't need to.

`compute_input_token_budget()` derives the effective compaction threshold from
the *model's actual context window* when the user hasn't pinned an explicit
budget, while honouring an explicit setting exactly (clamped to the window).
Pure and side-effect free, so it's trivially unit-testable.

The model's context window is discovered at runtime via
`probe_context_length()` (Ollama `/api/show` → `context_length`), cached per
model name so we don't probe every turn.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.request
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Generous ceiling so long-context models are unblocked without sending a
# pathologically large prompt every turn. Covers 128K fully; bounds 1M models.
DEFAULT_HARD_MAX = 200_000
DEFAULT_BUDGET = 6000          # matches core.memory.DEFAULT_MAX_TOKENS
DEFAULT_HEADROOM = 0.85


def compute_input_token_budget(
    configured: int,
    context_length: int,
    explicit: bool,
    *,
    default: int = DEFAULT_BUDGET,
    headroom: float = DEFAULT_HEADROOM,
    hard_max: int = DEFAULT_HARD_MAX,
) -> int:
    """Return the effective soft input-token budget (compaction threshold).

    Rules:
      - Explicit user budget is honoured exactly, clamped to the model's
        window when that window is known (never send more than the model holds).
      - Otherwise scale to `headroom` of the context window, capped at
        `hard_max`, so long-context models use their capacity.
      - When the window is unknown, fall back to configured/default.
    """
    configured = int(configured or 0)
    context_length = int(context_length or 0)

    if explicit and configured > 0:
        return min(configured, context_length) if context_length > 0 else configured

    if context_length > 0:
        scaled = int(context_length * headroom)
        return max(1, min(scaled, hard_max))

    return configured if configured > 0 else default


# ───────────────────────────────────────────────────────────────────────
# Context-window discovery (Ollama /api/show), cached per model
# ───────────────────────────────────────────────────────────────────────

_cache: Dict[str, int] = {}
_cache_lock = threading.Lock()


def probe_context_length(model: str, *, ollama_base: Optional[str] = None, timeout: int = 4) -> int:
    """Best-effort discovery of a model's context window via Ollama /api/show.

    Returns 0 when unknown (non-Ollama provider, model not pulled, timeout,
    older Ollama without the field). Cached per model name — pass a model not
    seen before to trigger a probe; repeats are free.
    """
    if not model:
        return 0
    with _cache_lock:
        if model in _cache:
            return _cache[model]

    length = 0
    try:
        if ollama_base is None:
            from config import OLLAMA_BASE
            ollama_base = OLLAMA_BASE
        body = json.dumps({"model": model}).encode()
        req = urllib.request.Request(
            ollama_base.rstrip("/") + "/api/show",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        info = data.get("model_info") or {}
        # Key looks like "qwen2.<arch>.context_length" — scan for any *.context_length.
        for k, v in info.items():
            if k.endswith("context_length") and isinstance(v, (int, float)):
                length = int(v)
                break
    except Exception as e:  # noqa: BLE001 — discovery is best-effort
        logger.debug("[context_budget] probe failed for %s: %s", model, e)
        length = 0

    with _cache_lock:
        _cache[model] = length
    return length


def effective_max_tokens(
    model: str,
    *,
    configured: int = DEFAULT_BUDGET,
    explicit: bool = False,
    ollama_base: Optional[str] = None,
) -> int:
    """Convenience: probe the model's window and compute the budget in one call."""
    ctx_len = probe_context_length(model, ollama_base=ollama_base)
    return compute_input_token_budget(configured, ctx_len, explicit)


def reset_cache_for_tests() -> None:
    with _cache_lock:
        _cache.clear()


__all__ = [
    "compute_input_token_budget",
    "probe_context_length",
    "effective_max_tokens",
    "reset_cache_for_tests",
    "DEFAULT_BUDGET",
    "DEFAULT_HARD_MAX",
    "DEFAULT_HEADROOM",
]
