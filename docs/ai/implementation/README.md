---
phase: implementation
title: Implementation Guide
description: Technical implementation notes, patterns, and code guidelines
---

# Implementation Guide

## Development Setup

### Yêu cầu hệ thống

| Thành phần | Yêu cầu |
|---|---|
| OS | Windows 10/11 (khuyến nghị). Linux/Mac cần chỉnh `.bat` → `.sh` |
| Python | **3.10+** (`python --version`) |
| RAM | Tối thiểu 8GB; 16GB+ khuyến nghị |
| Ollama | Bắt buộc — https://ollama.com/ |
| Git | Để clone repo |

### Bước 1 — Clone

```bash
git clone <repo-url>
cd LocalHelpBot
```

### Bước 2 — Cài Ollama & pull model

```bash
ollama pull qwen3.5
ollama pull mxbai-embed-large
ollama pull glm-4.7-flash
```

**Chọn model theo phần cứng:**

| RAM | GPU VRAM | Chat Model | Deep Model |
|---|---|---|---|
| 8GB | None | `llama3.2:3b` / `qwen2.5:3b` | — |
| 8GB | 4–6GB | `qwen2.5:7b` | — |
| 16GB | None | `qwen3.5` / `gemma3:12b` | — |
| 16GB | 8GB+ | `qwen2.5-coder:7b` / `qwen3.5` | `qwen3.5` |
| 32GB+ | 12GB+ | `qwen3.5` / `glm-4.7-flash` | `glm-4.7-flash` |
| 64GB+ | 24GB+ | `glm-4.7-flash` / `qwen2.5:32b` | `llama3.3:70b` |

Benchmark nhanh: `ollama run <model> "..." --verbose` → xem `eval rate`. Nguyên tắc: model < 5 tok/s quá chậm; nên ≥ 10 tok/s. Embedding luôn dùng `mxbai-embed-large` (669MB).

### Bước 3 — venv + dependencies

**Windows:**
```bash
python -m venv venv
venv\Scripts\activate
pip install chromadb pycryptodome pywin32 langchain-core langchain-text-splitters langchain-community tiktoken pypdf docx2txt unstructured requests discord.py
```

**Linux/Mac** (thay `pywin32` bằng `cryptography`):
```bash
python3 -m venv venv && source venv/bin/activate
pip install chromadb pycryptodome cryptography langchain-core langchain-text-splitters langchain-community tiktoken pypdf docx2txt unstructured requests discord.py
```

### Bước 4 — Cấu hình [config.py](../../../config.py)

**4a. Model Ollama mặc định**
```python
CHAT_MODEL = "qwen3.5:latest"
LARGE_MODEL = "glm-4.7-flash:latest"      # "" nếu không có
EMBED_MODEL = "mxbai-embed-large:latest"
```

**4b. API Key** — khuyên dùng: bỏ qua bước này, nhập trong Web UI → tab **Change Mode** (sẽ được mã hoá, lưu vào `runtime_overrides.json`). Nếu muốn set mặc định:

```python
MODEL_PROVIDERS = {
    "primary": {"type": "api", "provider": "anthropic",
                "api_key": "sk-ant-xxxxx",
                "model": "claude-3-5-sonnet-20240620"},
    "fallback": {"type": "local", "provider": "ollama",
                 "model": "qwen3.5:latest"},
}
```

| Provider | Model ví dụ | Format key |
|---|---|---|
| `anthropic` | `claude-3-5-sonnet-20240620`, `claude-sonnet-4-5`, `claude-opus-4-20250514` | `sk-ant-...` |
| `openai` | `gpt-4o-mini`, `gpt-4o`, `o1-mini` | `sk-...` |
| `google` | `gemma-3-27b-it`, `gemma-3-12b-it`, `gemini-2.5-flash`, `gemini-2.5-pro` | `AIza...` |

Local only: đổi `primary.type` → `local`, `provider` → `ollama`.

**4c. Discord (optional)**
```python
DISCORD_TOKEN = "your-discord-bot-token"
DISCORD_SETTINGS = {
    "guilds": {YOUR_SERVER_ID: {"allowed_channels": [CH_ID], "admin_role_id": None}},
    "default_guild_id": YOUR_SERVER_ID,
    "allow_all_channels": False,
}
```

### Bước 5 — Build RAG

Đặt tài liệu (PDF, MD, TXT, DOCX, HTML, PY, CS, JS, TS, CPP) vào [docs/](../../../docs/), rồi:

```bash
python data/indexer.py
```

Chunk → embed (Ollama `mxbai-embed-large`) → ChromaDB tại `cad_db/`. Cập nhật sau: `python scripts/update_rag.py`.

### Bước 6 — Khởi chạy

```bash
.\start_localhelpbot.bat
```

UI tự mở tại `http://localhost:11435`. Đóng tab → proxy tự tắt sau 15s (heartbeat 45s threshold cho network). Hoặc nhấn nút **power off** góc phải UI.

## Code Structure

```
LocalHelpBot/
├── config.py                  # Config duy nhất — sửa khi clone
├── core/
│   ├── proxy.py               # HTTP proxy :11435, điểm vào chính
│   ├── orchestrator.py        # Multi-agent orchestrator
│   ├── agent.py               # Agentic loop (tool_use, self-fix)
│   ├── providers.py           # SmartProvider (cloud + Ollama fallback)
│   ├── secrets.py             # DPAPI / Fernet
│   ├── permissions.py         # Queue xin phép
│   ├── query.py               # RAG query engine
│   ├── tools.py               # Tools mặc định
│   ├── tool_schema.py         # Tool registry + load_plugins()
│   ├── context.py             # Env + tool catalog injection
│   ├── browser.py             # Đọc cookies/sessions/storage
│   ├── discord_gateway.py     # Discord client
│   ├── scheduler.py           # Automation scheduler
│   ├── mcp_server.py          # MCP server cho Claude Code
│   └── plugins/               # Plugin tools auto-loaded
├── data/
│   ├── indexer.py chunker.py embedder.py storage.py
├── scripts/
│   ├── update_rag.py process_data.py
├── docs/                      # RAG corpus + ai/ phase docs
├── HelpBotUI/index.html       # Web UI (script + CSS inline)
├── runtime_overrides.json     # (tự tạo) gitignored, API key mã hoá
├── .secret_key                # (Linux/Mac) Fernet key, gitignored
├── start_localhelpbot.bat     # Launcher Windows
└── build_exe.py               # One-file packaging
```

**Conventions**: lowercase_snake module name, tool names match plugin file name, config keys UPPER_SNAKE.

## Implementation Notes

### Core Features

- **Agentic loop**: structured `<tool_use>{...}</tool_use>` blocks, parallel tool calls per turn. Legacy `ACTION: {...}` vẫn parse back-compat.
- **SmartProvider**: primary cloud → auto fallback Ollama khi HTTP error / timeout / quota.
- **Permission modal**: 4 scope — Deny / Allow once / Allow for session / Always (this tool). Timeout 120s = deny.
- **Self-extension**: khi tool fail `ModuleNotFoundError: X` hoặc shell `EXIT: 127`, loop chèn hint gọi `install_package` → modal → pip install → retry.

### Virtual agents (dropdown Chat)

| Model | Mô tả |
|---|---|
| `auto-agent` | **Mặc định.** Smart Router — phân tích request, giao cho agent phù hợp |
| `cad-rag` | Chuyên gia CAD/AutoCAD — truy vấn RAG |
| `code-agent` | Đọc file, chạy lệnh, sửa bug tự động |
| `web-creep` | DuckDuckGo search, fetch URL |
| `browser-agent` | Cookies/sessions/LocalStorage Chrome/Edge/Brave |
| `deep-agent` | Model lớn (`glm-4.7-flash`) cho suy luận phức tạp |
| `ui-agent` | UI/Frontend specialist |

### Tool catalog

| Tool | Loại | Xin phép? |
|---|---|---|
| `read_file`, `list_dir`, `grep_file`, `glob_files` | Đọc local | ❌ |
| `search_web`, `fetch_url` | Web read-only | ❌ |
| `write_file`, `edit_file` | Ghi / sửa | ✅ |
| `run_command` | Shell | ✅ |
| `delete_file`, `move_file` (plugin `fs_extra`) | Xoá / move | ✅ |
| `make_dir` (plugin `fs_extra`) | Tạo thư mục | ❌ |
| `python_exec` (plugin `exec_tools`) | Python snippet subprocess | ✅ |
| `list_processes` (plugin `exec_tools`) | ps | ❌ |
| `kill_process` (plugin `exec_tools`) | kill pid | ✅ |
| `install_package` (plugin `package_tools`) | `pip install` PyPI | ✅ **kèm 'reason'** |
| `read_file_chunk` (plugin `document_tools`) | Text lớn range | ❌ |
| `read_pdf` / `write_pdf` | PDF (pypdf / reportlab) | ❌ / ✅ |
| `read_docx` / `write_docx` | Word (python-docx) | ❌ / ✅ |
| `task` / `delegate` | Sub-agent | ❌ |

### Plugin system

Thả file `core/plugins/<name>.py` expose `register(registry)`:

```python
from core.tool_schema import Tool, ToolRegistry
from core.permissions import request_permission

def _handler(args):
    return f"OK: got {args}"

def register(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="my_tool",
        description="What it does.",
        input_schema={"type": "object",
                      "properties": {"x": {"type": "string"}},
                      "required": ["x"]},
        handler=_handler,
        requires_permission=False,
        category="misc",
    ))
```

Restart proxy → thêm `"my_tool"` vào `AGENT_PROFILES[...]["tools"]` trong `config.py`. Plugin lỗi được log + skip, không crash core.

### Example tool flow

```
User: "Đếm số dòng Python trong dự án, group theo thư mục."

Turn 1: PLAN — glob_files + python_exec.
Turn 2: <tool_use>{"name":"glob_files","input":{"pattern":"**/*.py"}}</tool_use>
        → OK: 47 files
Turn 3: <tool_use>{"name":"python_exec","input":{"code":"..."}}</tool_use>
        → modal xin phép → Allow once
        → STDOUT: core/ 12, data/ 3, scripts/ 2 ...
Turn 4: final answer (no tool_use).
```

### `install_package` validation

Regex `[A-Za-z0-9._-]+` + optional `[extras]` và version specifier. **Không** URL, `git+`, path — giảm blast radius.

## Integration Points

- **VS Code Continue**: endpoint `http://localhost:11435`, chọn virtual agent (vd `auto-agent`) làm model name.
- **Claude Code MCP**: `core/mcp_server.py` expose 2 tools — `query_rag`, `list_docs`.
  ```json
  {"mcpServers": {"rag-cad": {
      "command": "python",
      "args": ["core/mcp_server.py"],
      "cwd": "/path/to/LocalHelpBot"}}}
  ```
- **Discord automation**:
  ```python
  AUTOMATION_TASKS = [
      {"id": "morning_report", "schedule": "08:00",
       "prompt": "Summarize all new docs added yesterday.",
       "recipient": CHANNEL_ID}
  ]
  ```

## Error Handling

Xem [testing](../testing/README.md) cho bảng lỗi thường gặp đầy đủ. Nguyên tắc:
- Plugin error → log + skip, không crash.
- Provider error → SmartProvider fallback trong suốt.
- Permission denied / timeout → tool trả `PERMISSION_DENIED: ...`, agent tiếp tục turn với feedback rõ ràng.
- Ollama offline → health-check + error message chỉ ra cách khởi Ollama.

Logs: `proxy.log`, `gateway.log` ở root.

## Performance Considerations

- ThreadingHTTPServer để parallel tool calls trong cùng 1 turn.
- RAG chunk size cân bằng giữa recall và cost embedding.
- Heartbeat 45s để tránh hang tài nguyên khi UI đóng.
- Streaming response cho chat endpoints — giảm TTFB cảm nhận.

## Security Notes

- API key **mã hoá** trước khi ghi đĩa:
  - Windows: DPAPI qua `win32crypt`, gắn user account, không passphrase.
  - Linux/Mac: Fernet (AES-128), key ở `.secret_key` (gitignored).
- In-memory giải mã lại để gọi API; UI chỉ nhận/gửi giá trị đã mask (`••••••••`).
- `runtime_overrides.json` đè config khi load; xoá file → reset mặc định.
- Tất cả write/exec tool đi qua permission layer, không có allowlist ngầm.
