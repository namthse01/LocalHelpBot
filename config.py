# ═══════════════════════════════════════════════════════════════
#  config.py — Cấu hình model duy nhất cho toàn bộ hệ thống
#
#  Khi clone về máy mới, chỉ cần sửa file này.
#  Chạy `ollama list` để xem model đang có trên máy.
# ═══════════════════════════════════════════════════════════════
import os
import os

# ── Ollama endpoint ──────────────────────────────────────────
OLLAMA_BASE = "http://localhost:11434"

# ── Model chat chính — dùng cho tất cả agents ───────────────
# Gợi ý theo RAM:
#   RAM  8GB → "llama3.2:3b"  hoặc "qwen2.5:7b"
#   RAM 16GB → "qwen3.5"      hoặc "qwen2.5-coder:7b"
#   RAM 32GB+ / GPU → "glm-4.7-flash" hoặc model lớn hơn
CHAT_MODEL = "qwen3.5:latest"

# ── Model lớn — dùng cho deep-agent (tác vụ suy luận phức tạp)
# Để trống ("") nếu máy không đủ RAM / không có model lớn
LARGE_MODEL = "glm-4.7-flash:latest"

# ── Model embedding — dùng cho ChromaDB RAG ─────────────────
# Luôn dùng mxbai-embed-large nếu có (chất lượng tốt nhất, chỉ 669MB)
# Thay thế: "nomic-embed-text" hoặc "all-minilm"
EMBED_MODEL = "mxbai-embed-large:latest"

# ── Port của proxy ───────────────────────────────────────────
PROXY_PORT = 11435

# ── Model Provider Configuration ─────────────────────────────
# Your API keys and preferred models
# If an API key is provided, the system will try this provider first.
# If tokens are exhausted or an error occurs, it will automatically fall back to Local (Ollama).
MODEL_PROVIDERS = {
    "primary": {
        "type": "api",
        "provider": "anthropic", # "anthropic", "openai", "google"
        "api_key": os.getenv("ANTHROPIC_API_KEY", "YOUR_ANTHROPIC_API_KEY"),
        "model": "claude-3-5-sonnet-20240620",
    },
    "fallback": {
        "type": "local",
        "provider": "ollama",
        "model": "qwen3.5:latest", # Fallback to this local model
    }
}

# To disable API and use local only, set primary type to "local"
# MODEL_PROVIDERS["primary"]["type"] = "local"

# Use environment variable if available, otherwise fallback to hardcoded token
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "MTQ4NTIzODMyMTU0MTI4MzkwMQ.GytimM.iPNSuHmnvasBuaerx8z4gi-zk3Jj_l64bJJcuY")

# Flexible Discord Configuration
DISCORD_SETTINGS = {
    "guilds": {
        1436893849518866543: {
            "allowed_channels": [
                1436893850139496673,
                1486028541060583535,
                1486028596370997399,
                1486028655208567019,
                1486352473018077394
            ],
            "admin_role_id": None,
        }
    },
    "default_guild_id": 1436893849518866543,
    "allow_all_channels": False,
}

# Keep old variables for backward compatibility if needed, but map them to DISCORD_SETTINGS
DISCORD_SERVER_ID = DISCORD_SETTINGS["default_guild_id"]
ALLOWED_CHANNELS = DISCORD_SETTINGS["guilds"].get(DISCORD_SERVER_ID, {}).get("allowed_channels", [])

# ── Agents Configuration ──────────────────────────────────────
# Define different specialists with their own system prompts and toolsets
AGENT_PROFILES = {
    "main": {
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
            "task", "search_web", "fetch_url",
            "read_file", "list_dir", "grep_file", "glob_files",
            "write_file", "edit_file", "run_command",
            # v3 plugins (core/plugins/*)
            "delete_file", "make_dir", "move_file",
            "python_exec", "list_processes", "kill_process",
            "install_package",
            "read_file_chunk", "read_pdf", "write_pdf", "read_docx", "write_docx",
        ]
    },
    "researcher": {
        "system_prompt": "You are a Deep Research Specialist. Use RAG tools to find exhaustive information. Be thorough and cite sources.",
        "model": "glm-4.7-flash:latest",
        "tools": ["read_file", "list_dir", "search_web"]
    },
    "coder": {
        "system_prompt": "You are a Code Analysis Specialist. Focus on implementation details, patterns, and bug hunting.",
        "model": "qwen3.5:latest",
        "tools": ["read_file", "list_dir", "search_web"]
    },
    "summarizer": {
        "system_prompt": "You are a Summary Specialist. Take complex information and turn it into a concise, bulleted summary for Discord.",
        "model": "qwen3.5:latest",
        "tools": []
    }
}

# ── Automation Tasks ────────────────────────────────────────────
# Daily/Repeated tasks. Format: { "id": "name", "schedule": "HH:MM", "prompt": "...", "recipient": "channel_id" }
AUTOMATION_TASKS = [
    {
        "id": "daily_summary",
        "schedule": "08:00", # 8 AM
        "prompt": "Summarize the most important documents added to the RAG system in the last 24 hours.",
        "recipient": 1436893850139496673, # Target Discord channel
    },
    {
        "id": "system_health_check",
        "schedule": "12:00", # Noon
        "prompt": "Check the local system logs for any critical errors in LocalHelpBot and report them.",
        "recipient": 1436893850139496673,
    }
]

# ── Runtime overrides ───────────────────────────────────────────
# If runtime_overrides.json exists next to config.py, it replaces matching keys
# (MODEL_PROVIDERS, AGENT_PROFILES, DISCORD_SETTINGS, AUTOMATION_TASKS) in memory.
# The UI "Apply Mode Changes" button writes to that file.
import json as _json
_OVERRIDES_PATH = os.path.join(os.path.dirname(__file__), "runtime_overrides.json")
if os.path.exists(_OVERRIDES_PATH):
    try:
        with open(_OVERRIDES_PATH, "r", encoding="utf-8") as _f:
            _ov = _json.load(_f)
        if "providers" in _ov:
            MODEL_PROVIDERS = _ov["providers"]
            # Decrypt api_key fields in-memory (they are stored encrypted on disk)
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
