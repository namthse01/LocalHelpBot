# LocalHelpBot

Hệ thống AI Multi-Agent chạy local, kết hợp RAG (Retrieval-Augmented Generation), Agentic Loop tự sửa lỗi, và Multi-Model Orchestration. Hỗ trợ fallback tự động từ Cloud API (**Anthropic / OpenAI / Google Gemini & Gemma**) sang Ollama local khi hết token hoặc mất mạng. Đổi provider / model / API key trực tiếp trong Web UI (tab **Change Mode**) — API key được mã hoá tại chỗ (DPAPI trên Windows, Fernet trên Linux/Mac). Agent có bộ công cụ đầy đủ (đọc file, ghi file, chạy lệnh, tìm kiếm web, fetch URL), các thao tác ghi/chạy lệnh sẽ **hỏi xin phép bạn qua modal** trước khi thực hiện. Tích hợp Discord Bot và Web UI.

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

**Tìm model phù hợp nhất cho máy của bạn:**

Mỗi máy có cấu hình khác nhau (RAM, GPU, CPU) nên không thể dùng chung một model. Cách tìm model tốt nhất:

1. **Kiểm tra tài nguyên máy:**

```bash
# Windows — xem RAM & GPU
systeminfo | findstr /C:"Total Physical Memory"
nvidia-smi          # nếu có GPU NVIDIA
```

```bash
# Linux/Mac
free -h             # RAM
nvidia-smi          # GPU NVIDIA
```

2. **Bảng chọn model theo phần cứng:**

| RAM | GPU VRAM | Chat Model (khuyến nghị) | Deep Model |
|---|---|---|---|
| 8GB | Không có | `llama3.2:3b` hoặc `qwen2.5:3b` | _(bỏ trống)_ |
| 8GB | 4-6GB | `qwen2.5:7b` | _(bỏ trống)_ |
| 16GB | Không có | `qwen3.5` hoặc `gemma3:12b` | _(bỏ trống)_ |
| 16GB | 8GB+ | `qwen2.5-coder:7b` hoặc `qwen3.5` | `qwen3.5` |
| 32GB+ | 12GB+ | `qwen3.5` hoặc `glm-4.7-flash` | `glm-4.7-flash` |
| 64GB+ | 24GB+ | `glm-4.7-flash` hoặc `qwen2.5:32b` | `llama3.3:70b` |

3. **Benchmark nhanh — so sánh tốc độ trên máy bạn:**

```bash
# Pull 2-3 model ứng viên rồi test
ollama pull qwen3.5
ollama pull gemma3:12b

# Đo tốc độ sinh text (tokens/giây)
ollama run qwen3.5 "Giải thích machine learning trong 100 từ" --verbose
ollama run gemma3:12b "Giải thích machine learning trong 100 từ" --verbose
```

Xem dòng `eval rate` ở cuối output — model nào cho **tokens/s cao hơn** mà vẫn trả lời chất lượng thì chọn model đó.

> **Nguyên tắc chung:** Model chạy dưới **5 tokens/s** sẽ rất chậm khi dùng thực tế. Nên chọn model cho tốc độ tối thiểu **10 tokens/s** trở lên.

4. **Model embedding (bắt buộc cho RAG):** Luôn dùng `mxbai-embed-large` — chỉ 669MB, chạy được trên mọi máy.

```bash
ollama pull mxbai-embed-large
```

Sau khi chọn xong, kiểm tra các model đã pull:

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

> **Lưu ý cho Linux/Mac:** bỏ `pywin32` và **thay bằng `cryptography`** (dùng để mã hoá API key khi không có DPAPI của Windows):
>
> ```bash
> pip install chromadb pycryptodome cryptography langchain-core langchain-text-splitters langchain-community tiktoken pypdf docx2txt unstructured requests discord.py
> ```

### Bước 4: Cấu hình `config.py`

Mở file `config.py` tại thư mục gốc và chỉnh các giá trị sau:

**4a. Model chat chính** — sửa `CHAT_MODEL` cho khớp model đã pull:

```python
CHAT_MODEL = "qwen3.5:latest"       # Model dùng cho tất cả agents
LARGE_MODEL = "glm-4.7-flash:latest" # Model lớn cho deep-agent (để "" nếu không có)
EMBED_MODEL = "mxbai-embed-large:latest"  # Model embedding cho RAG
```

**4b. API Key (tuỳ chọn — có thể đặt trong `config.py` HOẶC nhập sau trong Web UI):**

> **Cách khuyên dùng:** bỏ qua bước này và nhập trực tiếp ở Web UI → tab **Change Mode** sau khi khởi động. API key sẽ được **mã hoá** và lưu vào `runtime_overrides.json` (đã nằm trong `.gitignore`). Không bao giờ commit API key vào git.

Nếu vẫn muốn set mặc định trong `config.py`:

```python
MODEL_PROVIDERS = {
    "primary": {
        "type": "api",
        "provider": "anthropic",  # "anthropic" | "openai" | "google"
        "api_key": "sk-ant-xxxxx",  # điền API key thật (hoặc để ENV var ANTHROPIC_API_KEY)
        "model": "claude-3-5-sonnet-20240620",
    },
    "fallback": {
        "type": "local",
        "provider": "ollama",
        "model": "qwen3.5:latest",
    }
}
```

**Các model API được hỗ trợ sẵn:**

| Provider | Ví dụ model | Định dạng API key |
|---|---|---|
| `anthropic` | `claude-3-5-sonnet-20240620`, `claude-sonnet-4-5`, `claude-opus-4-20250514` | `sk-ant-...` |
| `openai` | `gpt-4o-mini`, `gpt-4o`, `o1-mini` | `sk-...` |
| `google` | `gemma-3-27b-it`, `gemma-3-12b-it`, `gemini-2.5-flash`, `gemini-2.5-pro` (free tier) | `AIza...` |

Nếu **chỉ muốn dùng local** (không cần API key), đổi primary thành:

```python
"primary": {
    "type": "local",
    "provider": "ollama",
    "model": "qwen3.5:latest",
},
```

> Hoặc trong Web UI → **Change Mode** → chọn **"Local Only (Ollama)"**, dropdown sẽ liệt kê các model đã pull, chọn xong nhấn **Apply Mode Changes**.

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

> **Lưu ý:** Hệ thống **không tự chạy** khi mở VS Code. Bạn cần khởi động thủ công bằng một trong các cách bên dưới.

Mở terminal trong thư mục project rồi gõ:

```bash
.\start_localhelpbot.bat
```

Có thể dùng terminal bên ngoài (cmd, PowerShell) hoặc terminal tích hợp trong VS Code — đều được.

Proxy sẽ khởi chạy và tự mở Web UI trên trình duyệt tại `http://localhost:11435`.

> **Tắt:** Đóng tab UI trên trình duyệt → hệ thống tự tắt toàn bộ sau 15 giây. Hoặc nhấn nút **power off** trên góc phải UI để tắt ngay.
>
> **Discord / Dịch vụ bên thứ 3:** Không tự kết nối khi khởi động. Vào Web UI → tab **Connect** → nhấn nút **Connect** để kết nối khi cần.

---

## Cách sử dụng

### Web UI (http://localhost:11435)

Giao diện Web có 5 tab:

| Tab | Chức năng |
|---|---|
| **Chat** | Chat với AI. Chọn agent từ dropdown rồi nhắn tin |
| **Agents** | Xem và chỉnh sửa system prompt, model của từng agent |
| **Connect** | Cấu hình và kết nối Discord Bot (nhấn Connect để bật, Disconnect để tắt) |
| **Daily Tasks** | Quản lý các tác vụ tự động (automation scheduler) |
| **Change Mode** | Đổi provider / model / API key (Anthropic, OpenAI, Google Gemini/Gemma, Local Ollama). Khi chọn "Local Only" sẽ hiện dropdown các model Ollama đã pull. API key được mã hoá khi lưu. |

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

## Bộ công cụ của Agent & Hệ thống xin phép

Agent chính có các tool sau và sẽ tự gọi khi cần:

| Tool | Loại | Có xin phép? |
|---|---|---|
| `read_file`, `list_dir`, `grep_file` | Đọc file cục bộ | ❌ (read-only) |
| `search_web`, `fetch_url` | Tìm kiếm / đọc trang web | ❌ (read-only) |
| `write_file` | Ghi file vào ổ đĩa | ✅ **hỏi qua modal** |
| `run_command` | Chạy shell command | ✅ **hỏi qua modal** |
| `delegate` | Giao việc cho specialist khác | ❌ |

**Modal xin phép** hiện ra trong Web UI mỗi khi agent muốn ghi file hoặc chạy lệnh, với 4 lựa chọn:

- **Deny** — từ chối lần này
- **Allow once** — cho phép đúng lần này
- **Allow for session** — cho phép tool này trên đúng path/command đó cả session
- **Always (this tool)** — cho phép blanket tool này trong cả session

Nếu bạn không phản hồi trong 120 giây, mặc định là **từ chối**.

---

## Mã hoá API key & Runtime Overrides

Khi bạn click **Apply Mode Changes** trong Web UI, thay đổi được ghi vào file `runtime_overrides.json` ở gốc project (đã nằm trong `.gitignore`):

- **API key được mã hoá** trước khi ghi xuống đĩa:
  - **Windows:** dùng DPAPI qua `win32crypt` — gắn với user account, không passphrase.
  - **Linux/Mac:** dùng Fernet (AES-128) với key lưu ở `.secret_key` (cũng đã gitignore).
- Trong bộ nhớ, API key được giải mã lại để gọi API bình thường.
- UI chỉ nhận/gửi giá trị **đã che** (`••••••••`) khi bạn đã nhập key trước đó. Nếu không sửa, giá trị cũ được giữ nguyên.

Cơ chế ưu tiên khi load config:

1. `config.py` đọc trước (giá trị mặc định).
2. Nếu có `runtime_overrides.json` → đè lên `MODEL_PROVIDERS`, `AGENT_PROFILES`, `DISCORD_SETTINGS`, `AUTOMATION_TASKS`.
3. `Apply Mode Changes` cập nhật in-memory **và** ghi đĩa, đồng thời reload `smart_provider` mà không cần restart.

> Nếu muốn reset về mặc định: xoá `runtime_overrides.json` và restart.

---

## Endpoints nội bộ (cho debug)

| Endpoint | Method | Mục đích |
|---|---|---|
| `/api/config` | GET/POST | Đọc / cập nhật cấu hình (API key luôn bị mask ở GET) |
| `/api/stats` | GET | 20 request gần nhất + thời gian xử lý, model đã dùng |
| `/api/tags` | GET | Danh sách model Ollama + các virtual agent |
| `/api/permissions/pending` | GET | Các yêu cầu xin phép đang chờ |
| `/api/permissions/resolve` | POST | `{id, approved, scope}` — trả lời modal |
| `/api/heartbeat` | POST | UI ping (mặc định 5s). Nếu không ping trong 45s → proxy tự tắt |
| `/api/shutdown` | POST | Tắt toàn bộ hệ thống |

Header `X-Response-Time-Ms` và `X-Model-Used` được đính kèm mọi response chat — xem trong DevTools để đo độ trễ.

---

## Cấu trúc thư mục

```
LocalHelpBot/
├── config.py                  # File cấu hình duy nhất — sửa file này khi clone về
├── core/                      # Logic chính
│   ├── proxy.py               # HTTP Proxy server (port 11435) — multi-thread, điểm vào chính
│   ├── orchestrator.py        # Multi-Agent orchestrator — điều phối agents
│   ├── agent.py               # Agentic loop — tự chạy tool, tự sửa lỗi, có timing
│   ├── providers.py           # Smart Model Provider — Anthropic/OpenAI/Google + Ollama fallback
│   ├── secrets.py             # Mã hoá API key (DPAPI trên Windows, Fernet trên Linux/Mac)
│   ├── permissions.py         # Hàng đợi xin phép cho write_file / run_command
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
├── HelpBotUI/                 # Web UI
│   └── index.html             # Giao diện chính (script + CSS inline)
├── config.py                  # Cấu hình mặc định (checked in)
├── runtime_overrides.json     # (tự tạo) Ghi đè runtime từ Web UI — KHÔNG commit, chứa API key mã hoá
├── .secret_key                # (tự tạo, Linux/Mac) Fernet key cho mã hoá — KHÔNG commit
├── start_localhelpbot.bat     # Script khởi chạy toàn bộ hệ thống (Windows)
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
| `FileNotFoundError: HelpBotUI/index.html` | Thiếu file UI | Đảm bảo có file `HelpBotUI/index.html` |
| Port 11435 đã bị chiếm | Có process khác dùng port | Đổi `PROXY_PORT` trong `config.py` hoặc kill process cũ |
| `HTTP 401 Unauthorized` từ API | API key sai, hoặc key không khớp provider (vd key Google nhập vào provider Anthropic) | Check lại provider/key format: `sk-ant-...` (Anthropic), `sk-...` (OpenAI), `AIza...` (Google) |
| `HTTP 400 Bad Request` từ Google | Model không tồn tại (vd `gemma-4`) hoặc bị safety-block | Xem log `[google] HTTP 400 — body: ...` ở console. Dùng model hợp lệ như `gemma-3-27b-it` |
| `PERMISSION_DENIED: user declined ...` | Bạn đã từ chối modal xin phép hoặc bỏ 120s không phản hồi | Thử lại và bấm "Allow" |
| UI báo "Failed to fetch" giữa chừng | Request quá dài, UI đã tắt. Không còn xảy ra sau khi chuyển sang ThreadingHTTPServer + heartbeat 45s | Restart proxy. Nếu tái diễn, tăng `HEARTBEAT_TIMEOUT` trong `core/proxy.py` |
| `Could not decrypt api_key` khi start | Đã đổi máy/user Windows (DPAPI bị khoá theo account) hoặc mất `.secret_key` | Xoá `runtime_overrides.json` và nhập lại key qua Web UI |

---

## Tóm tắt lệnh nhanh

```bash
# 1. Clone
git clone <repo-url> && cd LocalHelpBot

# 2. Pull models
ollama pull qwen3.5 && ollama pull mxbai-embed-large && ollama pull glm-4.7-flash

# 3. Setup Python (Windows)
python -m venv venv && venv\Scripts\activate
pip install chromadb pycryptodome pywin32 langchain-core langchain-text-splitters langchain-community tiktoken pypdf docx2txt unstructured requests discord.py
#    Linux/Mac — thay `pywin32` bằng `cryptography`:
# pip install chromadb pycryptodome cryptography langchain-core langchain-text-splitters langchain-community tiktoken pypdf docx2txt unstructured requests discord.py

# 4. Sửa config.py (model Ollama mặc định, Discord token nếu cần)
#    API key có thể nhập trực tiếp trong Web UI → tab Change Mode (sẽ được mã hoá)

# 5. Build RAG
python data/indexer.py

# 6. Chạy (mở terminal trong project folder)
.\start_localhelpbot.bat
# Mở http://localhost:11435 — vào tab Change Mode để chọn provider/model/API key
```
