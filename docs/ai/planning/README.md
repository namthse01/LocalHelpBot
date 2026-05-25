---
phase: planning
title: Project Planning & Task Breakdown
description: Break down work into actionable tasks and estimate timeline
---

# Project Planning & Task Breakdown

## Milestones

- [x] **M1 — v1 core**: proxy + orchestrator + single agent + Ollama only.
- [x] **M2 — v1.5 RAG + Discord**: ChromaDB indexer, Discord gateway, MCP server.
- [x] **M3 — v2 upgrade (2026-04)**: ported claude-code patterns — structured `<tool_use>` blocks, parallel tool calls per turn, `Tool` schema registry ([core/tool_schema.py](../../../core/tool_schema.py)), environment + tool-catalog injection ([core/context.py](../../../core/context.py)), real **Task sub-agent** tool (thay `delegate` ad-hoc). Legacy `ACTION: {...}` vẫn parse back-compat.
- [x] **M4 — Packaging**: one-file build qua [build_exe.py](../../../build_exe.py) → `dist/TheAgent0.exe`; [start_theagent0.bat](../../../start_theagent0.bat) chạy exe nếu có, fallback dev venv.
- [ ] **M5 — Linux/Mac parity**: script `.sh` tương đương, test Fernet path end-to-end.
- [ ] **M6 — Agent self-improve**: cải thiện memory loop (bot "stupid in memory" — ghi chú trong commit `38658a8`).
- [ ] **M7 — Plugin marketplace**: signed plugin loader, versioning.

## Task Breakdown

### Phase 1: Foundation ✅
- [x] 1.1: Proxy ThreadingHTTPServer với Ollama-compatible endpoints.
- [x] 1.2: SmartProvider (Anthropic / OpenAI / Google + Ollama fallback).
- [x] 1.3: Secrets encryption (DPAPI + Fernet).

### Phase 2: Core Features ✅
- [x] 2.1: Agentic loop với tool_use parsing.
- [x] 2.2: Permission modal + 4 scope levels.
- [x] 2.3: RAG pipeline (indexer, chunker, embedder, storage).
- [x] 2.4: Plugin system + `install_package` self-extension.

### Phase 3: Integration & Polish
- [x] 3.1: Web UI với 5 tab.
- [x] 3.2: Discord gateway + automation scheduler.
- [x] 3.3: MCP server cho Claude Code.
- [x] 3.4: One-file packaging.
- [ ] 3.5: Agent memory redesign — ưu tiên cao.
- [ ] 3.6: Linux/Mac start script.
- [ ] 3.7: Auto-detect GPU/RAM để suggest default model.

## Dependencies

- **External runtime**: Ollama daemon port `11434` (không bundle được).
- **Python deps**: `chromadb`, `pycryptodome`, `pywin32` (Windows) / `cryptography` (Linux/Mac), `langchain-core`, `langchain-text-splitters`, `langchain-community`, `tiktoken`, `pypdf`, `docx2txt`, `unstructured`, `requests`, `discord.py`. Optional: `reportlab` (write_pdf), `python-docx` (docx read/write).
- **Task dependencies**: M6 (memory) blocks M7 (plugin marketplace dùng memory để persist state).

## Timeline & Estimates

- M1–M4: đã hoàn thành tính đến 2026-04-14.
- M5 (Linux/Mac): ~1 tuần effort — chủ yếu test + script.
- M6 (memory): 2–3 tuần — cần redesign context injection.
- M7 (marketplace): 3–4 tuần sau khi M6 xong.

## Risks & Mitigation

- **Ollama API change**: pin version trong docs + health-check ở startup.
- **DPAPI mất key khi đổi Windows user**: document workaround (xoá `runtime_overrides.json`, re-enter key).
- **Plugin crash core**: đã mitigate — `load_plugins()` catch + log + skip.
- **Cloud quota exhaustion giữa request**: SmartProvider fallback sang Ollama trong suốt.
- **Memory regression**: viết integration test cho multi-turn trước khi refactor M6.

## Resources Needed

- 1 dev chính (owner repo).
- Máy test Windows 11 + Linux (WSL/VM) cho M5.
- GPU 8GB+ khuyến nghị để test các model 7B+.
