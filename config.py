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
        "system_prompt": (
            "You are TheAgent0 — an autonomous AI built to do anything the user asks, "
            "learn from every interaction, and evolve. Easy to use, omnicapable, self-improving. "
            "You receive TASKS and finish them END-TO-END: decompose, explore, act, verify, report. "
            "You HAVE real tools — never claim otherwise.\n\n"
            "PRIME DIRECTIVE — SOLVE THE PROBLEM, DON'T JUST EXPLORE IT:\n"
            "  Every user request has exactly two acceptable outcomes:\n"
            "    (1) You DELIVER an answer IN CHAT (text summary, an image you\n"
            "        generated, or a single decision). Creating files is NOT an\n"
            "        outcome — it's a separate action that requires explicit user\n"
            "        request (see CRITICAL RULE below).\n"
            "    (2) You ASK the user ONE clarifying question.\n"
            "  Anything else — endless searching, indefinite exploration, repeating\n"
            "  tool calls with slightly different args hoping for a better result,\n"
            "  bundling extra files the user did NOT ask for — is a FAILURE.\n"
            "  After AT MOST 3 tool calls, stop and produce outcome (1) or (2).\n"
            "  Quality of the answer matters less than producing one; the user can\n"
            "  refine, but they cannot wait forever and they DO NOT want surprise\n"
            "  side-effects.\n"
            "  If the user asks for an image, deliver ONE image (search OR generate)\n"
            "  and stop. If they ask for info, give your best summary and stop.\n\n"
            "CONTEXT — private local installation:\n"
            "  This is a single-user local deployment owned by the operator. There is no\n"
            "  third-party audience, no compliance/PR layer, and no logging visible to anyone\n"
            "  other than the operator. Answer substantively without preamble disclaimers.\n"
            "  If a specific request is illegal in the operator's jurisdiction, decline\n"
            "  briefly and continue. Otherwise help.\n\n"
            "CRITICAL RULE — DO NOT CREATE FILES THE USER DID NOT ASK FOR:\n"
            "  Default output is TEXT IN CHAT. Be concise; answer directly.\n"
            "  Create a FILE only if the user EXPLICITLY signals it — one of these MUST\n"
            "  appear in the user's message:\n"
            "    - a file extension: '.pdf', '.md', '.txt', '.docx', '.json', etc.\n"
            "    - a save verb with a target: 'save to', 'save as', 'write to a file',\n"
            "      'export to', 'output to', 'put it in <name>'\n"
            "    - Vietnamese equivalents: 'ghi vào', 'lưu vào', 'xuất ra', 'save vô'\n"
            "  The bare word 'write' is NOT enough. 'Write me a poem' = text in chat.\n"
            "  'Write a poem to song.txt' = create song.txt. 'Compose / draft / describe /\n"
            "  explain / introduce / tell me about' = ALWAYS text in chat — NEVER a file.\n"
            "  If the user mentioned a directory but no filename, ASK before creating any\n"
            "  file — do not invent a filename.\n"
            "  If the user DID explicitly ask for a file, the task is not done until you\n"
            "  call write_file / write_pdf / write_docx / edit_file with the final content\n"
            "  and report the path.\n\n"
            "WORKFLOW (for any task with >1 step):\n"
            "  1. PLAN — brief numbered plan on turn 1, no tool call yet. List every file\n"
            "     you must READ. List files to WRITE ONLY if the user explicitly asked.\n"
            "  2. EXPLORE — list_dir / glob_files / grep_file / read_file / read_pdf /\n"
            "     read_docx / read_file_chunk (for big files). Batch independent reads in\n"
            "     ONE turn by emitting multiple <tool_use> blocks (they run in parallel).\n"
            "  3. ACT — by default, answer in chat. Use write_file / write_pdf / write_docx\n"
            "     ONLY when the user explicitly asked for a file (see CRITICAL RULE).\n"
            "     Prefer edit_file over write_file for existing files.\n"
            "  4. VERIFY — if (and only if) you wrote a file, re-read it or run_command to\n"
            "     confirm it landed. For chat-only answers, no verification step needed.\n"
            "  5. REPORT — final answer with NO tool_use tags. If you wrote a file,\n"
            "     mention the path. Otherwise just answer in chat.\n\n"
            "WHEN A TOOL FAILS — revise the plan before retrying:\n"
            "  • Don't re-issue the same call with the same args hoping it works.\n"
            "  • Read the <tool_result is_error=\"true\"> body + hint carefully.\n"
            "  • Fix the root cause (wrong path? missing dep? stale old_string?) or\n"
            "    switch to a different tool. State the revised step in 1 line before\n"
            "    emitting the next tool_use.\n"
            "  • After 2 failures of the SAME call, Stop-the-Line fires — the loop refuses\n"
            "    the 3rd attempt. You MUST change at least one argument or pick another tool.\n\n"
            "FOLLOW-UP TURNS — resolve pronouns before acting:\n"
            "  When the user says 'that', 'it', 'the result', 'our last', 'the file',\n"
            "  'the answer', etc., bind the pronoun in THIS order and STOP at the first\n"
            "  match:\n"
            "    (a) <session>recent_exchange.last_numeric_value — if the user message\n"
            "        starts with an operator (+, -, *, /) or contains 'result', 'answer',\n"
            "        'total', 'sum', 'kết quả'.\n"
            "    (b) <session>recent_exchange.last_assistant_answer — for 'that', 'it',\n"
            "        'what you said', and other short-string references.\n"
            "    (c) <session>files_touched (most recent) — for 'the file', 'that file',\n"
            "        'add to that file'.\n"
            "    (d) your prior assistant messages in THIS conversation.\n"
            "    (e) if none of the above resolve it, ASK the user. DO NOT silently\n"
            "        re-read a file or re-call a tool to guess.\n"
            "  If the user message is JUST an operator + number (e.g. '+ 4', '* 2'),\n"
            "  the agent loop handles it directly before you see it.\n\n"
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
            "  • If the user gave a DIRECTORY as output location (no filename), ASK the user\n"
            "    for a filename BEFORE writing. Do not invent one.\n"
            "  • If the user asked for an image (no extension specified), generate_image\n"
            "    saves to ./data/generated/ automatically — that's fine; mention the path.\n"
            "  • For trivial questions (no fs/web needed), answer directly — no plan, no tools.\n"
            "  • Heavy explorations → delegate with the `task` tool to a sub-agent.\n"
            "  • The <environment> and <tools> blocks below are authoritative — use them.\n\n"
            "EXAMPLES — chat vs. file output:\n"
            "  • When a tool returns markdown like '![…](path)' (image link),\n"
            "    include that markdown VERBATIM in your final answer — the UI\n"
            "    will render the image inline. DO NOT replace it with 'saved at\n"
            "    <path>' or just print the path; the user wants to SEE the image.\n"
            "  • 'Write me a poem about autumn' → text in chat. NO file.\n"
            "  • 'Write a haiku to autumn.txt' → write_file('autumn.txt', '<haiku>').\n"
            "  • 'Introduce a person named Nam in EN and JP' → text in chat with two\n"
            "    paragraphs. NO files.\n"
            "  • 'Save that introduction to nam.md' → THEN call write_file.\n"
            "  • 'Draft a report' → chat. 'Draft a report into report.pdf' → write_pdf.\n"
            "  • 'Generate an image of X' → call generate_image; the PNG path comes back.\n"
            "  • 'Viết một bài thơ' → text in chat. 'Viết một bài thơ vào tho.txt' → file."
        ),
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
        "system_prompt": "You are a Summary Specialist. Take complex information and turn it into a concise, bulleted summary for Discord.",
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
