---
phase: deployment
title: Deployment Strategy
description: Define deployment process, infrastructure, and release procedures
---

# Deployment Strategy

## Infrastructure

- **Local-first, single-user.** Không có cloud hosting — mỗi user chạy trên máy riêng.
- **Components trên máy user:**
  - Ollama daemon (port `11434`) — external dependency, cài riêng từ https://ollama.com/.
  - TheAgent0 proxy (port `11435`) — chạy qua `.exe` đã build hoặc dev venv.
  - ChromaDB embed tại thư mục `cad_db/` (tự tạo khi index).
- **Không có staging / production split** — môi trường duy nhất là local user machine. "Dev" = chạy từ venv; "Release" = chạy từ `dist/TheAgent0.exe`.

## Deployment Pipeline

### Build process

One-file packaging qua [build_exe.py](../../../build_exe.py):

```bash
python build_exe.py
# → dist/TheAgent0.exe
```

- Đóng gói `core/`, `data/`, `scripts/`, `TheAgent0UI/`, `config.py` vào single exe (PyInstaller one-file).
- **Không bundle** Ollama (license/size) — user phải cài riêng.
- Asset UI (`TheAgent0UI/index.html`) embedded dưới dạng resource.

### CI/CD

- Chưa có CI tự động. Release hiện tại = manual: `python build_exe.py` → test thủ công → commit `dist/TheAgent0.exe` nếu muốn ship.
- Future: GitHub Actions build trên `windows-latest`, upload artifact.

## Environment Configuration

### Development

- Chạy trực tiếp từ venv: `.\start_theagent0.bat` tự detect — nếu không có `dist/TheAgent0.exe` thì chạy `python core/proxy.py` với venv.
- Config: sửa trực tiếp [config.py](../../../config.py).
- Hot-reload: đổi provider/model/key qua Web UI → Change Mode → Apply (không cần restart).

### Staging

- N/A (single-env).

### Production (user machine)

- Chạy từ `dist/TheAgent0.exe` qua `start_theagent0.bat` (auto fallback dev nếu exe thiếu).
- Config runtime persist trong `runtime_overrides.json` ở cùng thư mục exe.
- Log: `proxy.log`, `gateway.log` ở cùng thư mục.

## Deployment Steps

1. **Pre-deployment checklist**
   - [ ] Ollama đang chạy (`curl http://localhost:11434/api/tags`).
   - [ ] Đã pull chat model + `mxbai-embed-large`.
   - [ ] RAG đã build (`cad_db/` tồn tại).
   - [ ] `config.py` đã set `CHAT_MODEL`, `EMBED_MODEL` đúng.
2. **Execution**
   - Chạy `.\start_theagent0.bat`.
   - Chờ UI tự mở `http://localhost:11435`.
   - (Optional) Vào **Change Mode** nhập API key cloud → Apply.
3. **Post-deployment validation**
   - Chat với `auto-agent` → nhận response.
   - Vào **Connect** → test Discord (nếu dùng).
   - Check `/api/stats` xem model đã dùng đúng.
4. **Rollback**: xoá `runtime_overrides.json` → reset về mặc định `config.py`; hoặc rollback git + rebuild exe.

## Database Migrations

- **ChromaDB** là file-based, không schema migration. Khi đổi embedding model (vd `mxbai-embed-large` → khác), phải **rebuild toàn bộ index** vì vector dimension đổi: xoá `cad_db/` + `python data/indexer.py`.
- **Backup**: copy thư mục `cad_db/` trước khi rebuild.
- `runtime_overrides.json` dạng JSON flat — không migration; khi breaking change schema, xoá file và re-enter config qua UI.

## Secrets Management

- **API key cloud**:
  - Không commit trong `config.py` — dùng Web UI → Change Mode → Apply.
  - Mã hoá trước khi ghi `runtime_overrides.json`:
    - Windows: DPAPI qua `win32crypt` — gắn user account, không passphrase, không portable giữa máy/user.
    - Linux/Mac: Fernet (AES-128) với key ở `.secret_key` (cũng gitignored).
  - In-memory: giải mã lại để gọi API.
- **Discord token**: hiện tại ở `config.py::DISCORD_TOKEN` — tránh commit; dự kiến migrate sang flow encrypted runtime override.
- **Key rotation**: user tự đổi qua UI (API key thật), `.secret_key` không rotate tự động.
- **Load priority**:
  1. `config.py` (default).
  2. `runtime_overrides.json` đè lên `MODEL_PROVIDERS`, `AGENT_PROFILES`, `DISCORD_SETTINGS`, `AUTOMATION_TASKS`.
  3. Apply Mode Changes = update in-memory + ghi đĩa + reload `smart_provider`.

## Rollback Plan

- **Trigger rollback khi**: proxy không start được, chat luôn 500, Change Mode gây config corrupt.
- **Steps**:
  1. Stop proxy (UI power off hoặc kill process).
  2. Xoá / rename `runtime_overrides.json` → reset về `config.py` defaults.
  3. Nếu lỗi code: `git checkout <last-known-good>` → rebuild exe nếu cần.
  4. Nếu RAG corrupt: xoá `cad_db/` → `python data/indexer.py` lại.
- **Communication**: single-user không cần — just restart. Nếu share build trong nhóm, note version ở README release.
