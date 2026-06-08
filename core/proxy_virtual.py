"""Virtual-model registry: names, system prompts, and the /api/tags shim.

Extracted from :mod:`core.proxy` (behavior-preserving move — no logic
changes). Holds the virtual-model name constants (wire identifiers like
``auto-agent`` / ``research-agent`` — DO NOT rename), the forward-only
specialist system prompts, and ``_tags_with_virtual`` which advertises the
virtual models alongside Ollama's real ones. Re-exported from ``core.proxy``.
"""

from __future__ import annotations

import json
import urllib.request

from config import CHAT_MODEL, OLLAMA_BASE

REAL_MODEL = CHAT_MODEL
CAD_MODEL     = "cad-rag"
UI_MODEL      = "ui-agent"
CODE_MODEL    = "code-agent"
WEB_MODEL     = "web-creep"
BROWSER_MODEL = "browser-agent"
DEEP_MODEL    = "deep-agent"
AUTO_MODEL     = "auto-agent"
VISION_MODEL_V = "vision-agent"   # v4 Slice 5
RESEARCH_MODEL = "research-agent"  # v5 — deep multi-source research

VIRTUAL_MODELS = [CAD_MODEL, UI_MODEL, CODE_MODEL, WEB_MODEL, BROWSER_MODEL, DEEP_MODEL, AUTO_MODEL, VISION_MODEL_V, RESEARCH_MODEL]

# (Legacy: CAD_SYSTEM + SCORING_THRESHOLD removed in v4. cad-rag now routes
# through `cad-rag-specialist` profile + agentic query_rag tool. See config.py.)
UI_SYSTEM = "You are a UI/Frontend specialist agent..."
CODE_SYSTEM = "You are an autonomous coding agent..."
BROWSER_SYSTEM = "You are browser-agent, a local browser data reader..."
WEB_SYSTEM = "You are web-creep, an autonomous web research agent..."


def _tags_with_virtual() -> bytes:
    try:
        with urllib.request.urlopen(OLLAMA_BASE + "/api/tags", timeout=5) as r:
            data = json.loads(r.read())
    except Exception:
        data = {"models": []}
    virtual = [
        {"name": CAD_MODEL, "model": CAD_MODEL, "modified_at": "2026-01-01T00:00:00Z", "size": 0, "details": {"parameter_size": "7.6B", "family": "CAD/AutoCAD RAG agent"}},
        {"name": UI_MODEL, "model": UI_MODEL, "modified_at": "2026-01-01T00:00:00Z", "size": 0, "details": {"parameter_size": "7.6B", "family": "UI/Frontend agent"}},
        {"name": CODE_MODEL, "model": CODE_MODEL, "modified_at": "2026-01-01T00:00:00Z", "size": 0, "details": {"parameter_size": "7.6B", "family": "Agentic code agent"}},
        {"name": WEB_MODEL, "model": WEB_MODEL, "modified_at": "2026-01-01T00:00:00Z", "size": 0, "details": {"parameter_size": "7.6B", "family": "Web research agent"}},
        {"name": BROWSER_MODEL, "model": BROWSER_MODEL, "modified_at": "2026-01-01T00:00:00Z", "size": 0, "details": {"parameter_size": "7.6B", "family": "Browser cookies/storage reader"}},
        {"name": DEEP_MODEL, "model": DEEP_MODEL, "modified_at": "2026-01-01T00:00:00Z", "size": 0, "details": {"parameter_size": "19B", "family": "Deep reasoning agent (glm-4.7-flash)"}},
        {"name": AUTO_MODEL, "model": AUTO_MODEL, "modified_at": "2026-01-01T00:00:00Z", "size": 0, "details": {"parameter_size": "7.6B", "family": "Smart Router Agent"}},
        {"name": VISION_MODEL_V, "model": VISION_MODEL_V, "modified_at": "2026-01-01T00:00:00Z", "size": 0, "details": {"parameter_size": "?", "family": "Multimodal vision agent (llava)"}},
        {"name": RESEARCH_MODEL, "model": RESEARCH_MODEL, "modified_at": "2026-01-01T00:00:00Z", "size": 0, "details": {"parameter_size": "7.6B", "family": "Deep research agent (multi-round + HTML report)"}},
    ]
    data["models"] = virtual + data.get("models", [])
    return json.dumps(data).encode()
