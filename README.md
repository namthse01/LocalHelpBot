# Local AI Agent System — RAG + Agentic Proxy cho VS Code (Continue)

> **Mục tiêu:** Chạy một bộ AI agents hoàn toàn local (không cần internet, không tốn tiền API)
> trực tiếp trong VS Code thông qua extension **Continue**, dùng model local qua **Ollama**.

---

## Tổng quan kiến trúc

```
VS Code (Continue extension)
        │
        │  chat requests  (port 11435)
        ▼
  rag_proxy.py  ◄──── virtual model router
        │
        ├─► cad-rag       → ChromaDB RAG → qwen2.5-coder:7b (port 11434)
        ├─► ui-agent      → UI/Frontend system prompt → qwen2.5-coder:7b
        ├─► code-agent    → Agentic loop (file + web tools) → qwen2.5-coder:7b
        ├─► web-creep     → Agentic loop (search + fetch) → qwen2.5-coder:7b
        ├─► browser-agent → Đọc Chrome/Edge cookies & localStorage local
        └─► (pass-through) → Ollama port 11434 (qwen2.5-coder:7b, llama3.2:3b...)
```

**Luồng hoạt động:**
1. Continue gửi request tới `localhost:11435` (proxy)
2. Proxy phân loại theo tên model:
   - `cad-rag` → query ChromaDB → inject context vào prompt → gửi Ollama
   - `code-agent` / `web-creep` / `browser-agent` → chạy **agentic loop** (LLM tự gọi tools)
   - Model khác → pass-through thẳng sang Ollama `:11434`

---

## Yêu cầu hệ thống

| Thứ | Version | Ghi chú |
|-----|---------|---------|
| Python | 3.10+ | Cần `X \| Y` union type syntax |
| Ollama | bất kỳ | Chạy local, port 11434 |
| VS Code | bất kỳ | Cần extension Continue |
| Continue | 0.8+ | Extension ID: `Continue.continue` |
| RAM | 8GB+ | 7B model cần ~5GB VRAM hoặc RAM |

---

## Cài đặt từ đầu

### 1. Clone repo

```bash
git clone <repo-url>
cd rag
```

### 2. Tạo virtual environment và cài dependencies

```bash
python -m venv venv

# Windows
venv\Scripts\pip install chromadb pycryptodome pywin32

# Linux/Mac
venv/bin/pip install chromadb pycryptodome
# (pywin32 chỉ cần trên Windows để decrypt Chrome cookies)
```

### 3. Cài Ollama và pull models

```bash
# Cài Ollama tại: https://ollama.com
ollama pull qwen2.5-coder:7b      # model chính — dùng cho tất cả agents
ollama pull nomic-embed-text       # embedding model — dùng cho ChromaDB RAG
ollama pull llama3.2:3b            # model nhỏ, trả lời nhanh (optional)
```

### 4. Build ChromaDB knowledge base (CAD/AutoCAD)

Bỏ file tài liệu PDF/MD vào thư mục `docs/`, rồi:

```bash
venv\Scripts\python embed.py
```

> File embed sẽ chunk tài liệu và lưu vào `cad_db/` (ChromaDB local).
> Chạy lại bất cứ khi nào thêm tài liệu mới.

### 5. Cài Continue extension trong VS Code

1. Mở VS Code → Extensions → search `Continue` → Install
2. Copy file `~/.continue/config.json` từ repo này (xem mục cấu hình bên dưới)

### 6. Khởi động proxy

```bash
# Cách 1: double-click
start_rag_proxy.bat

# Cách 2: terminal
venv\Scripts\python rag_proxy.py
```

Terminal sẽ in:
```
[rag-proxy] http://localhost:11435
  cad-rag       -> RAG + qwen2.5-coder:7b
  ui-agent      -> UI system + qwen2.5-coder:7b
  code-agent    -> agentic file/cmd/web loop
  web-creep     -> agentic web search/fetch loop
  browser-agent -> local Chrome/Edge cookies & storage reader
```

---

## Cấu hình Continue (`~/.continue/config.json`)

File config của Continue **không nằm trong repo** (nó ở `C:\Users\<user>\.continue\config.json`).
Copy nội dung sau vào đó:

```json
{
  "models": [
    { "title": "Qwen2.5 Coder 7B (Local)", "provider": "ollama", "model": "qwen2.5-coder:7b", "apiBase": "http://localhost:11435" },
    { "title": "CAD / AutoCAD Agent",       "provider": "ollama", "model": "cad-rag",           "apiBase": "http://localhost:11435" },
    { "title": "UI / Frontend Agent",        "provider": "ollama", "model": "ui-agent",          "apiBase": "http://localhost:11435" },
    { "title": "Code Agent (fix/edit/run)",  "provider": "ollama", "model": "code-agent",        "apiBase": "http://localhost:11435" },
    { "title": "Web Creep (search & browse)","provider": "ollama", "model": "web-creep",         "apiBase": "http://localhost:11435" },
    { "title": "Browser Agent",              "provider": "ollama", "model": "browser-agent",     "apiBase": "http://localhost:11435" },
    { "title": "Llama3.2 3B Fast",           "provider": "ollama", "model": "llama3.2:3b",       "apiBase": "http://localhost:11435" }
  ],
  "tabAutocompleteModel": {
    "title": "Qwen2.5 Coder 7B Autocomplete",
    "provider": "ollama", "model": "qwen2.5-coder:7b",
    "apiBase": "http://localhost:11434"
  },
  "embeddingsProvider": {
    "provider": "ollama", "model": "nomic-embed-text",
    "apiBase": "http://localhost:11434"
  },
  "contextProviders": [
    { "name": "code" }, { "name": "docs" }, { "name": "diff" },
    { "name": "terminal" }, { "name": "problems" },
    { "name": "folder" }, { "name": "codebase" }
  ],
  "customCommands": [
    { "name": "fix",          "description": "Tự phân tích lỗi và fix",           "prompt": "Phân tích lỗi sau, xác định nguyên nhân, và viết code fix:\n{{{ input }}}" },
    { "name": "debug",        "description": "Debug step-by-step",                "prompt": "Debug step by step:\n1. Hành vi mong đợi\n2. Hành vi thực tế\n3. Nguyên nhân\n4. Fix\n\nVấn đề: {{{ input }}}" },
    { "name": "search-error", "description": "Tra lỗi và suggest fix",            "prompt": "Lỗi sau có nghĩa gì và cách fix:\n{{{ input }}}" },
    { "name": "improve",      "description": "Review và cải thiện code",          "prompt": "Review code này, tìm bug/performance issues/clarity issues, rồi viết lại tốt hơn:\n{{{ input }}}" }
  ],
  "experimental": {
    "modelContextProtocolServers": [{
      "transport": {
        "type": "stdio",
        "command": "d:/Nam_Work/yolo/rag/venv/Scripts/python.exe",
        "args": ["d:/Nam_Work/yolo/rag/mcp_server.py"]
      }
    }]
  }
}
```

> **Lưu ý path MCP server:** Đổi `d:/Nam_Work/yolo/rag/` thành đường dẫn thực của bạn.

---

## Mô tả từng file

```
rag/
├── rag_proxy.py          # HTTP server (port 11435) — router chính
│                         # Định nghĩa virtual models, system prompts
│                         # Xử lý CAD/UI/code/web/browser agents
│
├── agent_loop.py         # Agentic loop engine
│                         # - Gọi Ollama, parse ACTION:{...}
│                         # - Phát hiện lỗi tool → inject recovery hints
│                         # - Tự động hướng dẫn model search web khi gặp lỗi lạ
│                         # - MAX_TURNS=15, error streak protection
│
├── tools.py              # Tool implementations cho agents
│                         # FILE_TOOLS: read_file, write_file, list_dir,
│                         #             run_command (60s timeout), grep_file
│                         # WEB_TOOLS:  search_web (DuckDuckGo), fetch_url
│                         # ALL_TOOLS:  FILE_TOOLS + WEB_TOOLS
│
├── browser_tools.py      # Đọc dữ liệu browser local (Chrome/Edge/Brave)
│                         # - list_browser_profiles
│                         # - read_browser_cookies (decrypt AES-GCM v10)
│                         # - read_browser_storage (LocalStorage/LevelDB)
│                         # - read_browser_sessions (tab URLs)
│                         # NOTE: v20 App-Bound (Chrome 127+) không decrypt được
│
├── rag_query.py          # Query ChromaDB → trả về top-N documents
├── rag_update.py         # Thêm document mới vào ChromaDB
├── embed.py              # Đọc docs/ → chunk → embed → lưu vào cad_db/
├── chunk.py              # Text chunking logic
├── store.py              # ChromaDB store wrapper
├── mcp_server.py         # MCP server — expose RAG query cho Claude Code
├── main.py               # CLI test / entry point
│
├── docs/                 # Tài liệu nguồn cho RAG (PDF, MD)
│   ├── AutoCAD_RAG_DeepDive.pdf
│   ├── autocad_jig_rag.pdf
│   ├── frontend-basics.md
│   ├── winform-patterns.md
│   └── wpf-mvvm.md
│
├── cad_db/               # ChromaDB vector store (GITIGNORED — build từ embed.py)
├── venv/                 # Python venv (GITIGNORED)
├── personal_avatar/      # Ảnh nhân sự (GITIGNORED)
│
├── start_rag_proxy.bat   # Khởi động proxy (Windows)
├── start_rag_proxy.vbs   # Khởi động proxy ẩn (Windows, không có console window)
│
├── .github/agents/
│   └── web-crawler.agent.md   # Agent definition cho GitHub Actions / Copilot
│
└── ui-frontend.agent.md  # System prompt / hướng dẫn cho UI agent
```

---

## Cách dùng từng agent trong Continue

Nhấn `Ctrl+L` để mở chat, chọn model từ dropdown:

### CAD / AutoCAD Agent (`cad-rag`)
Hỏi về AutoCAD .NET API, C# code, Jig, v.v.
Tự động query ChromaDB → inject context → trả lời có source.
```
Làm thế nào để vẽ một đường thẳng bằng AutoCAD .NET API?
Jig trong AutoCAD hoạt động như thế nào?
```

### UI / Frontend Agent (`ui-agent`)
WinForms, WPF/XAML, MVVM, HTML/CSS/JS/React/Vue.
```
Viết một WPF DataGrid với MVVM binding
Tạo form đăng nhập WinForms đơn giản
```

### Code Agent (`code-agent`)
**Agent tự động:** đọc file, chạy lệnh, tìm web khi lỗi, tự fix.
```
Đọc file main.py và tìm bug trong hàm process_data()
Chạy tests và fix lỗi nếu có
Cài package thiếu và chạy lại script
```

### Web Creep (`web-creep`)
**Agent tự động:** tìm kiếm DuckDuckGo, đọc nội dung trang web.
```
Tìm cách cài đặt ChromaDB trên Python 3.12
Tài liệu về LangChain RetrievalQA là gì?
```

### Browser Agent (`browser-agent`)
Đọc cookies, LocalStorage, session tabs từ Chrome/Edge/Brave local.
```
Tôi đang đăng nhập những site nào trên Chrome?
Đọc cookies của github.com
LocalStorage của localhost:3000 có gì?
```

### Slash Commands (tất cả models)
Gõ `/` trong chat:
- `/fix` — phân tích lỗi và viết code fix
- `/debug` — debug step-by-step có cấu trúc
- `/search-error` — tra lỗi unfamiliar
- `/improve` — review và rewrite code

---

## Cách thêm tài liệu vào RAG (CAD knowledge base)

1. Bỏ file PDF hoặc Markdown vào `docs/`
2. Chạy: `venv\Scripts\python embed.py`
3. Restart proxy

Để xóa và build lại từ đầu:
```bash
rmdir /s /q cad_db
venv\Scripts\python embed.py
```

---

## Troubleshooting

### Continue báo "Model not found"
- Proxy chưa chạy → chạy `start_rag_proxy.bat`
- Proxy đang dùng code cũ → kill python.exe và chạy lại
- Kiểm tra: `curl http://localhost:11435/api/tags` → phải thấy `browser-agent` trong list

### Proxy crash ngay sau khi start
- Port 11435 bị chiếm → `netstat -ano | findstr 11435` → kill process đó
- Ollama chưa chạy (port 11434) → start Ollama trước

### Cookie decrypt báo "v20 App-Bound"
- Chrome 127+ dùng App-Bound Encryption, **không thể decrypt** từ ngoài Chrome.
- Metadata (domain, tên cookie, expiry) vẫn đọc được bình thường.
- Giải pháp: dùng Chrome DevTools → Application → Cookies để xem giá trị.

### ChromaDB không tìm thấy tài liệu
- Chạy lại `embed.py` sau khi thêm tài liệu
- Kiểm tra `SCORING_THRESHOLD = 0.3` trong `rag_proxy.py` — giảm nếu muốn kết quả lỏng hơn

### `search_web` không trả kết quả
- DuckDuckGo thỉnh thoảng thay đổi HTML → regex fallback sẽ tự kick in
- Nếu vẫn lỗi: dùng `fetch_url` với URL trực tiếp từ documentation

---

## Kiến trúc chi tiết — agentic loop

```python
# agent_loop.py — cách hoạt động
while turn < MAX_TURNS (15):
    response = ollama_chat(conversation)

    if "ACTION: {...}" in response:
        tool_name, args = parse_action(response)
        result = tools[tool_name](args)

        if is_error(result):           # phát hiện ERROR:, Traceback, EXIT:1...
            error_streak += 1
            if error_streak >= 4:      # quá nhiều lỗi liên tiếp → tổng kết
                inject("Tổng kết lỗi, không thử nữa")
            else:
                inject(recovery_hint)  # hướng dẫn: fix path / install / search_web
        else:
            error_streak = 0
            inject("Kết quả tool, tiếp tục nếu cần")
    else:
        return response                # câu trả lời cuối cùng
```

---

## Dependencies cần thiết

```
chromadb          # Vector database — CAD RAG
pycryptodome      # AES-GCM decrypt Chrome v10 cookies
pywin32           # DPAPI decrypt (Windows only) — Chrome cookie master key
```

Standard library (không cần cài):
`sqlite3, json, re, subprocess, urllib, pathlib, tempfile, shutil, ctypes`

---

## Ghi chú bảo mật

- **Browser cookies:** Chỉ đọc dữ liệu local của chính máy bạn. Không gửi ra ngoài.
- **Chrome v20 cookies:** App-Bound Encryption (Chrome 127+) — giá trị cookie không thể đọc được từ ngoài Chrome.
- **Proxy chỉ bind localhost** — không expose ra network ngoài.
- **Không có telemetry** — `allowAnonymousTelemetry: false` trong config.json.
