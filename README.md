# TheAgent0

> One AI. Easy to use. Can do everything. Self-learning. Self-evolving.
> (Project formerly known as **LocalHelpBot** — see [docs/ai/vision/lego-architecture.md](docs/ai/vision/lego-architecture.md) for the north star.)


> **v4 (2026-05) — ChatGPT-grade UX, deep tools, self-learning.**
> Building on the v3 reliability core:
> - **Modern chat UI** — markdown + syntax highlighting (marked + highlight.js),
>   conversation sidebar (`/api/sessions`), slash commands (`/new`, `/agent`,
>   `/remember`, `/help`), drag-and-drop file upload, confidence badges,
>   quick-action chips. Inline in [TheAgent0UI/index.html](TheAgent0UI/index.html).
> - **Deep computer access** — `screenshot`, `clipboard_read/write`,
>   `system_info` (CPU/RAM/GPU/disks/net), `open_with_default_app`,
>   `list_windows` (Windows), `watch_file`, `find_in_files`, `read_env`.
>   See [core/plugins/system_tools.py](core/plugins/system_tools.py).
> - **Web/resource power** — `download_file`, `extract_text` (readability-style),
>   `github_search_repos`, `github_read_file`, `github_releases`,
>   `pypi_search`, `pypi_info`, `youtube_transcript`, `wikipedia_summary`.
>   See [core/plugins/web_extras.py](core/plugins/web_extras.py).
> - **Self-learning** — persistent `LessonsStore` ([core/lessons.py](core/lessons.py))
>   injected into T1 of every system prompt; `save_lesson`, `learn_from_file`,
>   `learn_from_url` tools (the latter two ingest content into the RAG corpus);
>   `update_self` for `git pull --ff-only` on main; opt-in auto-capture of
>   user corrections; opt-in auto-pull of missing Ollama models. Feature flags
>   in [config.py](config.py): `LESSONS_AUTO_CAPTURE`, `UPDATE_SELF_ENABLED`,
>   `OLLAMA_AUTO_PULL` (all default OFF).
> - **Multimodal** — `describe_image` + `screenshot_and_describe` route to
>   Ollama's vision model (default `llava:latest`, configurable via
>   `VISION_MODEL`). New `vision-agent` virtual model.
>   See [core/plugins/vision_tools.py](core/plugins/vision_tools.py).
> - **Test suite** — 66 pytest tests covering memory, tool envelope,
>   Stop-the-Line, config schema, files_touched, and lessons. Run via
>   `python -m pytest tests/`.
>
> Total tool count: **46** (was 22 in v3).

> **v3 (2026-05) — Reliability & UX upgrade.** Borrows patterns from
> [openclaw](https://github.com/) and the
> [How_AI_Learn](https://github.com/) skills collection:
> - **Memory continuity** — server-side `ConversationStore` keyed by
>   `X-Session-Id`; 4-tier context engine ([core/context.py](core/context.py))
>   replaces the old "blow away the system prompt on every turn" pattern.
> - **Typed tool envelopes** — `ToolResult` + `ErrorCode` + per-code
>   recovery hints ([core/tool_schema.py](core/tool_schema.py)).
> - **Stop-the-Line guard** — same call failing twice → 3rd attempt
>   blocked with `LOOP_BUDGET` ([core/agent.py](core/agent.py)).
> - **Structured permission previews** — diff for `edit_file`, shell
>   for `run_command`, hostname highlight for `fetch_url`, risk levels.
> - **Subsystem logging** — colored console + JSONL file + Logs tab
>   ([core/logs.py](core/logs.py), `/api/logs/tail`).
> - **Typed config** — pydantic v2 validation; a typo in
>   `AGENT_PROFILES` now fails at import with a pointed error.
> - **Agentic RAG** — `query_rag` is now a first-class tool the agent
>   CHOOSES to call, not auto-injected. Optional **Chain-of-Verification**
>   per profile (`"verify": "high"`).
> - **Healthcheck** — `python -m core.healthcheck` (and `/api/healthcheck`)
>   validates Ollama, models, RAG dim, sessions dir, port.
>
> See [docs/ai/testing/troubleshooting.md](docs/ai/testing/troubleshooting.md)
> for the v3 failure-mode catalogue.

> **v2 (2026-04):** ported core patterns from claude-code — structured `<tool_use>` blocks with **parallel tool calls per turn**, `Tool` schema registry ([core/tool_schema.py](core/tool_schema.py)), environment + tool-catalog injection ([core/context.py](core/context.py)), real **Task sub-agent** tool replacing the ad-hoc `delegate`. Legacy `ACTION: {...}` still parsed for back-compat. One-file packaging via [build_exe.py](build_exe.py) → `dist/TheAgent0.exe`; [start_theagent0.bat](start_theagent0.bat) runs the exe if built, else the dev venv. Ollama remains an external dependency.

Hệ thống AI Multi-Agent chạy local, kết hợp RAG + Agentic Loop tự sửa lỗi + Multi-Model Orchestration. Fallback tự động từ Cloud API (**Anthropic / OpenAI / Google Gemini & Gemma**) sang Ollama local khi hết token hoặc mất mạng. Đổi provider / model / API key trực tiếp trong Web UI (tab **Change Mode**) — API key mã hoá tại chỗ (DPAPI trên Windows, Fernet trên Linux/Mac). Tích hợp Discord Bot, MCP server cho Claude Code, và VS Code (Continue).

---

## Quick start

```bash
git clone <repo-url> && cd TheAgent0

# Pull models
ollama pull huihui_ai/qwen2.5-abliterate:7b && ollama pull mxbai-embed-large && ollama pull glm-4.7-flash
# (The default CHAT_MODEL is an abliterated Qwen 2.5 — refusals removed at the
# weights level, intended for single-user local use. Swap models any time —
# see "Switching chat model" below.)

# Python venv (Windows)
python -m venv venv && venv\Scripts\activate
pip install -r requirements.txt
# Linux/Mac: pip works the same; pywin32 is skipped on non-Windows.

# Activate git hooks (chặn commit nhầm API key / log / runtime_overrides.json).
# Chạy 1 lần sau khi clone:
git config core.hooksPath .githooks

# Sửa config.py (CHAT_MODEL, EMBED_MODEL). API key nhập trong Web UI → Change Mode.

# Build RAG & chạy
python data/indexer.py
.\start_theagent0.bat
# UI tự mở http://localhost:11435
```

### Healthcheck (v3)

Run this FIRST when something looks off — it validates Ollama, models,
RAG dim, sessions dir, and port:

```powershell
.\venv\Scripts\python -m core.healthcheck
```

Output is a colored pass/fail/warn table. The Web UI shows the same
banner at the top of the **Change Mode** tab.

### Image generation (optional)

The `generate_image` tool calls a local **AUTOMATIC1111 Stable Diffusion
WebUI** at `http://127.0.0.1:7860`. It's dormant until A1111 is running.

One-time setup:

```bash
git clone https://github.com/AUTOMATIC1111/stable-diffusion-webui
cd stable-diffusion-webui
# Drop a .safetensors / .ckpt checkpoint into models/Stable-diffusion/
```

Enable the API — edit `webui-user.bat` (Windows) and set:

```
set COMMANDLINE_ARGS=--api
```

Then launch with `webui-user.bat`. When you see
`Running on local URL: http://127.0.0.1:7860`, ask the bot:

> generate an image of a forest at dawn, 768x512

PNGs land under `data/generated/sd-<timestamp>.png`. Override the
endpoint via `A1111_BASE` in `config.py` if you run A1111 elsewhere.

### Switching chat model

The default is `huihui_ai/qwen2.5-abliterate:7b`. To swap, edit
`CHAT_MODEL` in [config.py](config.py) (one place — all 5 agent profiles
reference the constant), OR override at runtime without touching source
by adding a key to `runtime_overrides.json`. The runtime override is
parsed at config.py:337-342 and beats the source default.

### Agent catalog

See [AGENTS.md](AGENTS.md) for a one-page guide to every virtual model
(`auto-agent`, `code-agent`, `cad-rag`, `ui-agent`, `web-creep`,
`browser-agent`, `deep-agent`) — when to use which, example prompts,
gotchas.

### Bảo mật API key

- Key nhập qua Web UI (tab **Change Mode**) — mã hoá bằng DPAPI (Win) / Fernet (Linux/Mac) trước khi ghi xuống `runtime_overrides.json`.
- File đó + `.secret_key` + `.env` + `*.log` đã có trong `.gitignore`. Hook ở `.githooks/pre-commit` chặn commit nhầm (file hoặc content match `sk-ant-`, `sk-proj-`, `sk-`, `AIza`, `enc:v1:`).
- Không bao giờ paste key vào `config.py` hay bất kỳ file tracked nào — dùng Web UI hoặc env var (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`).

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
TheAgent0/
├── config.py                  # Config duy nhất
├── core/                      # proxy, orchestrator, agent, providers, tools, plugins/
├── data/                      # RAG pipeline (chunk, embed, store)
├── scripts/                   # update_rag.py, process_data.py
├── TheAgent0UI/               # Web UI
├── docs/                      # RAG corpus + ai/ phase docs
├── build_exe.py               # One-file packaging
└── start_theagent0.bat        # Launcher
```

Xem đầy đủ trong [docs/ai/implementation/README.md](docs/ai/implementation/README.md#code-structure).
