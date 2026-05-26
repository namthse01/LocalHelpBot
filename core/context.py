"""Context engine — 4-tier system-prompt assembler.

Inspired by `How_AI_Learn/agent-skills/skills/context-engineering` and
`openclaw/src/context-engine/types.ts`. The job of this module is to
take everything that *could* go into the system prompt for one turn and
return a single, budget-aware string the model will actually attend to.

The four tiers, in order of priority (highest first — never pruned):

  T1 Rules     — base agent system prompt + project memory (CLAUDE.md,
                 AGENT.md, .theagent0.md — and .localhelpbot.md kept for
                 backward compatibility with the legacy project name).
                 These are the "always true" facts of how this agent works.
  T2 Session   — user-stated goal, sticky decisions, files touched, and
                 the running conversation summary. Persists across turns.
  T3 Task      — environment block (cwd / OS / date / git), tools
                 catalog, runtime metadata (active provider/model).
                 Rebuilt every turn but cheap.
  T4 Iteration — hints from the most recent tool result(s) — e.g. the
                 last error code and recovery hint. Used to nudge the
                 model away from repeating a failing call.

The public entry point is `assemble_context(...)` which returns an
`AssembledContext` dataclass.

For backward compatibility we still export `build_system_context(...)`
and the other helpers. New call sites should prefer
`assemble_context(...)` because it can consult a `Session` object and
inject T2 data the older API doesn't know about.
"""

from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from core.conversation_store import Session, get_store
from core.memory import (
    CHARS_PER_TOKEN,
    SUMMARY_END,
    SUMMARY_MARKER,
    get_default_engine,
)

PROJECT_MEMORY_FILES = ("CLAUDE.md", "AGENT.md", ".theagent0.md", ".localhelpbot.md")
PROJECT_MEMORY_BYTES = 4000

# Soft caps per tier — when the assembled prompt exceeds these we start
# pruning lower-priority content (T4 first, then T3 cosmetics, then T2
# session detail). T1 never shrinks.
DEFAULT_TIER_BUDGETS = {
    "T1_rules": 2500,    # chars (~900 tokens)
    "T2_session": 1200,  # chars (~430 tokens)
    "T3_task": 2500,     # chars (~900 tokens) — tools catalog dominates
    "T4_iter": 600,      # chars (~210 tokens)
}


# ───────────────────────────────────────────────────────────────────────
# Return type
# ───────────────────────────────────────────────────────────────────────


@dataclass
class AssembledContext:
    """Result of context assembly.

    Callers pass `system` straight to the model as the first message and
    `history` as the rest of the conversation.

    `tier_budgets` and `pruned` are diagnostic — the proxy exposes them
    on the /api/stats/turns endpoint so the UI can show "how full was
    each tier this turn".
    """

    system: str
    history: List[Dict[str, Any]]
    est_tokens: int
    session_id: str
    tier_budgets: Dict[str, int] = field(default_factory=dict)
    pruned: List[str] = field(default_factory=list)
    summary: str = ""  # current summary if any


# ───────────────────────────────────────────────────────────────────────
# Environment helpers (kept for backward compatibility)
# ───────────────────────────────────────────────────────────────────────


def _git(cmd: List[str], cwd: str) -> str:
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
    return {
        "is_repo": True,
        "branch": branch or "(detached)",
        "dirty": bool(status),
        "status": "\n".join(status.splitlines()[:10]),
        "last_commit": last,
    }


def _read_project_memory(cwd: str) -> str:
    parts: List[str] = []
    for name in PROJECT_MEMORY_FILES:
        p = Path(cwd) / name
        if p.exists() and p.is_file():
            try:
                txt = p.read_text(encoding="utf-8", errors="replace")[:PROJECT_MEMORY_BYTES]
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


def render_tools_catalog(tools: Iterable, *, compact: bool = False) -> str:
    """Render an iterable of Tool objects into a system-prompt catalog.

    When `compact=True` we drop per-arg type annotations and limit each
    description to one line — used as a fallback when T3 is over budget.
    """
    lines = ["<tools>"]
    for t in tools:
        schema = getattr(t, "input_schema", {}) or {}
        props = schema.get("properties", {}) if isinstance(schema, dict) else {}
        required = set(schema.get("required", []) if isinstance(schema, dict) else [])

        if compact:
            arg_names = ", ".join(props.keys())
            desc = (getattr(t, "description", "") or "").splitlines()[0][:120]
            perm = " [asks]" if getattr(t, "requires_permission", False) else ""
            lines.append(f"  - {t.name}({arg_names}){perm} — {desc}")
        else:
            arg_bits = []
            for name, spec in props.items():
                typ = spec.get("type", "any") if isinstance(spec, dict) else "any"
                mark = "" if name in required else "?"
                arg_bits.append(f"{name}{mark}:{typ}")
            perm = " [ASKS PERMISSION]" if getattr(t, "requires_permission", False) else ""
            lines.append(f"  - {t.name}({', '.join(arg_bits)}){perm} — {t.description}")
    lines.append("</tools>")
    return "\n".join(lines)


# Tool-use format block — included in T3 so the model knows the wire format.
TOOL_USE_FORMAT = (
    "<tool_use_format>\n"
    "  To call a tool, emit ONE OR MORE JSON blocks wrapped in <tool_use>...</tool_use> tags:\n"
    "    <tool_use>{\"name\": \"read_file\", \"input\": {\"path\": \"x.py\"}}</tool_use>\n"
    "  You may emit MULTIPLE <tool_use> tags in one turn to run tools in parallel.\n"
    "  Legacy format still works: a line starting with  ACTION: {\"tool\":..., ...}\n"
    "  When you're DONE, just write your final answer without any <tool_use> tag.\n"
    "</tool_use_format>"
)


# ───────────────────────────────────────────────────────────────────────
# Tier renderers
# ───────────────────────────────────────────────────────────────────────


def _render_t2_session(session: Optional[Session]) -> str:
    """Render T2 (session metadata) into a `<session>` block.

    Returns "" if there's nothing worth saying — the assembler skips
    empty tiers entirely so the prompt stays focused.
    """
    if session is None:
        return ""
    lines: List[str] = []
    if session.goal:
        lines.append(f"  goal: {session.goal}")
    if session.last_profile:
        lines.append(f"  last_profile: {session.last_profile}")
    if session.files_touched:
        # Keep most-recent 8 files, paths only.
        recent_files = session.files_touched[-8:]
        lines.append("  files_touched:")
        for f in recent_files:
            lines.append(f"    - {f}")
    if session.sticky:
        lines.append("  sticky_decisions:")
        for k, v in session.sticky.items():
            lines.append(f"    {k}: {v}")
    # <recent_exchange> sits at the END of T2 so it's closest to the
    # model's recency window. Drives pronoun resolution ("our last
    # result", "that") on the next turn. Skipped entirely when both
    # slots are empty so fresh sessions stay quiet.
    if session.last_short_answer or session.last_numeric_value is not None:
        lines.append("  recent_exchange:")
        if session.last_short_answer:
            short = session.last_short_answer.replace("\n", " ").strip()
            if len(short) > 240:
                short = "…" + short[-240:]
            short = short.replace('"', "'")
            lines.append(f'    last_assistant_answer: "{short}"')
        if session.last_numeric_value is not None:
            v = session.last_numeric_value
            num_str = str(int(v)) if v == int(v) and abs(v) < 1e16 else f"{v:g}"
            lines.append(f"    last_numeric_value: {num_str}")
    if not lines:
        return ""
    return "<session>\n" + "\n".join(lines) + "\n</session>"


def _render_t4_iteration(last_tool_hints: List[str]) -> str:
    """Render T4 — the last error/recovery hint, if any."""
    if not last_tool_hints:
        return ""
    body = "\n".join(f"  - {h}" for h in last_tool_hints[-3:])
    return f"<iteration_hints>\n{body}\n</iteration_hints>"


def _fit_to_budget(text: str, budget_chars: int) -> tuple[str, bool]:
    """Truncate `text` to ~budget_chars characters, returning (text, truncated)."""
    if len(text) <= budget_chars:
        return text, False
    return text[: budget_chars - 60].rstrip() + "\n…[tier truncated to fit budget]", True


# ───────────────────────────────────────────────────────────────────────
# The main API
# ───────────────────────────────────────────────────────────────────────


def assemble_context(
    *,
    session_id: str,
    base_prompt: str,
    tools: Iterable,
    history: Optional[List[Dict[str, Any]]] = None,
    runtime_block: str = "",
    last_tool_hints: Optional[List[str]] = None,
    cwd: Optional[str] = None,
    tier_budgets: Optional[Dict[str, int]] = None,
    profile_name: Optional[str] = None,
) -> AssembledContext:
    """Build the system prompt + cleaned history for one model call.

    Parameters
    ----------
    session_id:
        Identity for the conversation. Looked up against the global
        ConversationStore to populate T2 (goal, sticky, files touched).
    base_prompt:
        The agent's role prompt (from AGENT_PROFILES). Tier T1 always
        includes this verbatim.
    tools:
        Iterable of Tool objects available this turn. Used for the T3
        tools catalog.
    history:
        Prior conversation messages excluding the system prompt. We may
        merge a summary system message back in here.
    runtime_block:
        Optional `<runtime ... />` line the orchestrator builds (active
        provider, model). Included in T3.
    last_tool_hints:
        Strings produced by the agent loop after recent tool calls;
        e.g. "edit_file failed twice with STALE_OLD_STRING — re-read file
        before retrying". Goes into T4.
    cwd:
        Override working directory (defaults to os.getcwd()).
    tier_budgets:
        Overrides for `DEFAULT_TIER_BUDGETS`. Useful in tests.
    profile_name:
        Stored on the session for "remember which agent was last used".
    """

    cwd = cwd or os.getcwd()
    budgets = {**DEFAULT_TIER_BUDGETS, **(tier_budgets or {})}
    pruned: List[str] = []

    # ── Session lookup (T2) ──────────────────────────────────────────
    store = get_store()
    session = store.get(session_id)
    if profile_name:
        store.note(session_id, profile=profile_name)
        session = store.get(session_id)

    # ── T1: Rules ─────────────────────────────────────────────────────
    t1_parts: List[str] = [(base_prompt or "").strip()]
    project_memory = _read_project_memory(cwd)
    if project_memory:
        t1_parts.append("<project_memory>\n" + project_memory + "\n</project_memory>")
    # v4 Slice 4.2: inject persisted lessons learned from the user.
    try:
        from core.lessons import get_lessons_store
        lessons_block = get_lessons_store().render_for_prompt(session_id=session_id)
        if lessons_block:
            t1_parts.append(lessons_block)
    except Exception:
        # Lessons are best-effort; never block context assembly.
        pass
    t1 = "\n\n".join(t1_parts)

    # ── T2: Session ──────────────────────────────────────────────────
    t2 = _render_t2_session(session)
    t2_full = t2
    if t2 and len(t2) > budgets["T2_session"]:
        t2, was_truncated = _fit_to_budget(t2, budgets["T2_session"])
        if was_truncated:
            pruned.append("T2_session:truncated")

    # ── T3: Task ─────────────────────────────────────────────────────
    env_block = build_env_block(cwd)
    catalog_full = render_tools_catalog(tools, compact=False)
    catalog = catalog_full
    if len(catalog) > budgets["T3_task"]:
        # First try a compact catalog (drops arg types and trims descriptions).
        catalog = render_tools_catalog(tools, compact=True)
        pruned.append("T3_task:catalog_compacted")
        if len(catalog) > budgets["T3_task"]:
            catalog, _ = _fit_to_budget(catalog, budgets["T3_task"])
            pruned.append("T3_task:catalog_truncated")

    t3_parts = [env_block]
    if runtime_block:
        t3_parts.append(runtime_block)
    t3_parts.append(catalog)
    t3_parts.append(TOOL_USE_FORMAT)
    t3 = "\n\n".join(t3_parts)

    # ── T4: Iteration hints ──────────────────────────────────────────
    t4 = _render_t4_iteration(last_tool_hints or [])
    if t4 and len(t4) > budgets["T4_iter"]:
        t4, was_truncated = _fit_to_budget(t4, budgets["T4_iter"])
        if was_truncated:
            pruned.append("T4_iter:truncated")

    # ── Combine + summary fold ───────────────────────────────────────
    sections = [t1]
    if t2:
        sections.append(t2)
    sections.append(t3)
    if t4:
        sections.append(t4)
    system_text = "\n\n".join(s for s in sections if s)

    # Carried-forward conversation summary lives at the end so the model
    # sees it last (recency bias). We merge using the memory engine so
    # the marker shape stays consistent with what clients expect.
    engine = get_default_engine()
    history = list(history or [])
    carried_summary = ""
    if session and session.summary:
        carried_summary = session.summary
    if not carried_summary:
        carried_summary = engine.extract_summary(history)
    if carried_summary:
        system_text = engine.merge_summary(system_text, carried_summary)

    # Drop any summary-only system messages from history — they're folded
    # into our system_text above, no need to double-include.
    cleaned_history = []
    for m in history:
        if m.get("role") == "system":
            continue  # any system message is replaced by ours
        cleaned_history.append(m)

    est_tokens = int(len(system_text) / CHARS_PER_TOKEN)
    return AssembledContext(
        system=system_text,
        history=cleaned_history,
        est_tokens=est_tokens,
        session_id=session_id,
        tier_budgets={
            "T1_rules": len(t1),
            "T2_session": len(t2_full),
            "T3_task": len(t3),
            "T4_iter": len(t4),
        },
        pruned=pruned,
        summary=carried_summary,
    )


# ───────────────────────────────────────────────────────────────────────
# Backward-compat shim
# ───────────────────────────────────────────────────────────────────────


def build_system_context(
    base_prompt: str,
    tools: Iterable,
    cwd: Optional[str] = None,
) -> str:
    """Legacy entry point — preserved for callers that don't have a
    session_id yet (orchestrator pre-rewrite, scheduler, MCP server).

    Returns just the system prompt string, with no T2 session metadata.
    New code should call `assemble_context(...)` instead.
    """
    ctx = assemble_context(
        session_id="legacy-no-session",
        base_prompt=base_prompt,
        tools=tools,
        history=None,
        cwd=cwd,
    )
    return ctx.system


__all__ = [
    "AssembledContext",
    "assemble_context",
    "build_env_block",
    "build_system_context",
    "DEFAULT_TIER_BUDGETS",
    "get_git_info",
    "render_tools_catalog",
    "TOOL_USE_FORMAT",
]
