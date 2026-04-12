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
        "system_prompt": "You are the Main Orchestrator of LocalHelpBot. Your job is to analyze user requests and delegate to the best specialist if needed. Specialists: 'researcher' (deep RAG), 'coder' (code analysis), 'summarizer' (concise reports).",
        "model": "qwen3.5:latest",
        "tools": ["delegate", "search_web"]
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
