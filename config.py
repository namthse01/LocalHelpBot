# ═══════════════════════════════════════════════════════════════
#  config.py — Cấu hình model duy nhất cho toàn bộ hệ thống
#
#  Khi clone về máy mới, chỉ cần sửa file này.
#  Chạy `ollama list` để xem model đang có trên máy.
# ═══════════════════════════════════════════════════════════════

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

# ── Discord Integration ───────────────────────────────────────
DISCORD_TOKEN = "MTQ4NTIzODMyMTU0MTI4MzkwMQ.GytimM.iPNSuHmnvasBuaerx8z4gi-zk3Jj_l64bJJcuY"
DISCORD_SERVER_ID = 1436893849518866543
ALLOWED_CHANNELS = [
    1436893850139496673,
    1486028541060583535,
    1486028596370997399,
    1486028655208567019,
    1486352473018077394
]
