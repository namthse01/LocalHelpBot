---
phase: monitoring
title: Monitoring & Observability
description: Define monitoring strategy, metrics, alerts, and incident response
---

# Monitoring & Observability

## Key Metrics

### Performance
- **Response latency** — header `X-Response-Time-Ms` đính kèm mọi chat response (xem DevTools).
- **Model used** — header `X-Model-Used` cho biết provider / model thực tế phục vụ (primary hay fallback).
- **Tokens/s** ở phía Ollama: `ollama run <model> --verbose` → `eval rate`.
- **RAG query time** — log trong `proxy.log`.

### Usage / business
- Số request gần nhất + model: `GET /api/stats` (20 mục).
- Tần suất dùng từng virtual agent (rút từ `/api/stats`).
- Số lần fallback cloud → local (log `[provider] fallback triggered`).

### Errors
- HTTP error rate từ cloud (401, 400, 429) — log line `[anthropic|openai|google] HTTP <code>`.
- Permission denied count — log `[permissions] denied <tool>`.
- Plugin load failures — log `[plugins] failed to load <file>: <err>`.

## Monitoring Tools

- **Local only**: không APM bên ngoài. Cần đủ:
  - `proxy.log` — request/response, tool calls, errors.
  - `gateway.log` — Discord gateway events.
  - Browser DevTools → Network tab để xem header + timing.
  - `/api/stats` cho quick glance.
- **Future**: optional OpenTelemetry exporter nếu chạy shared; hiện tại YAGNI.

## Logging Strategy

- **Files**: `proxy.log`, `gateway.log` ở root (gitignored).
- **Levels**: INFO mặc định, DEBUG khi bật env `THEAGENT0_DEBUG=1`.
- **Format**: timestamp + component tag + message, vd `[proxy] POST /api/chat model=auto-agent ms=412`.
- **Retention**: append, rotate manual (chưa tự rotate).
- **Sensitive data**: API key **không bao giờ** log — mask trước khi print. Prompt user có thể log DEBUG, tránh ở INFO nếu chứa PII.

## Alerts & Notifications

Dự án local single-user → không alerting platform. "Alert" = hiển thị ngay trên UI.

### Critical
- **Ollama unreachable** → Chat trả error banner "Connection refused localhost:11434" + hướng dẫn khởi Ollama.
- **Cloud API 401/403** → UI toast "Invalid API key" trong Change Mode.
- **Proxy crash** → heartbeat mất, UI hiện "Disconnected", user restart bằng tay.

### Warning
- **Fallback triggered** → header `X-Model-Used` đổi, UI có thể hiện badge "fallback: ollama/qwen3.5".
- **Heartbeat missing 30s/45s** → proxy self-shutdown countdown log.
- **Plugin load fail** → log warn ở startup, tool vắng mặt trong registry.

## Dashboards

- **Built-in**: `/api/stats` JSON = dashboard primitive.
- **UI `Agents` tab**: xem system prompt + model hiện tại của từng agent.
- **UI header badges**: provider active, model active (nếu implement).
- Custom Grafana / Prometheus: YAGNI cho single-user.

## Incident Response

### On-call rotation
- N/A — single-user. Owner repo tự xử khi gặp issue.

### Incident process
1. **Detection**: UI báo lỗi / proxy log error / Discord bot im.
2. **Triage**: xem `proxy.log` tail, `/api/stats`, header `X-Model-Used`.
3. **Diagnosis**:
   - Ollama down → khởi lại.
   - Cloud 401/429 → đổi provider qua Change Mode.
   - Tool lỗi liên tục → check plugin hoặc disable trong `AGENT_PROFILES`.
   - RAG sai → rebuild `cad_db/`.
4. **Resolution**: fix root cause; nếu lỗi code, patch + rebuild exe.
5. **Post-mortem**: ghi vào commit message hoặc PR description; cập nhật bảng lỗi ở [testing/README.md](../testing/README.md).

## Health Checks

- **Ollama**: `GET http://localhost:11434/api/tags` phải trả 200 + list model.
- **Proxy**: `GET http://localhost:11435/api/tags` trả virtual agent + Ollama model.
- **RAG**: `cad_db/` tồn tại + có file; `query_rag` via MCP hoặc `cad-rag` agent trả kết quả.
- **UI heartbeat**: `POST /api/heartbeat` mỗi 5s; không ping trong 45s → proxy auto-shutdown.
- **Discord** (nếu Connect): `/api/stats` có entry từ Discord channel gần nhất; bot status "online".

## Observability hooks đã có sẵn

| Hook | Mục đích |
|---|---|
| Header `X-Response-Time-Ms` | Latency per request |
| Header `X-Model-Used` | Provider/model thực tế (debug fallback) |
| `/api/stats` | 20 request gần nhất |
| `/api/permissions/pending` | Tool đang chờ approve |
| `proxy.log` / `gateway.log` | Chi tiết request, tool call, error |
