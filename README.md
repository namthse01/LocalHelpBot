# LocalHelpBot

Hệ thống AI Multi-Agent chạy local, kết hợp RAG (Retrieval-Augmented Generation), Agentic Loop tự sửa lỗi, và Multi-Model Orchestration. Hỗ trợ fallback tự động từ Cloud API (Anthropic/OpenAI) sang Ollama local khi hết token hoặc mất mạng. Tích hợp Discord Bot và Web UI.

---

## Yêu cầu hệ thống

| Thành phần | Yêu cầu |
|---|---|
| OS | Windows 10/11 (khuyến nghị). Linux/Mac cần chỉnh lại file `.bat` thành `.sh` |
| Python | **3.10 trở lên** — kiểm tra bằng `python --version` |
| RAM | Tối thiểu 8GB (chạy model nhỏ). 16GB+ khuyến nghị |
| Ollama | Bắt buộc — tải tại https://ollama.com/ |
| Git | Để clone repo |

---

## Hướng dẫn cài đặt từng bước

### Bước 1: Clone repo

```bash
git clone <repo-url>
cd LocalHelpBot
```

### Bước 2: Cài Ollama và pull model

Tải Ollama từ https://ollama.com/ và cài đặt. Sau đó mở terminal chạy:

```bash
ollama pull qwen3.5
ollama pull mxbai-embed-large
ollama pull glm-4.7-flash
```

**Chọn model theo RAM:**
- **8GB RAM**: dùng `llama3.2:3b` hoặc `qwen2.5:7b`
- **16GB RAM**: dùng `qwen3.5` hoặc `qwen2.5-coder:7b`
- **32GB+ RAM / có GPU**: dùng `glm-4.7-flash` hoặc model lớn hơn

Sau khi pull xong, kiểm tra bằng:

```bash
ollama list
```

Đảm bảo Ollama đang chạy (mặc định port `11434`).

### Bước 3: Tạo virtual environment và cài dependencies

**Windows:**

```bash
python -m venv venv
venv\Scripts\activate
```

**Linux/Mac:**

```bash
python3 -m venv venv
source venv/bin/activate
```

**Cài thư viện (chạy trong venv đã activate):**

```bash
pip install chromadb pycryptodome pywin32 langchain-core langchain-text-splitters langchain-community tiktoken pypdf docx2txt unstructured requests discord.py
```

> **Lưu ý cho Linux/Mac:** bỏ `pywin32` khỏi lệnh pip vì đó là thư viện chỉ dành cho Windows.

### Bước 4: Cấu hình `config.py`

Mở file `config.py` tại thư mục gốc và chỉnh các giá trị sau:

**4a. Model chat chính** — sửa `CHAT_MODEL` cho khớp model đã pull:

```python
CHAT_MODEL = "qwen3.5:latest"       # Model dùng cho tất cả agents
LARGE_MODEL = "glm-4.7-flash:latest" # Model lớn cho deep-agent (để "" nếu không có)
EMBED_MODEL = "mxbai-embed-large:latest"  # Model embedding cho RAG
```

**4b. API Key (tuỳ chọn)** — nếu muốn dùng Cloud API làm primary, local làm fallback:

```python
MODEL_PROVIDERS = {
    "primary": {
        "type": "api",
        "provider": "anthropic",  # hoặc "openai"
        "api_key": "sk-ant-xxxxx",  # điền API key thật
        "model": "claude-3-5-sonnet-20240620",
    },
    "fallback": {
        "type": "local",
        "provider": "ollama",
        "model": "qwen3.5:latest",
    }
}
```

Nếu **chỉ muốn dùng local** (không cần API key), đổi primary thành:

```python
"primary": {
    "type": "local",
    "provider": "ollama",
    "model": "qwen3.5:latest",
},
```

**4c. Discord Bot (tuỳ chọn)** — nếu muốn kết nối Discord:

```python
DISCORD_TOKEN = "your-discord-bot-token-here"

DISCORD_SETTINGS = {
    "guilds": {
        YOUR_SERVER_ID: {
            "allowed_channels": [CHANNEL_ID_1, CHANNEL_ID_2],
            "admin_role_id": None,
        }
    },
    "default_guild_id": YOUR_SERVER_ID,
    "allow_all_channels": False,
}
```

### Bước 5: Khởi tạo Knowledge Base (RAG)

Đặt tài liệu (PDF, MD, TXT, DOCX, HTML, PY, CS, JS, TS, CPP) vào thư mục `docs/`.

Chạy indexer để build vector database:

```bash
python data/indexer.py
```

Lệnh này sẽ:
1. Chunk tài liệu (chia nhỏ thành đoạn)
2. Embed bằng Ollama (`mxbai-embed-large`)
3. Lưu vào ChromaDB tại thư mục `cad_db/`

> **Cập nhật RAG sau khi thêm tài liệu mới:** chạy `python scripts/update_rag.py`

### Bước 6: Khởi chạy hệ thống

**Windows:**

```bash
.\start_localhelpbot.bat
```

Script này sẽ tự động:
1. Khởi chạy **Agentic Proxy** trên `http://localhost:11435`
2. Khởi chạy **Discord Gateway** (nếu đã cấu hình token)
3. Mở **Web UI** trên trình duyệt tại `http://localhost:11435`

**Linux/Mac (chạy thủ công):**

```bash
# Terminal 1: Proxy + Web UI
python core/proxy.py

# Terminal 2 (tuỳ chọn): Discord Gateway
python core/discord_gateway.py
```

Sau đó mở trình duyệt tại `http://localhost:11435`.

---

## Cách sử dụng

### Web UI (http://localhost:11435)

Giao diện Web có 5 tab:

| Tab | Chức năng |
|---|---|
| **Chat** | Chat với AI. Chọn agent từ dropdown rồi nhắn tin |
| **Agents** | Xem và chỉnh sửa system prompt, model của từng agent |
| **Connect** | Cấu hình Discord Bot Token, Server ID, Allowed Channels |
| **Daily Tasks** | Quản lý các tác vụ tự động (automation scheduler) |
| **Change Mode** | Chuyển đổi giữa Cloud API và Local Ollama |

### Các virtual model (chọn trong dropdown Chat)

| Model | Mô tả |
|---|---|
| `auto-agent` | **Mặc định.** Smart Router — tự phân tích request và giao cho agent phù hợp |
| `cad-rag` | Chuyên gia CAD/AutoCAD — tra cứu RAG knowledge base |
| `code-agent` | Agent code — đọc file, chạy lệnh, sửa bug tự động (agentic loop) |
| `web-creep` | Agent web — tìm kiếm DuckDuckGo, fetch URL, nghiên cứu online |
| `browser-agent` | Đọc cookies/sessions/LocalStorage từ Chrome/Edge/Brave local |
| `deep-agent` | Sử dụng model lớn (`glm-4.7-flash`) cho suy luận phức tạp |
| `ui-agent` | Chuyên gia UI/Frontend |

### Tích hợp với IDE (VS Code + Continue)

Cấu hình extension **Continue** để trỏ đến proxy:

- **Ollama endpoint:** `http://localhost:11435`
- **Model name:** chọn một trong các virtual model ở trên (vd: `auto-agent`)

Proxy sẽ hiện tất cả virtual model + model Ollama thật trong danh sách model của Continue.

### Tích hợp MCP Server (Claude Code)

File `core/mcp_server.py` cung cấp MCP server cho Claude Code. Cấu hình trong Claude Code settings:

```json
{
  "mcpServers": {
    "rag-cad": {
      "command": "python",
      "args": ["core/mcp_server.py"],
      "cwd": "/path/to/LocalHelpBot"
    }
  }
}
```

MCP server cung cấp 2 tools: `query_rag` (truy vấn RAG) và `list_docs` (liệt kê tài liệu đã index).

### Automation Tasks (Discord)

Trong `config.py`, thêm task vào `AUTOMATION_TASKS`:

```python
AUTOMATION_TASKS = [
    {
        "id": "morning_report",
        "schedule": "08:00",  # Giờ chạy (HH:MM)
        "prompt": "Summarize all new documentation added to the system yesterday.",
        "recipient": 123456789012345678  # Discord Channel ID
    }
]
```

Bot sẽ tự động chạy prompt và gửi kết quả vào Discord channel đã chỉ định.

---

## Cấu trúc thư mục

```
LocalHelpBot/
├── config.py                  # File cấu hình duy nhất — sửa file này khi clone về
├── core/                      # Logic chính
│   ├── proxy.py               # HTTP Proxy server (port 11435) — điểm vào chính
│   ├── orchestrator.py        # Multi-Agent orchestrator — điều phối agents
│   ├── agent.py               # Agentic loop — tự chạy tool, tự sửa lỗi
│   ├── providers.py           # Smart Model Provider — API fallback sang Local
│   ├── query.py               # RAG query engine — truy vấn ChromaDB
│   ├── tools.py               # Tools: read_file, write_file, run_command, search_web, fetch_url, grep_file
│   ├── browser.py             # Tools đọc cookies/sessions/storage từ browser local
│   ├── discord_gateway.py     # Discord Bot client
│   ├── scheduler.py           # Automation scheduler cho daily tasks
│   └── mcp_server.py          # MCP server cho Claude Code
├── data/                      # Pipeline xử lý tài liệu
│   ├── indexer.py             # Entry point: chunk → embed → store
│   ├── chunker.py             # Chunk tài liệu (code, markdown, PDF...)
│   ├── embedder.py            # Embed chunks qua Ollama API
│   └── storage.py             # Lưu vào ChromaDB
├── scripts/
│   ├── update_rag.py          # Cập nhật RAG khi thêm docs mới
│   └── process_data.py        # Script phụ xử lý data
├── docs/                      # Thư mục chứa tài liệu cho RAG knowledge base
├── HelpBotUI/                 # Web UI (HTML/CSS/JS)
│   ├── css/styles.css
│   └── js/main.js
├── static/                    # Web UI standalone (legacy)
│   ├── index.html
│   └── script.js
├── start_localhelpbot.bat     # Script khởi chạy toàn bộ hệ thống (Windows)
├── start_rag_proxy.bat        # Script chạy proxy riêng (Windows)
└── .gitignore
```

---

## Xử lý lỗi thường gặp

| Lỗi | Nguyên nhân | Cách sửa |
|---|---|---|
| `Connection refused localhost:11434` | Ollama chưa chạy | Mở Ollama app hoặc chạy `ollama serve` |
| `Model not found` | Chưa pull model | Chạy `ollama pull <tên-model>` |
| `RAG database path not found: cad_db` | Chưa build RAG | Chạy `python data/indexer.py` |
| `No module named 'chromadb'` | Chưa cài dependencies | Activate venv rồi chạy lại `pip install ...` |
| `Invalid Discord Token` | Token sai trong config.py | Kiểm tra lại `DISCORD_TOKEN` |
| `FileNotFoundError: HelpBotUI/index.html` | Thiếu file UI | Đảm bảo có file `HelpBotUI/index.html` hoặc dùng `static/index.html` |
| Port 11435 đã bị chiếm | Có process khác dùng port | Đổi `PROXY_PORT` trong `config.py` hoặc kill process cũ |

---

## Tóm tắt lệnh nhanh

```bash
# 1. Clone
git clone <repo-url> && cd LocalHelpBot

# 2. Pull models
ollama pull qwen3.5 && ollama pull mxbai-embed-large && ollama pull glm-4.7-flash

# 3. Setup Python
python -m venv venv && venv\Scripts\activate
pip install chromadb pycryptodome pywin32 langchain-core langchain-text-splitters langchain-community tiktoken pypdf docx2txt unstructured requests discord.py

# 4. Sửa config.py (model name, API key nếu cần, Discord token nếu cần)

# 5. Build RAG
python data/indexer.py

# 6. Chạy
.\start_localhelpbot.bat
# Mở http://localhost:11435
```
