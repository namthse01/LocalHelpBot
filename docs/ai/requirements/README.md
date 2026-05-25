---
phase: requirements
title: Requirements & Problem Understanding
description: Clarify the problem space, gather requirements, and define success criteria
---

# Requirements & Problem Understanding

## Problem Statement

- Cloud LLM workflows (Claude / GPT / Gemini) are powerful nhưng phụ thuộc mạng, quota, và chi phí. Khi hết token hoặc offline, developer mất hoàn toàn công cụ AI.
- Người dùng ảnh hưởng: developer / power user Windows cần trợ lý AI làm việc với codebase cục bộ, tài liệu CAD, và bot Discord nội bộ.
- Tình trạng hiện tại: dùng từng tool riêng (Claude Code, ChatGPT web, script RAG rời rạc), không có fallback local, không có permission layer cho các thao tác ghi file / chạy lệnh.

## Goals & Objectives

**Primary goals**
- Multi-agent system chạy local, kết hợp RAG + Agentic loop tự sửa lỗi.
- Fallback tự động từ Cloud API (Anthropic / OpenAI / Google Gemini & Gemma) sang Ollama local khi hết quota hoặc mất mạng.
- Đổi provider / model / API key ngay trong Web UI (tab **Change Mode**), API key mã hoá tại chỗ (DPAPI trên Windows, Fernet trên Linux/Mac).
- Permission modal cho các thao tác rủi ro (write_file, run_command, install_package, …).

**Secondary goals**
- Tích hợp Discord Bot, MCP server cho Claude Code, và VS Code (Continue) qua cùng một proxy `localhost:11435`.
- Plugin system để thêm tool mới không sửa core.
- One-file packaging qua [build_exe.py](../../../build_exe.py) → `dist/TheAgent0.exe`.

**Non-goals**
- Không bundle Ollama (phải cài ngoài).
- Không hosted / multi-tenant — chỉ chạy single-user local.
- Không thay thế training / fine-tuning pipeline.

## User Stories & Use Cases

- **As a developer**, I want to chat với agent hiểu codebase để nó đọc file, chạy lệnh, sửa bug tự động — nhưng phải xin phép trước khi ghi hoặc chạy shell.
- **As a CAD engineer**, I want to hỏi về tài liệu AutoCAD/Jig nội bộ mà không upload lên cloud → `cad-rag` agent truy vấn ChromaDB local.
- **As a power user**, I want đổi giữa Claude Sonnet (cloud) và Qwen3.5 (local) trong 2 click khi hết quota, không restart.
- **As a team**, I want Discord bot tự gửi báo cáo sáng 08:00 (automation scheduler).
- **Edge cases**: mất mạng giữa chừng → fallback sang Ollama; API key sai format → báo lỗi rõ ràng; user từ chối modal xin phép → tool trả `PERMISSION_DENIED`.

## Success Criteria

- Proxy khởi chạy < 5s, Web UI mở tự động tại `http://localhost:11435`.
- Model chat local đạt tối thiểu **10 tokens/s** trên máy cấu hình trung bình (16GB RAM).
- Fallback Cloud → Local trong suốt 1 request khi API lỗi, không crash session.
- Mọi `write_file` / `run_command` đều qua modal trong ≤ 120s timeout.
- RAG query trả về < 3s cho corpus ~100 tài liệu.

## Constraints & Assumptions

- **Technical**: Windows 10/11 chính thức (Linux/Mac cần đổi `.bat` → `.sh`). Python 3.10+. Ollama bắt buộc chạy ở port 11434. RAM ≥ 8GB.
- **Business / policy**: API key không bao giờ được commit; runtime_overrides.json nằm trong `.gitignore`; mã hoá bắt buộc khi ghi đĩa.
- **Assumption**: User đã có Ollama và pull được ít nhất 1 chat model + `mxbai-embed-large`.

## Questions & Open Items

- Có nên auto-detect GPU VRAM để suggest model mặc định không?
- Linux/Mac parity: cần script `.sh` tương đương `start_theagent0.bat`.
- Plugin marketplace / signed plugins — tương lai.
