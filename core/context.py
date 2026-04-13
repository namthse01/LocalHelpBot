"""
Environment + system-context builder, inspired by claude-code's src/context.ts.

Assembles a rich system prompt block with:
  - Platform / cwd / date
  - Git branch + short status
  - Tool catalog (auto-rendered from the Tool registry)
  - Optional CLAUDE.md / AGENT.md project memory

Goal: agents stop "hallucinating" about the environment and have a consistent,
model-agnostic context shape.
"""

from __future__ import annotations

import os
import platform
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

PROJECT_MEMORY_FILES = ("CLAUDE.md", "AGENT.md", ".localhelpbot.md")


def _git(cmd: list[str], cwd: str) -> str:
    try:
        out = subprocess.run(
            ["git", *cmd], cwd=cwd, capture_output=True, text=True, timeout=3,
            encoding="utf-8", errors="replace",
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def get_git_info(cwd: str) -> dict:
    if not (Path(cwd) / ".git").exists():
        return {"is_repo": False}
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    status = _git(["status", "--porcelain"], cwd)
    last = _git(["log", "-1", "--pretty=%h %s"], cwd)
    dirty = bool(status)
    status_lines = status.splitlines()[:10]
    return {
        "is_repo": True,
        "branch": branch or "(detached)",
        "dirty": dirty,
        "status": "\n".join(status_lines),
        "last_commit": last,
    }


def _read_project_memory(cwd: str) -> str:
    parts = []
    for name in PROJECT_MEMORY_FILES:
        p = Path(cwd) / name
        if p.exists() and p.is_file():
            try:
                txt = p.read_text(encoding="utf-8", errors="replace")[:4000]
                parts.append(f"=== {name} ===\n{txt}")
            except Exception:
                pass
    return "\n\n".join(parts)


def build_env_block(cwd: Optional[str] = None) -> str:
    cwd = cwd or os.getcwd()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    os_name = f"{platform.system()} {platform.release()}"
    git = get_git_info(cwd)
    git_line = (
        f"  branch={git['branch']} dirty={git['dirty']} last={git['last_commit']}"
        if git.get("is_repo") else "  (not a git repo)"
    )
    return (
        "<environment>\n"
        f"  cwd: {cwd}\n"
        f"  platform: {os_name}\n"
        f"  date: {now}\n"
        f"  git:\n{git_line}\n"
        "</environment>"
    )


def render_tools_catalog(tools: Iterable) -> str:
    """Render a Tool-object iterable into a readable catalog for the system prompt."""
    lines = ["<tools>"]
    for t in tools:
        schema = getattr(t, "input_schema", {}) or {}
        props = schema.get("properties", {}) if isinstance(schema, dict) else {}
        required = set(schema.get("required", []) if isinstance(schema, dict) else [])
        arg_bits = []
        for name, spec in props.items():
            typ = spec.get("type", "any") if isinstance(spec, dict) else "any"
            mark = "" if name in required else "?"
            arg_bits.append(f"{name}{mark}:{typ}")
        perm = " [ASKS PERMISSION]" if getattr(t, "requires_permission", False) else ""
        lines.append(f"  - {t.name}({', '.join(arg_bits)}){perm} — {t.description}")
    lines.append("</tools>")
    return "\n".join(lines)


def build_system_context(base_prompt: str, tools: Iterable, cwd: Optional[str] = None) -> str:
    """Wrap a base agent system prompt with environment + tool catalog + memory."""
    cwd = cwd or os.getcwd()
    memory = _read_project_memory(cwd)
    env = build_env_block(cwd)
    catalog = render_tools_catalog(tools)
    parts = [base_prompt.strip(), env, catalog]
    if memory:
        parts.append("<project_memory>\n" + memory + "\n</project_memory>")
    parts.append(
        "<tool_use_format>\n"
        "  To call a tool, emit ONE OR MORE JSON blocks wrapped in <tool_use>...</tool_use> tags:\n"
        "    <tool_use>{\"name\": \"read_file\", \"input\": {\"path\": \"x.py\"}}</tool_use>\n"
        "  You may emit MULTIPLE <tool_use> tags in one turn to run tools in parallel.\n"
        "  Legacy format still works: a line starting with  ACTION: {\"tool\":..., ...}\n"
        "  When you're DONE, just write your final answer without any <tool_use> tag.\n"
        "</tool_use_format>"
    )
    return "\n\n".join(parts)
