---
phase: testing
title: Testing Strategy
description: Define testing approach, test cases, and quality assurance
---

# Testing Strategy

## Test Coverage Goals

- Dự án single-user local, chưa có test suite tự động → coverage chính **bằng manual smoke test** + integration flow.
- Mục tiêu ngắn hạn: unit test cho `core/tool_schema.py`, `core/secrets.py`, `core/providers.py` (fallback logic).
- Mục tiêu dài hạn: integration test cho agentic loop (fake provider + stub tools).

## Unit Tests

### core/secrets.py
- [ ] Windows DPAPI: encrypt → decrypt → roundtrip trên cùng user account.
- [ ] Linux/Mac Fernet: encrypt → decrypt với `.secret_key` mới tạo.
- [ ] Error: decrypt sai user/key → raise `Could not decrypt api_key`.

### core/tool_schema.py
- [ ] Registry: register + lookup + list.
- [ ] `load_plugins()`: plugin hợp lệ → tool được register.
- [ ] `load_plugins()`: plugin lỗi import → log + skip, plugin khác vẫn load.
- [ ] `install_package` validator: accept `pandas`, `numpy[extra]`, `requests>=2`; reject URL, `git+...`, path.

### core/providers.py
- [ ] SmartProvider: primary OK → dùng primary.
- [ ] SmartProvider: primary HTTP 401 → fallback sang Ollama trong suốt 1 request.
- [ ] SmartProvider: cả hai fail → raise error rõ ràng.

### core/permissions.py
- [ ] Queue request → resolve `approved=True` → tool chạy.
- [ ] Timeout 120s → mặc định deny.
- [ ] Scope "session" → lần 2 cùng path/cmd không prompt lại.

## Integration Tests

- [ ] Full agentic loop với fake provider: user prompt → 2 tool calls parallel → final answer.
- [ ] `/api/generate` end-to-end với virtual agent `cad-rag` → RAG hit → response.
- [ ] `/api/permissions/*` roundtrip: pending → resolve → tool continue.
- [ ] Ollama offline → health-check endpoint báo lỗi rõ ràng, không 500.
- [ ] Heartbeat: không ping trong 45s → proxy tự tắt.

## End-to-End Tests

- [ ] Golden path: start bat → UI mở → chat với `auto-agent` → agent gọi `read_file` → trả lời.
- [ ] Cloud → Local fallback: bắt đầu với Anthropic key hết quota → tự switch sang Ollama, không crash session.
- [ ] Change Mode: đổi provider qua UI → Apply → next request dùng provider mới không cần restart.
- [ ] `install_package` flow: `python_exec` với `pandas` → ModuleNotFoundError → hint → modal approve → retry OK.
- [ ] Discord: Connect → bot online → mention → bot reply trong allowed channel; channel khác → silent.
- [ ] Regression: virtual agent list trong `/api/tags` đầy đủ; VS Code Continue vẫn dùng được.

## Test Data

- RAG fixtures: 3–5 PDF nhỏ trong `docs/` (có sẵn `AutoCAD_RAG_DeepDive.pdf`, `autocad_jig_rag.pdf`, `frontend-basics.md`, `winform-patterns.md`, `wpf-mvvm.md`).
- Fake provider stub cho SmartProvider test.
- Test Ollama model nhỏ nhất: `qwen2.5:0.5b` để CI nhanh (nếu setup CI sau).

## Test Reporting & Coverage

- Chưa có pipeline coverage. Khi thêm: `pytest --cov=core --cov-report=term-missing`, threshold 80% cho `core/`.
- Manual test log ghi trong PR description.

## Manual Testing

- **UI smoke** (5 tab): Chat gửi/nhận, Agents sửa prompt save, Connect toggle Discord, Daily Tasks list, Change Mode apply.
- **Permission modal**: thử mọi scope (Deny / Allow once / Session / Always).
- **Trình duyệt**: Chrome + Edge (chính) tối thiểu.
- **Shutdown paths**: close tab, power off button, `/api/shutdown` POST.

## Performance Testing

- Benchmark `eval rate` model chat phải ≥ 10 tok/s trên máy target.
- RAG query P95 < 3s cho corpus 100 docs.
- First token P95: < 2s (cloud), < 5s (local 7B).

## Bug Tracking

### Known issues / common errors

| Lỗi | Nguyên nhân | Cách sửa |
|---|---|---|
| `Connection refused localhost:11434` | Ollama chưa chạy | Mở Ollama app hoặc `ollama serve` |
| `Model not found` | Chưa pull | `ollama pull <model>` |
| `RAG database path not found: cad_db` | Chưa build RAG | `python data/indexer.py` |
| `No module named 'chromadb'` | Chưa cài deps | Activate venv rồi pip install |
| `Invalid Discord Token` | Token sai | Kiểm tra `DISCORD_TOKEN` |
| `FileNotFoundError: HelpBotUI/index.html` | Thiếu UI file | Đảm bảo file tồn tại |
| Port 11435 bị chiếm | Process khác | Đổi `PROXY_PORT` hoặc kill process |
| `HTTP 401 Unauthorized` | Key sai / nhầm provider | Check format: `sk-ant-` / `sk-` / `AIza-` |
| `HTTP 400` từ Google | Model không tồn tại / safety block | Log `[google] HTTP 400` — dùng model hợp lệ |
| `PERMISSION_DENIED: user declined ...` | Từ chối modal / 120s timeout | Retry và Allow |
| "Failed to fetch" giữa chừng | Request quá dài, UI tắt | Đã fix với ThreadingHTTPServer + heartbeat 45s |
| `Could not decrypt api_key` | Đổi máy/user (DPAPI) / mất `.secret_key` | Xoá `runtime_overrides.json`, nhập lại key |

- **Severity**: crash / data loss = P0; fallback fail = P1; UI quirk = P2.
- **Regression**: sau mỗi PR touching `core/agent.py` hoặc `core/providers.py`, chạy lại Golden path + fallback E2E.

## Known limitations

- Agent memory "stupid" (commit `38658a8`) — refactor theo M6 planning.
- Linux/Mac chưa có start script, chỉ chạy qua python trực tiếp.
