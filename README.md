# TheAgent0

> One AI. Easy to use. Can do everything. Self-learning. Self-evolving.
> (Project formerly known as **LocalHelpBot** — see [docs/ai/vision/lego-architecture.md](docs/ai/vision/lego-architecture.md) for the north star.)


> **v5 (2026-06) — odysseus-grade depth + real speculative decoding.**
> Ports the highest-value subsystems from
> [odysseus](https://github.com/pewdiepie-archdaemon/odysseus) into TheAgent0's
> architecture, keeping our virtual-model proxy, typed `ToolResult` envelopes,
> and Stop-the-Line guard intact:
> - **Self-evolving Skills** — beyond one-line lessons, the agent now distills
>   reusable *procedures* into `data/skills/<slug>/SKILL.md` (when-to-use +
>   steps + pitfalls + verification). Matching skills are keyword-retrieved and
>   auto-injected into T1 of the prompt. Tools: `save_skill`, `list_skills`,
>   `delete_skill`. Auto-extraction after complex runs is opt-in
>   (`SKILLS_AUTO_EXTRACT`, default OFF). See [core/skills.py](core/skills.py).
> - **Deep Research** — a multi-round plan→search→read→synthesize loop that
>   gathers and cites several web sources, then writes a self-contained **visual
>   HTML report** to `data/research/`. Use the `deep_research` tool or the new
>   **`research-agent`** virtual model. See [core/deep_research.py](core/deep_research.py).
> - **Tool & prompt security** — SSRF guard on every outbound fetch
>   (`file://`, cloud-metadata `169.254.x` blocked; loopback allowed for local
>   serving), a prompt-injection policy that treats all external/tool content as
>   *data not instructions*, and **guide-only enforcement** — when you say
>   "don't use any tools" the loop *hard-blocks* tool calls instead of trusting
>   prompt compliance. See [core/security.py](core/security.py).
> - **Adaptive context budget** — compaction threshold is derived from the
>   model's real context window (probed via Ollama `/api/show`) instead of a
>   fixed 6000, so long-context models stop getting throttled.
>   See [core/context_budget.py](core/context_budget.py).
> - **Real DFlash / vLLM backend** — a new `vllm` provider type talks to a local
>   vLLM server running [DFlash](https://arxiv.org/abs/2602.06036) block-diffusion
>   speculative decoding (2-4× faster generation). Launch it with
>   [scripts/serve_vllm_dflash.py](scripts/serve_vllm_dflash.py); point a
>   provider slot at it. NVIDIA/AMD GPU on Linux/WSL2 required — see
>   "vLLM + DFlash" below. On Windows/macOS keep using Ollama.
> - **Tests** — 226 pytest tests (was 66 in v4 docs): adds security, skills,
>   context-budget, deep-research, vLLM/DFlash, and guide-only enforcement.
>
> Total tool count: **51** (was 46 in v4).

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

### One-command install (khuyến nghị)

Bộ cài đặt đa nền tảng tự **dò RAM/CPU/GPU của máy bạn**, chọn đúng cỡ
model phù hợp, rồi tự lo hết: tạo venv → cài deps → cài/khởi động Ollama
→ tải đúng model → bật git hooks → build RAG. Không cần sửa file nào.

```bash
git clone <repo-url> && cd TheAgent0
```

**Windows** — nháy đúp `install.bat`, hoặc trong terminal:

```bat
install.bat
```

**Linux / macOS:**

```bash
chmod +x install.sh && ./install.sh
```

Tự động (không hỏi gì): `install.bat --yes` / `./install.sh --yes`.
Các cờ khác: `--skip-models`, `--skip-rag`, `--skip-ollama`.

Cài xong, khởi chạy:

```bash
.\start_theagent0.bat      # Windows
./start_theagent0.sh       # Linux / macOS
# UI tự mở http://localhost:11435
```

> Xem trước máy bạn sẽ được gán model nào (không cài gì cả):
> `python -m core.hardware`

Bảng chọn model theo phần cứng (lấy VRAM nếu có GPU NVIDIA, ngược lại lấy RAM):

| Ngân sách | Chat | Embed | Vision | Large |
|---|---|---|---|---|
| < 6 GB  | `llama3.2:3b` | `nomic-embed-text` | `moondream` | — |
| 6–12 GB | `qwen2.5-abliterate:7b` | `mxbai-embed-large` | `llava:7b` | — |
| 12–24 GB | `qwen2.5-abliterate:14b` | `mxbai-embed-large` | `llava` | — |
| ≥ 24 GB | `qwen2.5-abliterate:32b` | `mxbai-embed-large` | `llava` | `glm-4.7-flash` |

### Cài thủ công (fallback)

```bash
# Pull models (chọn cỡ theo máy — xem bảng trên)
ollama pull huihui_ai/qwen2.5-abliterate:14b && ollama pull mxbai-embed-large

# Python venv (Windows)
python -m venv venv && venv\Scripts\activate
pip install -r requirements.txt
# Linux/Mac: pip works the same; pywin32 is skipped on non-Windows.

# Activate git hooks (chặn commit nhầm API key / log / runtime_overrides.json).
git config core.hooksPath .githooks

# Build RAG & chạy
python data/indexer.py
.\start_theagent0.bat
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

The `generate_image` tool runs a Hugging Face `diffusers` Stable
Diffusion pipeline **in-process** — no separate WebUI to install. The
tool is dormant until you install the (optional, multi-GB) deps.

One-time install:

```powershell
# CPU-only:
pip install diffusers transformers torch accelerate safetensors

# NVIDIA GPU (recommended — 10-30× faster). Pick the CUDA version
# that matches your driver; cu121 covers most current NVIDIA setups:
pip install torch --index-url https://download.pytorch.org/whl/cu121
pip install diffusers transformers accelerate safetensors
```

Default model is **`stabilityai/sdxl-turbo`** (~6 GB, 1–4 steps,
~10–30 s per image on a 6–12 GB GPU). The first call downloads the
weights to `~/.cache/huggingface/` (~30–90 s of one-time lag);
subsequent calls reuse the cached pipeline in VRAM.

Then ask the bot:

> generate an image of a forest at dawn, 1024x1024

PNGs land under `data/generated/sd-<timestamp>.png`. To swap models,
edit `SD_MODEL_ID` in [config.py](config.py) — examples are in the
inline comments (SD 2.1, SDXL Base, etc.). The plugin auto-tunes
step count, guidance, and size defaults per model.

Safety note: `safety_checker=None` is set in the plugin because this is
a single-user local install. The built-in NSFW filter would otherwise
blur output for adult creative work you've opted into.

### vLLM + DFlash (real speculative decoding, v5)

[DFlash](https://arxiv.org/abs/2602.06036) is a block-diffusion *draft model*
for speculative decoding: a tiny draft proposes 15-16 tokens that the target
model verifies in parallel, giving **2-4× faster generation with identical
output**. It plugs into **vLLM** (not Ollama), so this path needs an
**NVIDIA/AMD GPU on Linux/WSL2**. On Windows/macOS, keep using Ollama.

1. Install vLLM (≥ 0.20.1) in its own venv: `uv pip install -e ".[vllm]"`.
2. Launch a DFlash-accelerated server (the helper picks the matching
   `z-lab/*-DFlash` draft for your model):

   ```bash
   python scripts/serve_vllm_dflash.py --model Qwen/Qwen3.5-27B
   # dry-run the command:   --print     # list supported models:  --list
   ```

3. Point TheAgent0 at it — set a provider slot in `runtime_overrides.json`
   (via the UI **Change Mode** tab) or `config.py:MODEL_PROVIDERS`:

   ```python
   "primary": {"type": "vllm", "model": "Qwen/Qwen3.5-27B",
               "base_url": "http://localhost:8000/v1"}
   ```

The `base_url` defaults to `config.VLLM_BASE` (`http://localhost:8000/v1`).
vLLM speaks the OpenAI API, so DFlash is transparent to TheAgent0 — you just
get faster turns. **Note:** DFlash is a *serving-layer* technique; it cannot be
applied to hosted models (Claude/GPT/Gemini), only to models you serve yourself.

### Switching chat model

The default is `huihui_ai/qwen2.5-abliterate:14b`. To swap, edit
`CHAT_MODEL` in [config.py](config.py) (one place — all 5 agent profiles
reference the constant), OR override at runtime without touching source
by adding a key to `runtime_overrides.json`. The runtime override is
parsed at config.py:337-342 and beats the source default.

Common sizes: `:7b` (~5 GB, fits on most laptops), `:14b` (~9 GB,
default — best writing on 16GB+ RAM), `:32b` (~20 GB, needs strong GPU).

### Creative-mode sampling

`run_agent` auto-detects creative-writing requests (English: "write a
story", "describe a scene", "roleplay", etc.; Vietnamese: "viết truyện",
"kể chuyện", "miêu tả", etc.) and bumps the Ollama sampling preset to
`SAMPLING_CREATIVE` (temperature 0.85, num_predict 4096, top_p 0.95,
repeat_penalty 1.1) for that turn. Tool-use turns stay on the
deterministic `SAMPLING_DEFAULT` (temp 0.3) so `<tool_use>` JSON
doesn't drift. See [core/providers.py](core/providers.py).

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
├── config.py                  # Config duy nhất (system-prompt text → core/agent_prompts.py)
├── core/                      # proxy, orchestrator, agent, providers, tools, plugins/
│   ├── proxy.py               # Ollama-compat HTTP proxy + virtual-model dispatch (ProxyHandler)
│   │                          #   └ split: proxy_virtual / proxy_forward / proxy_prep / proxy_discord / proxy_runtime
│   ├── agent.py               # tool-calling agent loop
│   │                          #   └ split: agent_parsing / agent_compaction / agent_fastpath / agent_results
│   ├── orchestrator.py        # multi-agent router (AgentOrchestrator)
│   │                          #   └ split: orchestrator_capture (per-turn lesson/preference/model-pull)
│   └── agent_prompts.py       # verbatim system-prompt strings used by config.AGENT_PROFILES
├── data/                      # RAG pipeline (chunk, embed, store)
├── scripts/                   # update_rag.py, process_data.py
├── TheAgent0UI/               # Web UI
├── docs/                      # RAG corpus + ai/ phase docs
├── build_exe.py               # One-file packaging
└── start_theagent0.bat        # Launcher
```

> The `proxy_*`, `agent_*`, `orchestrator_capture`, and `agent_prompts` modules are
> behavior-preserving extractions — each symbol is re-exported from its original
> module, so `import core.proxy`, `core.agent`, `core.orchestrator`, and `config`
> are unchanged.

Xem đầy đủ trong [docs/ai/implementation/README.md](docs/ai/implementation/README.md#code-structure).
