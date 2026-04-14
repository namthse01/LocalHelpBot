# LocalHelpBot

> **v2 (2026-04):** ported core patterns from claude-code — structured `<tool_use>` blocks with **parallel tool calls per turn**, `Tool` schema registry ([core/tool_schema.py](core/tool_schema.py)), environment + tool-catalog injection ([core/context.py](core/context.py)), real **Task sub-agent** tool replacing the ad-hoc `delegate`. Legacy `ACTION: {...}` still parsed for back-compat. One-file packaging via [build_exe.py](build_exe.py) → `dist/LocalHelpBot.exe`; [start_localhelpbot.bat](start_localhelpbot.bat) runs the exe if built, else the dev venv. Ollama remains an external dependency.

Hệ thống AI Multi-Agent chạy local, kết hợp RAG + Agentic Loop tự sửa lỗi + Multi-Model Orchestration. Fallback tự động từ Cloud API (**Anthropic / OpenAI / Google Gemini & Gemma**) sang Ollama local khi hết token hoặc mất mạng. Đổi provider / model / API key trực tiếp trong Web UI (tab **Change Mode**) — API key mã hoá tại chỗ (DPAPI trên Windows, Fernet trên Linux/Mac). Tích hợp Discord Bot, MCP server cho Claude Code, và VS Code (Continue).

---

## Quick start

```bash
git clone <repo-url> && cd LocalHelpBot

# Pull models
ollama pull qwen3.5 && ollama pull mxbai-embed-large && ollama pull glm-4.7-flash

# Python venv (Windows)
python -m venv venv && venv\Scripts\activate
pip install chromadb pycryptodome pywin32 langchain-core langchain-text-splitters langchain-community tiktoken pypdf docx2txt unstructured requests discord.py
# Linux/Mac: thay `pywin32` bằng `cryptography`

# Sửa config.py (CHAT_MODEL, EMBED_MODEL). API key nhập trong Web UI → Change Mode.

# Build RAG & chạy
python data/indexer.py
.\start_localhelpbot.bat
# UI tự mở http://localhost:11435
```

Chi tiết setup + troubleshooting: [docs/ai/implementation/](docs/ai/implementation/README.md) và [docs/ai/testing/](docs/ai/testing/README.md).

---

## Documentation map

Tài liệu đầy đủ tổ chức theo ai-devkit phases trong [docs/ai/](docs/ai/):

| Phase | Nội dung |
|---|---|
| [requirements/](docs/ai/requirements/README.md) | Problem statement, goals, user stories, success criteria, constraints |
| [design/](docs/ai/design/README.md) | Architecture diagram, components, data flow, API design, non-functional reqs |
| [planning/](docs/ai/planning/README.md) | Milestones (v1 → v2 → packaging → roadmap), task breakdown, risks |
| [implementation/](docs/ai/implementation/README.md) | Setup từng bước, code structure, virtual agents, tool catalog, plugin system |
| [testing/](docs/ai/testing/README.md) | Test strategy, E2E flows, bảng lỗi thường gặp đầy đủ |
| [deployment/](docs/ai/deployment/README.md) | Build exe, runtime overrides, secrets encryption, rollback |
| [monitoring/](docs/ai/monitoring/README.md) | Metrics, logs, health checks, incident response |

`docs/` cũng chứa corpus RAG (PDF, MD); đặt thêm tài liệu vào đây rồi chạy `python scripts/update_rag.py` để reindex.

---

## Repo layout (rút gọn)

```
LocalHelpBot/
├── config.py                  # Config duy nhất
├── core/                      # proxy, orchestrator, agent, providers, tools, plugins/
├── data/                      # RAG pipeline (chunk, embed, store)
├── scripts/                   # update_rag.py, process_data.py
├── HelpBotUI/                 # Web UI
├── docs/                      # RAG corpus + ai/ phase docs
├── build_exe.py               # One-file packaging
└── start_localhelpbot.bat     # Launcher
```

Xem đầy đủ trong [docs/ai/implementation/README.md](docs/ai/implementation/README.md#code-structure).
