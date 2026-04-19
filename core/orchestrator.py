"""
Multi-agent orchestrator — ports claude-code's Task/AgentTool pattern.

One entry point, N specialists. The orchestrator:
  • Builds a ToolRegistry once (shared).
  • For each specialist, derives a filtered sub-registry per profile.
  • Injects <environment>, <tools> catalog and <project_memory> blocks into
    the system prompt (see core/context.build_system_context).
  • Exposes a `task` tool (and legacy `delegate` alias) that spawns a nested
    specialist with an isolated conversation, bubbling progress events up
    to the parent stream (claude-code's AgentToolProgress pattern).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable, Dict, List, Optional

from config import AGENT_PROFILES
from core.context import build_system_context
from core.memory import extract_summary, merge_summary
from core.providers import smart_provider
from core.tool_schema import Tool, ToolRegistry, build_default_registry

logger = logging.getLogger(__name__)


class AgentOrchestrator:
    def __init__(self, legacy_tools: Optional[Dict[str, Callable]] = None):
        # Build the canonical registry (wraps legacy callables with schema).
        self.registry: ToolRegistry = build_default_registry()
        # Preserve legacy dict for any caller that still passes it through proxy.py.
        self.tools_registry = legacy_tools or {}
        self.active_sessions: Dict[str, dict] = {}

    # ──────────────────────────────────────────────────────────
    #  Runtime metadata
    # ──────────────────────────────────────────────────────────
    def _runtime_block(self, agent_name: str) -> str:
        info = smart_provider.describe()
        active = info["last_used_provider"] or info["primary_provider"]
        active_m = info["last_used_model"] or info["primary_model"]
        return (
            f"<runtime agent=\"{agent_name}\" active=\"{active}/{active_m}\" "
            f"fallback=\"{info['fallback_provider']}/{info['fallback_model']}\"/>"
        )

    # ──────────────────────────────────────────────────────────
    #  Specialist loop
    # ──────────────────────────────────────────────────────────
    def run_specialist(
        self,
        agent_name: str,
        prompt: str,
        conversation: Optional[List[Dict[str, str]]] = None,
        stream_cb: Optional[Callable] = None,
        parent_id: Optional[str] = None,
    ) -> str:
        profile = AGENT_PROFILES.get(agent_name)
        if not profile:
            return f"ERROR: Specialist '{agent_name}' not found. Known: {list(AGENT_PROFILES.keys())}"

        # Resolve toolset for this specialist (filter registry + add task/delegate).
        wanted = list(profile.get("tools", []))
        sub_registry = self.registry.filter(wanted)

        has_delegate = "delegate" in wanted or "task" in wanted
        if has_delegate:
            sub_registry.register(self._make_task_tool(stream_cb, parent_id=agent_name))
            # Back-compat alias
            sub_registry.register(self._make_task_tool(stream_cb, parent_id=agent_name, alias="delegate"))

        # Build system prompt: base + runtime + env + tool catalog + memory +
        # tool-use format + any carried-forward conversation summary.
        base = profile["system_prompt"] + "\n\n" + self._runtime_block(agent_name)
        system_content = build_system_context(base, list(sub_registry))
        carried_summary = extract_summary(conversation or [])
        system_content = merge_summary(system_content, carried_summary)

        messages: List[Dict[str, str]] = [{"role": "system", "content": system_content}]

        if conversation:
            # Drop any existing system messages (summary already folded in).
            non_system = [m for m in conversation if m.get("role") != "system"]
            messages.extend(non_system)
            # Avoid duplicating the user prompt: if the last message in the
            # carried history IS the user's latest turn, we don't append it
            # again. Sub-agent (task-tool) calls pass conversation=None, so
            # the else-branch still runs for them.
            if not non_system or non_system[-1].get("role") != "user":
                messages.append({"role": "user", "content": prompt})
        else:
            messages.append({"role": "user", "content": prompt})

        session_id = str(uuid.uuid4())[:8]
        self.active_sessions[session_id] = {"agent": agent_name, "parent": parent_id}

        from core.agent import run_agent  # deferred import to avoid cycles
        if stream_cb:
            stream_cb({"type": "agent_start", "agent": agent_name, "session": session_id, "parent": parent_id})
        try:
            result = run_agent(messages, sub_registry, stream_cb=stream_cb)
        finally:
            if stream_cb:
                stream_cb({"type": "agent_end", "agent": agent_name, "session": session_id})
            self.active_sessions.pop(session_id, None)
        return result

    # ──────────────────────────────────────────────────────────
    #  Task / delegate tool (claude-code AgentTool port)
    # ──────────────────────────────────────────────────────────
    def _make_task_tool(
        self,
        parent_stream_cb: Optional[Callable],
        parent_id: Optional[str] = None,
        alias: str = "task",
    ) -> Tool:
        def handler(args: Dict[str, Any]) -> str:
            target = args.get("agent") or args.get("subagent") or args.get("specialist")
            prompt = args.get("prompt") or args.get("task") or args.get("description")
            if not target or not prompt:
                return "ERROR: task requires {agent, prompt}."
            if target not in AGENT_PROFILES:
                return f"ERROR: unknown specialist '{target}'. Known: {list(AGENT_PROFILES.keys())}"

            # Nested stream: tag sub events so UI can indent/group them.
            def nested_cb(ev):
                if parent_stream_cb and isinstance(ev, dict):
                    ev = dict(ev)
                    ev["nested_from"] = parent_id or "parent"
                    parent_stream_cb(ev)

            logger.info(f"[task] {parent_id or 'root'} → {target}: {prompt[:100]}…")
            result = self.run_specialist(target, prompt, stream_cb=nested_cb, parent_id=parent_id)
            # Return just the final text to the caller — isolating sub-context.
            return f"[sub-agent {target} finished]\n{result}"

        # Build per-specialist routing criteria so the caller can pick the right
        # one. Falls back to just the name if a profile has no description.
        spec_lines = []
        for name, prof in AGENT_PROFILES.items():
            if name == parent_id:  # hide the caller from its own list
                continue
            desc = (prof.get("description") or "").strip()
            spec_lines.append(f"  • {name}: {desc or '(no description)'}")
        spec_block = "\n".join(spec_lines) if spec_lines else "  (no specialists registered)"

        return Tool(
            name=alias,
            description=(
                "Spawn a sub-agent specialist with an isolated conversation.\n"
                "Use when the task needs a fresh context window, a specialist "
                "skillset, or can run in parallel with your own work.\n\n"
                "Available specialists:\n"
                f"{spec_block}\n\n"
                "Pick the ONE that best matches — do not delegate trivial work."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "description": "Specialist name"},
                    "prompt": {"type": "string", "description": "Task description for the sub-agent"},
                },
                "required": ["agent", "prompt"],
            },
            handler=handler,
            category="meta",
        )

    # ──────────────────────────────────────────────────────────
    #  Entry
    # ──────────────────────────────────────────────────────────
    def handle_request(
        self,
        user_prompt: str,
        conversation: Optional[List[Dict[str, str]]] = None,
        stream_cb: Optional[Callable] = None,
    ) -> str:
        return self.run_specialist("main", user_prompt, conversation, stream_cb=stream_cb)
