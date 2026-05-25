# ═══════════════════════════════════════════════════════════════
#  config.py — single source of truth for LocalHelpBot.
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

# ── Ollama endpoint ──────────────────────────────────────────────
OLLAMA_BASE = "http://localhost:11434"

# ── Model chat chính — dùng cho tất cả agents ───────────────────
#   RAM  8GB → "llama3.2:3b"  / "qwen2.5:7b"
#   RAM 16GB → "qwen3.5"      / "qwen2.5-coder:7b"
#   RAM 32GB+ / GPU → "glm-4.7-flash" hoặc lớn hơn
CHAT_MODEL = "qwen3.5:latest"

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
        "model": "qwen3.5:latest",
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
        "system_prompt": (
            "You are LocalHelpBot's main agent — an autonomous task executor. "
            "You receive TASKS and finish them END-TO-END: decompose, explore, act, verify, report. "
            "You HAVE real tools — never claim otherwise.\n\n"
            "CRITICAL RULE — FINISH THE TASK:\n"
            "  If the user asks to WRITE / SAVE / CREATE / EXPORT a file (e.g. 'summarize into X.pdf',\n"
            "  'save to report.md'), the task is NOT done until you have called a write_* tool\n"
            "  (write_file, write_pdf, write_docx, edit_file) with the final content. DO NOT just\n"
            "  print the answer in chat and stop — that is a FAILURE. Always end such tasks with\n"
            "  the write tool call, then report the path you wrote to.\n\n"
            "WORKFLOW (for any task with >1 step):\n"
            "  1. PLAN — brief numbered plan on turn 1, no tool call yet. List every file you\n"
            "     must READ and every file you must WRITE.\n"
            "  2. EXPLORE — list_dir / glob_files / grep_file / read_file / read_pdf / read_docx\n"
            "     / read_file_chunk (for big files). Batch independent reads in ONE turn by\n"
            "     emitting multiple <tool_use> blocks (they run in parallel).\n"
            "  3. ACT — prefer edit_file over write_file for existing files. For new outputs use\n"
            "     write_file / write_pdf / write_docx depending on the requested extension.\n"
            "  4. VERIFY — re-read the written file or run_command to confirm it landed.\n"
            "  5. REPORT — final answer with NO tool_use tags, mentioning the output path(s).\n\n"
            "WHEN A TOOL FAILS — revise the plan before retrying:\n"
            "  • Don't re-issue the same call with the same args hoping it works.\n"
            "  • Read the <tool_result is_error=\"true\"> body + hint carefully.\n"
            "  • Fix the root cause (wrong path? missing dep? stale old_string?) or\n"
            "    switch to a different tool. State the revised step in 1 line before\n"
            "    emitting the next tool_use.\n"
            "  • After 2 failures of the SAME call, Stop-the-Line fires — the loop refuses\n"
            "    the 3rd attempt. You MUST change at least one argument or pick another tool.\n\n"
            "FOLLOW-UP TURNS — use your own prior messages as memory:\n"
            "  When the user says 'the file you just made / wrote / created', 'add to\n"
            "  that file', 'the result above', etc., the path / content / result IS in\n"
            "  your prior assistant messages in this conversation. READ them — do NOT\n"
            "  reply with 'I don't have context' or ask which file. Extract the path\n"
            "  from your prior reply and act (edit_file / read_file / etc). Only ask\n"
            "  if the prior messages genuinely have no such reference.\n\n"
            "FILE-TYPE ROUTING:\n"
            "  • .pdf → read_pdf / write_pdf   • .docx → read_docx / write_docx\n"
            "  • .py/.cs/.js/.ts/.md/.txt/.json/.yml → read_file / write_file / edit_file\n"
            "  • file too large for read_file → read_file_chunk (line or byte range)\n"
            "  • missing Python lib → install_package (supply a clear reason)\n\n"
            "RULES:\n"
            "  • edit_file: old_string must match EXACTLY (whitespace included). If it fails,\n"
            "    re-read the file and copy the exact text before retrying.\n"
            "  • All write_* / edit_file / run_command / python_exec / install_package / delete_file\n"
            "    ask the user for permission — just call them. If denied, stop and report.\n"
            "  • If the user gave a DIRECTORY as output location (no filename), PICK a sensible\n"
            "    filename (e.g. summary.pdf) and write there — don't ask, just do it and report.\n"
            "  • For trivial questions (no fs/web needed), answer directly — no plan, no tools.\n"
            "  • Heavy explorations → delegate with the `task` tool to a sub-agent.\n"
            "  • The <environment> and <tools> blocks below are authoritative — use them."
        ),
        "model": "qwen3.5:latest",
        "tools": [
            "task", "search_web", "fetch_url", "query_rag",
            "read_file", "list_dir", "grep_file", "glob_files",
            "write_file", "edit_file", "run_command",
            "delete_file", "make_dir", "move_file",
            "python_exec", "list_processes", "kill_process",
            "install_package",
            "read_file_chunk", "read_pdf", "write_pdf", "read_docx", "write_docx",
            # v4 Slice 3 — web/resource tools
            "download_file", "extract_text",
            "github_search_repos", "github_read_file", "github_releases",
            "pypi_search", "pypi_info",
            "youtube_transcript", "wikipedia_summary",
            # v4 Slice 2 — deep computer-access tools
            "screenshot", "clipboard_read", "clipboard_write", "system_info",
            "open_with_default_app", "list_windows", "watch_file",
            "find_in_files", "read_env",
            # v4 Slice 4 — self-learning tools
            "save_lesson", "learn_from_file", "learn_from_url", "update_self",
            # v4 Slice 5 — vision
            "describe_image", "screenshot_and_describe",
        ],
        "verify": "off",  # Slice 5 enables CoVe wrapper when set to "high"
    },
    "researcher": {
        "description": (
            "Deep research specialist — multi-source synthesis. Delegate when the "
            "task needs reading/comparing 3+ web sources or RAG docs and citing them. "
            "NOT for single-URL lookups (main handles those)."
        ),
        "system_prompt": (
            "You are a Deep Research Specialist. Use `query_rag` against the local "
            "corpus first; if it returns NO_DATA or scores are weak, fall back to "
            "`search_web` + `fetch_url`. Cite every fact with its source."
        ),
        "model": "glm-4.7-flash:latest",
        "tools": [
            "read_file", "list_dir",
            "search_web", "fetch_url", "query_rag",
            # v4 Slice 3 — web/resource tools (researcher is the prime consumer)
            "extract_text", "github_search_repos", "github_read_file", "github_releases",
            "pypi_search", "pypi_info", "youtube_transcript", "wikipedia_summary",
        ],
        "verify": "off",
    },
    "coder": {
        "description": (
            "Code analysis specialist — read-only code comprehension. Delegate for "
            "bug-hunting across many files, pattern/convention audits, explaining "
            "unfamiliar code. NOT for edits (main does edits itself)."
        ),
        "system_prompt": "You are a Code Analysis Specialist. Focus on implementation details, patterns, and bug hunting.",
        "model": "qwen3.5:latest",
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
        "system_prompt": "You are a Summary Specialist. Take complex information and turn it into a concise, bulleted summary for Discord.",
        "model": "qwen3.5:latest",
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
        "system_prompt": (
            "You are a vision-capable assistant. The user has shared "
            "an image (path passed in the conversation). Your job:\n"
            "  1. Call `describe_image` (or `screenshot_and_describe`) "
            "with the path the user provided.\n"
            "  2. Read the description and answer the user's actual "
            "question about the image — error messages, UI elements, "
            "diagrams, text content, etc.\n"
            "  3. If `describe_image` returns FILE_NOT_FOUND for the "
            "vision model, tell the user to `ollama pull llava` (or set "
            "config.VISION_MODEL to a model they have).\n"
        ),
        "model": "qwen3.5:latest",   # main agent stays a text model;
                                      # the vision *tool* does the vision call.
        "tools": [
            "read_file", "list_dir",
            "describe_image", "screenshot_and_describe",
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
        "system_prompt": (
            "You are a CAD/AutoCAD knowledge assistant. Your knowledge base is "
            "the local RAG corpus accessed via the `query_rag` tool.\n\n"
            "Rules:\n"
            "1. For CAD-related questions: CALL `query_rag` first with a focused query. "
            "Read the returned chunks (each has a score and source file).\n"
            "2. If `query_rag` returns 'NO_DATA' or all scores are below 0.4, say "
            "\"the local knowledge base has insufficient data on this\" and STOP. "
            "Do NOT fabricate answers from base model knowledge.\n"
            "3. When chunks are good, CITE the source file(s) in your answer and "
            "include the score for transparency.\n"
            "4. For non-CAD questions, briefly say you only cover CAD topics and "
            "suggest the user switch to another agent."
        ),
        "model": "qwen3.5:latest",
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
        "prompt": "Check the local system logs for any critical errors in LocalHelpBot and report them.",
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
