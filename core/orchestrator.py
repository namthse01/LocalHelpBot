"""Multi-agent orchestrator — claude-code Task/AgentTool pattern, ported.

One entry point, N specialists. The orchestrator:

  • Builds a ToolRegistry once (shared) and derives a per-profile sub-registry
    on each call.
  • Calls `core.context.assemble_context()` so every specialist gets the
    4-tier system prompt (T1 Rules / T2 Session / T3 Task / T4 Iteration).
  • Threads a `session_id` through every specialist *and* every spawned
    sub-agent so a multi-agent task still shares the user's goal, the
    files-touched list, and sticky decisions via core.conversation_store.
  • Exposes a `task` tool (and legacy `delegate` alias) that spawns a
    nested specialist with an isolated conversation but a *shared*
    session — bubbling progress events up to the parent stream
    (claude-code's AgentToolProgress pattern).

Session ID propagation: if the caller doesn't supply one we synthesize
it from `core.conversation_store.derive_session_id(...)` using the
prompt as the seed. The proxy already derives the id from the HTTP body
and passes it in; sub-agents inherit the parent's id.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable, Dict, List, Optional

from config import AGENT_PROFILES
from core.context import assemble_context
from core.conversation_store import derive_session_id, get_store
from core.lessons import get_lessons_store
from core.memory import get_default_engine
from core.providers import smart_provider
from core.tool_schema import Tool, ToolRegistry, build_default_registry

logger = logging.getLogger(__name__)

# ── Split-D re-exports ───────────────────────────────────────
# The per-turn side-effect capture helpers (correction → lesson,
# preference → sticky, missing-model → ollama pull) live in
# core/orchestrator_capture.py. Re-imported here so the bare-name calls in
# run_specialist() resolve and tests/test_lessons.py (which calls
# orchestrator._maybe_capture_correction) keeps working. Behavior-preserving
# — see that module's docstring.
from core.orchestrator_capture import (  # noqa: E402,F401  (re-export)
    _CORRECTION_RE,
    _PREFERENCE_RE,
    _maybe_capture_correction,
    _maybe_capture_preference,
    _maybe_pull_missing_model,
)

# Chain-of-Verification addendum. Appended to the agent system prompt
# when `profile.verify == "high"`. Single-pass CoVe is friendlier to
# local 7B models than a multi-turn coordinator — we tell the agent to
# generate verification questions and answer them inline before its
# final answer. Pattern from:
#   How_AI_Learn/Prompt-Engineering-Techniques-Hub/Chain_of_Verification_Prompting.md
COVE_ADDENDUM = (
    "\n\nCHAIN-OF-VERIFICATION (verify=high):\n"
    "Before producing your FINAL answer (no tool_use), do this inside your last\n"
    "assistant message:\n"
    "  (a) DRAFT — write a brief first-pass answer.\n"
    "  (b) VERIFY — list 3 short verification questions about specific facts in\n"
    "      your draft (paths, numbers, claims). Answer each from the tool\n"
    "      results / source files you've already read this turn. If a question\n"
    "      cannot be answered from sources, mark it [UNVERIFIED] and explicitly\n"
    "      remove or hedge the claim in the next step.\n"
    "  (c) FINAL — produce the corrected answer, integrating the verification\n"
    "      findings. Drop or hedge any [UNVERIFIED] claim.\n"
    "Format the sections clearly so the user can audit:\n"
    "  ## Draft\n"
    "  …draft text…\n"
    "  ## Verification\n"
    "  1. Q: … → A: …\n"
    "  2. Q: … → A: …\n"
    "  3. Q: … → A: …\n"
    "  ## Final\n"
    "  …corrected answer…\n"
    "Only the ## Final block is the user-facing answer; the others are the\n"
    "audit trail.\n"
)


class AgentOrchestrator:
    def __init__(self, legacy_tools: Optional[Dict[str, Callable]] = None):
        # Canonical registry wraps every legacy callable with a schema.
        self.registry: ToolRegistry = build_default_registry()
        self.tools_registry = legacy_tools or {}
        # session_id -> {"agent", "parent"} — used by the UI status panel
        # and to scope the "task" tool's nested progress events.
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
    #  Session-aware specialist loop
    # ──────────────────────────────────────────────────────────
    def run_specialist(
        self,
        agent_name: str,
        prompt: str,
        conversation: Optional[List[Dict[str, str]]] = None,
        stream_cb: Optional[Callable] = None,
        parent_id: Optional[str] = None,
        *,
        session_id: Optional[str] = None,
    ) -> str:
        profile = AGENT_PROFILES.get(agent_name)
        if not profile:
            return f"ERROR: Specialist '{agent_name}' not found. Known: {list(AGENT_PROFILES.keys())}"

        # Resolve toolset for this specialist (filter registry + task tool).
        wanted = list(profile.get("tools", []))
        sub_registry = self.registry.filter(wanted)

        has_delegate = "delegate" in wanted or "task" in wanted
        if has_delegate:
            sub_registry.register(self._make_task_tool(stream_cb, parent_id=agent_name, parent_session=session_id))
            # Back-compat alias.
            sub_registry.register(self._make_task_tool(stream_cb, parent_id=agent_name, parent_session=session_id, alias="delegate"))

        # ── Session lookup / derivation ────────────────────────────
        if session_id is None:
            seed = (conversation or []) + [{"role": "user", "content": prompt}]
            session_id = derive_session_id(seed)
        store = get_store()
        sess = store.get_or_create(session_id, source="ui")
        if not sess.goal:
            sess.note_user_goal(prompt)
        store.note(session_id, profile=agent_name)

        # v4 Slice 4.3 — auto-capture corrections and preferences from
        # the new user message. Both are gated by feature flags in
        # config.py and never block the request on failure.
        _maybe_capture_correction(conversation, prompt, session_id)
        _maybe_capture_preference(prompt, session_id)

        # v4 Slice 4.5 — auto-pull the profile's model if it's missing
        # and OLLAMA_AUTO_PULL is on (gated by user permission).
        _maybe_pull_missing_model(profile.get("model", ""), stream_cb)

        # ── Server-side conversation history (robust cross-turn memory) ──
        # The Ollama protocol is stateless and our Web UI sends ONLY the
        # latest user message, so the model would otherwise see no prior
        # turns — follow-ups like "divide the two results above" break
        # because the single-value <recent_exchange> hint can't carry both
        # earlier answers. For a TOP-LEVEL run we replay this session's
        # stored turns as the model's history. Sub-agents keep their
        # isolated conversation (only T2 metadata is shared — see
        # _make_task_tool), so we never replay into them.
        top_level = parent_id is None
        effective_history = list(conversation or [])
        if top_level and sess.history:
            client_sent_history = any(
                isinstance(m, dict) and m.get("role") == "assistant"
                for m in effective_history
            )
            if not client_sent_history:
                # Thin client: prepend stored turns. The incoming latest
                # user message stays last so it is treated as the prompt.
                effective_history = list(sess.history) + effective_history

        # ── Assemble the system prompt + cleaned history ───────────
        base_prompt = profile["system_prompt"]
        if (profile.get("verify") or "off") == "high":
            base_prompt = base_prompt + COVE_ADDENDUM
        ctx = assemble_context(
            session_id=session_id,
            base_prompt=base_prompt,
            tools=list(sub_registry),
            history=effective_history,
            runtime_block=self._runtime_block(agent_name),
            profile_name=agent_name,
            last_user_message=prompt,
        )

        # v5: tool policy — if the user's turn forbids tools ("guide-only"),
        # the loop hard-blocks tool calls instead of trusting prompt
        # compliance. Best-effort; never blocks the request on import error.
        tool_policy = None
        try:
            from core.security import build_effective_tool_policy
            tool_policy = build_effective_tool_policy(last_user_message=prompt)
        except Exception:
            tool_policy = None

        # Build the final message list:
        #   1. system (from ctx)
        #   2. cleaned history (any prior user/assistant turns)
        #   3. (only if needed) the new user prompt
        messages: List[Dict[str, Any]] = [{"role": "system", "content": ctx.system}]
        history = list(ctx.history)
        messages.extend(history)
        if not history or history[-1].get("role") != "user":
            messages.append({"role": "user", "content": prompt})

        # Compact if we're somehow still over budget (e.g. very long
        # history that even the context engine couldn't shrink). The
        # MemoryEngine returns a CompactionResult — we only adopt it
        # when it actually fired so a bad summarizer call doesn't drop
        # state on the floor.
        # v5: derive the compaction threshold from the model's real context
        # window (long-context models shouldn't be capped at the 6000 default).
        # Falls back to the default engine if discovery fails. See
        # core/context_budget.py.
        try:
            from core.context_budget import effective_max_tokens
            from core.memory import MemoryEngine
            _max_tok = effective_max_tokens(profile.get("model", ""))
            engine = MemoryEngine(max_tokens=_max_tok)
        except Exception:
            engine = get_default_engine()
        compaction = engine.compact(messages, summarizer=smart_provider, prior_summary=ctx.summary)
        if compaction.fired:
            messages = compaction.messages
            store.note(session_id, summary=compaction.summary)

        # Track the active sub-session for UI status / "task" tool.
        run_id = str(uuid.uuid4())[:8]
        self.active_sessions[run_id] = {
            "agent": agent_name,
            "parent": parent_id,
            "session": session_id,
        }

        from core.agent import run_agent  # deferred import to avoid cycles
        if stream_cb:
            stream_cb({
                "type": "agent_start",
                "agent": agent_name,
                "session": session_id,
                "run_id": run_id,
                "parent": parent_id,
                "tier_budgets": ctx.tier_budgets,
                "pruned": ctx.pruned,
            })

        # v5: count tool calls + turns so we can decide whether the run was
        # complex enough to auto-extract a reusable skill from. We wrap the
        # caller's stream_cb transparently.
        run_stats = {"tool_calls": 0, "turns": 0}

        def _counting_cb(ev):
            if isinstance(ev, dict):
                t = ev.get("type")
                if t == "tool_call":
                    run_stats["tool_calls"] += 1
                elif t == "final":
                    run_stats["turns"] = int(ev.get("turns") or run_stats["turns"])
            if stream_cb:
                stream_cb(ev)

        try:
            result = run_agent(
                messages, sub_registry, stream_cb=_counting_cb,
                session_id=session_id, tool_policy=tool_policy,
            )
        finally:
            if stream_cb:
                stream_cb({"type": "agent_end", "agent": agent_name, "session": session_id, "run_id": run_id})
            self.active_sessions.pop(run_id, None)

        # Persist this turn so the NEXT (thin-client) request replays it as
        # real conversation history. Top-level only — sub-agent turns stay
        # isolated from the user-facing thread. Best-effort: a store hiccup
        # must never change the answer we just produced.
        if top_level:
            try:
                sess.record_message("user", prompt)
                sess.record_message("assistant", result)
            except Exception as e:  # noqa: BLE001
                logger.debug("[history] record turn failed: %s", e)

        # v5: skill auto-extraction — only at the top level (not sub-agents),
        # gated by config.SKILLS_AUTO_EXTRACT, and only when the run was
        # non-trivial. Best-effort; never affects the returned answer.
        if parent_id is None:
            try:
                from core.skills import maybe_extract_skill
                final_convo = messages + [{"role": "assistant", "content": result}]
                sk = maybe_extract_skill(
                    final_convo,
                    rounds=run_stats["turns"],
                    tool_count=run_stats["tool_calls"],
                    summarizer=smart_provider,
                )
                if sk and stream_cb:
                    stream_cb({"type": "status", "text": f"learned skill: {sk.name}"})
            except Exception as e:  # noqa: BLE001
                logger.debug("[skills] auto-extract failed: %s", e)

        return result

    # ──────────────────────────────────────────────────────────
    #  Task / delegate tool (claude-code AgentTool port)
    # ──────────────────────────────────────────────────────────
    def _make_task_tool(
        self,
        parent_stream_cb: Optional[Callable],
        parent_id: Optional[str] = None,
        parent_session: Optional[str] = None,
        alias: str = "task",
    ) -> Tool:
        def handler(args: Dict[str, Any]) -> str:
            target = args.get("agent") or args.get("subagent") or args.get("specialist")
            prompt = args.get("prompt") or args.get("task") or args.get("description")
            if not target or not prompt:
                return "ERROR: task requires {agent, prompt}."
            if target not in AGENT_PROFILES:
                return f"ERROR: unknown specialist '{target}'. Known: {list(AGENT_PROFILES.keys())}"

            # Nested progress events get tagged so the UI can group them
            # under the spawning agent visually.
            def nested_cb(ev):
                if parent_stream_cb and isinstance(ev, dict):
                    ev = dict(ev)
                    ev["nested_from"] = parent_id or "parent"
                    parent_stream_cb(ev)

            logger.info("[task] %s → %s: %s…", parent_id or "root", target, prompt[:100])
            # Inherit the parent's session_id so the sub-agent reads the
            # same goal/files_touched/sticky from the conversation store.
            # Its conversation messages remain isolated — only T2 metadata
            # is shared.
            result = self.run_specialist(
                target,
                prompt,
                stream_cb=nested_cb,
                parent_id=parent_id,
                session_id=parent_session,
            )
            return f"[sub-agent {target} finished]\n{result}"

        # Per-specialist routing guidance so the caller can pick the
        # right one. Falls back to just the name when a profile has no
        # description.
        spec_lines = []
        for name, prof in AGENT_PROFILES.items():
            if name == parent_id:
                continue  # don't list the caller as a delegate target
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
        *,
        session_id: Optional[str] = None,
    ) -> str:
        return self.run_specialist(
            "main",
            user_prompt,
            conversation,
            stream_cb=stream_cb,
            session_id=session_id,
        )
