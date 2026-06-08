# ═══════════════════════════════════════════════════════════════
#  config.py — single source of truth for TheAgent0.
#
#  Edit the values below to point at your Ollama models, API keys,
#  Discord wiring, and agent profiles. The schema in
#  `core/config_schema.py` validates everything at import time; a
#  typo (e.g. `"toolss"` instead of `"tools"`) will refuse to start
#  the proxy with a pointed error message instead of failing deep
#  in the agent loop later.
#
#  `runtime_overrides.json` (created by the UI's "Apply Mode Changes"
#  button) is merged on top of the values here at startup.
# ═══════════════════════════════════════════════════════════════
import os
from pathlib import Path

# Agent system-prompt text lives in a dedicated leaf module (core/agent_prompts.py)
# so this file stays focused on configuration values. The constants below are the
# verbatim system_prompt strings, slotted into AGENT_PROFILES unchanged.
from core.agent_prompts import (
    CAD_RAG_SPECIALIST_SYSTEM_PROMPT,
    CODER_SYSTEM_PROMPT,
    MAIN_SYSTEM_PROMPT,
    RESEARCHER_SYSTEM_PROMPT,
    SUMMARIZER_SYSTEM_PROMPT,
    VISION_SPECIALIST_SYSTEM_PROMPT,
)

# ── Ollama endpoint ──────────────────────────────────────────────
OLLAMA_BASE = "http://localhost:11434"

# ── Model chat chính — dùng cho tất cả agents ───────────────────
#   RAM  8GB → "llama3.2:3b"  / "qwen2.5:7b"
#   RAM 16GB → "qwen3.5"      / "qwen2.5-coder:7b"
#   RAM 32GB+ / GPU → "glm-4.7-flash" hoặc lớn hơn
# Default is an abliterated Qwen 2.5 (refusals removed at the weights
# level) for unrestricted single-user local use. Swap back via
# runtime_overrides.json without editing source — see README.
CHAT_MODEL = "huihui_ai/qwen2.5-abliterate:14b"

# ── Image generation (diffusers, in-process) ───────────────────
# Used by the `generate_image` tool. Any HuggingFace text-to-image model
# id works. Defaults to SDXL Turbo — fast (~1-4 steps), ~6 GB VRAM,
# decent quality. Requires `pip install diffusers transformers torch
# accelerate safetensors` — see README "Image generation" section.
# Alternative model ids you can paste here:
#   SD_MODEL_ID = "stabilityai/stable-diffusion-2-1"   # 5 GB, classic SD
#   SD_MODEL_ID = "stabilityai/stable-diffusion-xl-base-1.0"  # 7 GB SDXL
SD_MODEL_ID = "stabilityai/sdxl-turbo"

# ── Model lớn — dùng cho deep-agent (suy luận phức tạp) ─────────
# Leave "" if your machine cannot host a large model.
LARGE_MODEL = "glm-4.7-flash:latest"

# ── Model embedding — RAG vector store ─────────────────────────
# mxbai-embed-large is the recommended default (1024-dim, 669MB).
# Changing this requires re-embedding cad_db/ from scratch.
EMBED_MODEL = "mxbai-embed-large:latest"

# ── Vision model (v4 Slice 5) ──────────────────────────────────
# Used by describe_image / screenshot_and_describe. Install via
# `ollama pull llava`. Leave as-is even if llava isn't installed —
# the tools degrade gracefully with a clear install hint.
VISION_MODEL = "llava:latest"

# ── Port of the proxy ──────────────────────────────────────────
PROXY_PORT = 11435

# ── vLLM + DFlash backend (v5 — real speculative decoding) ──────
# vLLM serves an OpenAI-compatible API. DFlash (block-diffusion speculative
# decoding, https://arxiv.org/abs/2602.06036) is enabled at the vLLM server's
# launch via --speculative-config, giving 2-4× faster generation transparently.
# To USE it as TheAgent0's model, set a provider slot to:
#   {"type": "vllm", "model": "Qwen/Qwen3.5-27B", "base_url": VLLM_BASE}
# and launch the server with: python scripts/serve_vllm_dflash.py --help
# Requires an NVIDIA/AMD GPU on Linux/WSL2. On Windows/macOS use Ollama.
VLLM_BASE = os.getenv("VLLM_BASE", "http://localhost:8000/v1")

# ── Self-learning feature flags (v4 Slice 4) ───────────────────
# Auto-capture: detect "no/don't/actually" style corrections and save
# them as lessons. Off by default — users can flip it on after seeing
# how the lessons feature works.
LESSONS_AUTO_CAPTURE = False

# update_self tool: allow `git pull --ff-only` on main from inside the
# agent. Disabled by default — full self-modification is high risk.
UPDATE_SELF_ENABLED = False

# Auto-pull missing Ollama models: when a chat hits a 404 because the
# requested model isn't installed, prompt the user to `ollama pull` it.
# Off by default; on for the convenience win.
OLLAMA_AUTO_PULL = False

# ── Self-evolving skills (v5 — ported from odysseus) ────────────
# After a complex top-level run (>=2 turns or >=2 tool calls), ask the LLM
# to distill the approach into a reusable SKILL.md under data/skills/.
# Matching skills are auto-injected into the system prompt on future turns.
# Off by default — flip on once you've seen save_skill / list_skills work.
SKILLS_AUTO_EXTRACT = False

# ── Model Provider Configuration ───────────────────────────────
# Primary is tried first; on token-exhaust / error / 401 we fall
# back to the local provider.
MODEL_PROVIDERS = {
    "primary": {
        "type": "api",
        "provider": "anthropic",  # "anthropic" | "openai" | "google"
        "api_key": os.getenv("ANTHROPIC_API_KEY", "YOUR_ANTHROPIC_API_KEY"),
        "model": "claude-3-5-sonnet-20240620",
    },
    "fallback": {
        "type": "local",
        "provider": "ollama",
        "model": CHAT_MODEL,
    },
}

# Discord token. Prefer env var; the hardcoded fallback is kept for
# convenience but never commit a real token here.
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "MTQ4NTIzODMyMTU0MTI4MzkwMQ.GytimM.iPNSuHmnvasBuaerx8z4gi-zk3Jj_l64bJJcuY")

# Per-guild allow-list. Add a guild id with its channel list to allow
# the bot to respond there.
DISCORD_SETTINGS = {
    "guilds": {
        1436893849518866543: {
            "allowed_channels": [
                1436893850139496673,
                1486028541060583535,
                1486028596370997399,
                1486028655208567019,
                1486352473018077394,
            ],
            "admin_role_id": None,
        }
    },
    "default_guild_id": 1436893849518866543,
    "allow_all_channels": False,
}

# Back-compat aliases — kept because various modules still import them.
DISCORD_SERVER_ID = DISCORD_SETTINGS["default_guild_id"]
ALLOWED_CHANNELS = DISCORD_SETTINGS["guilds"].get(DISCORD_SERVER_ID, {}).get("allowed_channels", [])

# ── Agent profiles ─────────────────────────────────────────────
AGENT_PROFILES = {
    "main": {
        "description": (
            "General task executor & router. Owns the user conversation, plans, "
            "calls tools end-to-end, delegates to other specialists when needed. "
            "Default choice — only delegate if the task obviously fits a specialist."
        ),
        "system_prompt": MAIN_SYSTEM_PROMPT,
        "model": CHAT_MODEL,
        "tools": [
            # ── High-frequency / high-leverage tools FIRST ────────────
            # Primacy bias is real: tools at the top get picked more often.
            # Put the actions the user most commonly wants at the start.
            "generate_image", "describe_image", "screenshot_and_describe",
            "search_web", "fetch_url",
            "read_file", "write_file", "edit_file",
            "list_dir", "grep_file", "glob_files",
            "run_command", "python_exec",
            # ── Common follow-ons ──────────────────────────────────────
            "read_pdf", "write_pdf", "read_docx", "write_docx",
            "read_file_chunk",
            "task", "query_rag",
            "delete_file", "make_dir", "move_file",
            "install_package",
            # v4 Slice 3 — web/resource tools (less common)
            "download_file", "extract_text",
            "github_search_repos", "github_read_file", "github_releases",
            "pypi_search", "pypi_info",
            "youtube_transcript", "wikipedia_summary",
            # v4 Slice 2 — deep computer-access tools (rare)
            "screenshot", "clipboard_read", "clipboard_write", "system_info",
            "open_with_default_app", "list_windows", "watch_file",
            "find_in_files", "read_env",
            "list_processes", "kill_process",
            # v4 Slice 4 — self-learning tools (rare)
            "save_lesson", "learn_from_file", "learn_from_url", "update_self",
            # v5 — self-evolving skills + deep research
            "save_skill", "list_skills", "delete_skill", "deep_research",
        ],
        "verify": "off",  # Slice 5 enables CoVe wrapper when set to "high"
    },
    "researcher": {
        "description": (
            "Deep research specialist — multi-source synthesis. Delegate when the "
            "task needs reading/comparing 3+ web sources or RAG docs and citing them. "
            "NOT for single-URL lookups (main handles those)."
        ),
        "system_prompt": RESEARCHER_SYSTEM_PROMPT,
        "model": "glm-4.7-flash:latest",
        "tools": [
            "read_file", "list_dir",
            "search_web", "fetch_url", "query_rag",
            # v4 Slice 3 — web/resource tools (researcher is the prime consumer)
            "extract_text", "github_search_repos", "github_read_file", "github_releases",
            "pypi_search", "pypi_info", "youtube_transcript", "wikipedia_summary",
            # v5 — multi-round research + skill memory
            "deep_research", "save_skill", "list_skills",
        ],
        "verify": "off",
    },
    "coder": {
        "description": (
            "Code analysis specialist — read-only code comprehension. Delegate for "
            "bug-hunting across many files, pattern/convention audits, explaining "
            "unfamiliar code. NOT for edits (main does edits itself)."
        ),
        "system_prompt": CODER_SYSTEM_PROMPT,
        "model": CHAT_MODEL,
        "tools": [
            "read_file", "list_dir", "search_web", "grep_file", "glob_files",
            "read_file_chunk",
            # v4 Slice 2: read-only system-info tools for context (no destructive ops)
            "system_info", "find_in_files",
        ],
        "verify": "off",
    },
    "summarizer": {
        "description": (
            "Terse summary generator for Discord — takes long content, returns "
            "short bullets. Delegate for final output compression only."
        ),
        "system_prompt": SUMMARIZER_SYSTEM_PROMPT,
        "model": CHAT_MODEL,
        "tools": [],
        "verify": "off",
    },
    # v4 Slice 5: vision-specialist profile. Reached via `vision-agent`
    # virtual model in core/proxy.py. Defaults its model to VISION_MODEL
    # (llava) so it speaks multimodal directly.
    "vision-specialist": {
        "description": (
            "Multimodal vision specialist — reads images directly. "
            "Use when the user uploads an image, a screenshot, or asks "
            "you to look at something visual."
        ),
        "system_prompt": VISION_SPECIALIST_SYSTEM_PROMPT,
        "model": CHAT_MODEL,   # main agent stays a text model;
                                      # the vision *tool* does the vision call.
        "tools": [
            "read_file", "list_dir",
            "describe_image", "screenshot_and_describe",
            "generate_image",
        ],
        "verify": "off",
    },
    # Routed to by the cad-rag virtual model. Agentic RAG: the agent
    # CHOOSES when to call query_rag, instead of having it pre-injected.
    # (Replaces the legacy _inject_rag path in core/proxy.py.)
    "cad-rag-specialist": {
        "description": (
            "CAD / AutoCAD knowledge specialist — queries the local RAG corpus. "
            "Reached via the `cad-rag` virtual model."
        ),
        "system_prompt": CAD_RAG_SPECIALIST_SYSTEM_PROMPT,
        "model": CHAT_MODEL,
        "tools": ["query_rag", "read_file", "list_dir"],
        "verify": "off",
    },
}

# ── Automation tasks ───────────────────────────────────────────
AUTOMATION_TASKS = [
    {
        "id": "daily_summary",
        "schedule": "08:00",
        "prompt": "Summarize the most important documents added to the RAG system in the last 24 hours.",
        "recipient": 1436893850139496673,
    },
    {
        "id": "system_health_check",
        "schedule": "12:00",
        "prompt": "Check the local system logs for any critical errors in TheAgent0 and report them.",
        "recipient": 1436893850139496673,
    },
]


# ═════════════════════════════════════════════════════════════════
# Runtime override merge + validation
# ═════════════════════════════════════════════════════════════════
#
# Everything above is the static defaults. The UI / Discord adapter
# can persist runtime tweaks to `runtime_overrides.json` (encrypted
# api_keys, edited prompts, etc.). We merge that file in here and
# then run the whole tree through the pydantic schema in
# `core/config_schema.py` — typos blow up at import time instead of
# at the first specialist invocation.
import json as _json

_OVERRIDES_PATH = Path(__file__).parent / "runtime_overrides.json"

if _OVERRIDES_PATH.exists():
    try:
        with _OVERRIDES_PATH.open("r", encoding="utf-8") as _f:
            _ov = _json.load(_f)
        if "providers" in _ov:
            MODEL_PROVIDERS = _ov["providers"]
            # Decrypt api_key fields in-memory (stored encrypted on disk).
            try:
                from core.secrets import decrypt_secret as _dec
                for _slot in ("primary", "fallback"):
                    _p = MODEL_PROVIDERS.get(_slot) or {}
                    if _p.get("api_key"):
                        _p["api_key"] = _dec(_p["api_key"])
            except Exception as _de:
                print(f"[config] Could not decrypt api_key: {_de}")
        if "agents" in _ov:
            AGENT_PROFILES = _ov["agents"]
        if "discord" in _ov:
            DISCORD_SETTINGS = _ov["discord"]
        if "tasks" in _ov:
            AUTOMATION_TASKS = _ov["tasks"]
    except Exception as _e:
        print(f"[config] Failed to load runtime_overrides.json: {_e}")

# Validate (the schema's pretty-printer prints + raises SystemExit on
# failure). We don't *keep* the RootConfig instance around — the
# legacy dicts above are the source-of-truth for the rest of the
# codebase. But running validation here makes typos fail loudly.
try:
    from core.config_schema import RootConfig, pretty_print_validation_error
    from pydantic import ValidationError as _VE

    _CONFIG_VIEW = {
        "ollama_base": OLLAMA_BASE,
        "chat_model": CHAT_MODEL,
        "large_model": LARGE_MODEL,
        "embed_model": EMBED_MODEL,
        "proxy_port": PROXY_PORT,
        "providers": MODEL_PROVIDERS,
        "discord_token": DISCORD_TOKEN,
        "discord": DISCORD_SETTINGS,
        "agents": AGENT_PROFILES,
        "tasks": AUTOMATION_TASKS,
    }
    try:
        CONFIG = RootConfig.model_validate(_CONFIG_VIEW)
    except _VE as _e:
        pretty_print_validation_error(_e)
        raise SystemExit(1)
except SystemExit:
    raise
except Exception as _e:
    # Schema module itself broke (rare). Don't kill the proxy over
    # that — just emit a console warning and keep going with the
    # un-validated dicts so the dev can debug.
    print(f"[config] schema validation skipped (non-fatal): {_e}")
    CONFIG = None
