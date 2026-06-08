"""Skill tools — the agent's interface to the self-evolving SkillStore.

  save_skill   — persist a reusable procedure (when-to-use + steps + pitfalls).
  list_skills  — list saved skills, or search them by query.
  delete_skill — remove a skill by name/slug.

Skills are auto-injected into the system prompt (T1) when they match the
user's request — see core/skills.py:render_for_prompt and core/context.py.
The agent rarely needs to call list_skills (retrieval is automatic); the
high-value tool here is save_skill, used when the agent discovers a
procedure worth keeping. See [[core/lessons.py]] for the lighter
one-line-correction sibling.
"""
from __future__ import annotations

from typing import Any, Dict, List

from core.skills import get_skills_store
from core.tool_schema import ErrorCode, Tool, ToolRegistry, ToolResult


def _as_str_list(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    # tolerate a newline/semicolon string
    return [s.strip() for s in str(v).replace(";", "\n").splitlines() if s.strip()]


def _save_skill(args: Dict[str, Any]) -> ToolResult:
    title = (args.get("title") or args.get("name") or "").strip()
    if not title:
        return ToolResult.error(ErrorCode.INVALID_ARGS, "save_skill requires 'title'.", retryable=False)
    steps = _as_str_list(args.get("steps") or args.get("procedure"))
    if not steps:
        return ToolResult.error(
            ErrorCode.INVALID_ARGS,
            "save_skill requires 'steps' (a non-empty list of procedure steps).",
            retryable=False,
        )
    store = get_skills_store()
    if store.has_title(title):
        return ToolResult.success(f"OK: a skill named '{title}' already exists — not duplicated.")
    sk = store.add(
        title=title,
        when_to_use=(args.get("when_to_use") or "").strip(),
        procedure=steps,
        pitfalls=_as_str_list(args.get("pitfalls")),
        verification=_as_str_list(args.get("verification")),
        tags=_as_str_list(args.get("tags")),
        category=(args.get("category") or "general").strip() or "general",
        source="taught",
        confidence=float(args.get("confidence") or 0.85),
        description=(args.get("description") or "").strip(),
    )
    if sk is None:
        return ToolResult.error(ErrorCode.UNKNOWN, "save_skill: could not persist the skill.", retryable=False)
    return ToolResult.success(
        f"OK: skill '{sk.name}' saved with {len(sk.procedure)} steps. "
        f"It will be auto-recalled when a future task matches.",
        skill=sk.name,
    )


def _list_skills(args: Dict[str, Any]) -> ToolResult:
    store = get_skills_store()
    query = (args.get("query") or "").strip()
    if query:
        skills = store.search(query, limit=int(args.get("limit") or 8))
    else:
        skills = sorted(store.load_all(), key=lambda s: s.uses, reverse=True)[: int(args.get("limit") or 20)]
    if not skills:
        return ToolResult.success("NO_DATA: no saved skills" + (f" match '{query}'." if query else "."))
    lines = [f"{len(skills)} skill(s):"]
    for s in skills:
        lines.append(f"  • {s.name} [{s.category}] uses={s.uses} conf={s.confidence:.2f} — {s.description or s.when_to_use}")
    return ToolResult.success("\n".join(lines), count=len(skills))


def _delete_skill(args: Dict[str, Any]) -> ToolResult:
    name = (args.get("name") or args.get("skill") or "").strip()
    if not name:
        return ToolResult.error(ErrorCode.INVALID_ARGS, "delete_skill requires 'name'.", retryable=False)
    ok = get_skills_store().delete(name)
    if not ok:
        return ToolResult.error(ErrorCode.FILE_NOT_FOUND, f"No skill named '{name}'.", retryable=False)
    return ToolResult.success(f"OK: deleted skill '{name}'.")


def register(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="save_skill",
        description=(
            "Persist a REUSABLE PROCEDURE you discovered so you can follow it "
            "next time a similar task comes up. Use after solving a multi-step "
            "problem that will recur (a sequence of commands / edits / tool "
            "calls). Provide 'title' + 'steps' (and ideally 'when_to_use', "
            "'pitfalls', 'tags'). NOT for one-off facts — use save_lesson for "
            "short corrections/preferences."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short skill name (<10 words)."},
                "when_to_use": {"type": "string", "description": "One sentence trigger condition."},
                "steps": {"type": "array", "items": {"type": "string"}, "description": "3-7 imperative steps."},
                "pitfalls": {"type": "array", "items": {"type": "string"}, "description": "Common failure modes."},
                "verification": {"type": "array", "items": {"type": "string"}, "description": "How to confirm success."},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "3-5 keywords for retrieval."},
                "category": {"type": "string"},
                "confidence": {"type": "number", "description": "0-1 reliability/reusability."},
            },
            "required": ["title", "steps"],
        },
        handler=_save_skill,
        category="meta",
    ))
    registry.register(Tool(
        name="list_skills",
        description=(
            "List or search saved skills. Optional 'query' keyword-searches "
            "them; otherwise returns the most-used skills. Retrieval is usually "
            "automatic (matching skills are injected into your prompt), so call "
            "this only to audit or pick a skill by name."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
        handler=_list_skills,
        category="meta",
    ))
    registry.register(Tool(
        name="delete_skill",
        description="Delete a saved skill by name/slug. Use when a skill is wrong or obsolete.",
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        handler=_delete_skill,
        category="meta",
    ))
