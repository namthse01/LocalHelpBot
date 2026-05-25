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
import re
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

# v4 Slice 4.3 — auto-capture correction patterns. When the user's
# message starts with one of these AND there's a prior assistant turn,
# persist a lesson candidate. Off by default (config.LESSONS_AUTO_CAPTURE).
_CORRECTION_RE = re.compile(
    r"^\s*(no[,.\s!]|don[''']t|actually[,]?|that[''']s wrong|stop|"
    r"không phải|sai rồi|đừng|hãy nhớ|please remember|please don[''']t)",
    re.IGNORECASE,
)

# v4 Slice 4.3 — preference detection. When the user says "I prefer X" /
# "always do X" / "luôn dùng X" / "from now on", persist as a sticky
# decision tied to this session.
_PREFERENCE_RE = re.compile(
    r"\b(i prefer|i'd prefer|i would prefer|always (use|do|answer|reply)|"
    r"from now on|always remember|luôn (dùng|trả lời|viết)|ghi nhớ là)\b",
    re.IGNORECASE,
)


def _maybe_capture_correction(
    conversation: Optional[List[Dict[str, Any]]],
    user_message: str,
    session_id: str,
) -> None:
    """Slice 4.3 — auto-detect a correction in the user's new turn and
    persist it as a lesson. Cheap heuristic, never blocks the request."""
    try:
        from config import LESSONS_AUTO_CAPTURE   # type: ignore
        if not LESSONS_AUTO_CAPTURE:
            return
    except Exception:
        return
    if not user_message or not _CORRECTION_RE.match(user_message):
        return
    # Find the most recent assistant turn for context.
    trigger = ""
    for msg in reversed(conversation or []):
        if msg.get("role") == "assistant":
            c = msg.get("content", "")
            if isinstance(c, list):
                c = " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
            trigger = str(c or "")[:200]
            break
    try:
        get_lessons_store().record(
            lesson=user_message[:400],
            scope="global",
            trigger=trigger,
            source_session=session_id,
        )
        logger.info(f"[lessons] auto-captured correction (sid={session_id})")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[lessons] auto-capture failed: {e}")


def _maybe_capture_preference(user_message: str, session_id: str) -> None:
    """Slice 4.3 — detect 'I prefer X' / 'always X' style sticky prefs
    and store on the Session. No-op if already set or pattern misses."""
    if not user_message or not _PREFERENCE_RE.search(user_message):
        return
    try:
        store = get_store()
        sess = store.get(session_id)
        if sess is None:
            return
        # Lightweight extraction: take the matched verb + ≤120 chars context.
        snippet = user_message.strip()[:200]
        key = "preference_" + str(len(sess.sticky))
        sess.set_sticky(key, snippet)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[lessons] preference capture failed: {e}")


def _maybe_pull_missing_model(model: str, stream_cb: Optional[Callable]) -> None:
    """Slice 4.5 — when an agent profile names an Ollama model that
    isn't installed AND OLLAMA_AUTO_PULL is on, ask the user for
    permission and run `ollama pull` in a subprocess.

    Best-effort: silent no-op on any failure path; the existing provider
    fallback chain will catch the actual chat error if pull doesn't run.
    """
    try:
        from config import OLLAMA_AUTO_PULL, OLLAMA_BASE   # type: ignore
        if not OLLAMA_AUTO_PULL or not model:
            return
    except Exception:
        return
    try:
        # Check if model is already installed.
        import json
        import urllib.request
        with urllib.request.urlopen(OLLAMA_BASE + "/api/tags", timeout=3) as r:
            tags = json.loads(r.read())
        installed = {(m.get("name") or m.get("model") or "").lower() for m in tags.get("models", [])}
        wanted = model.lower()
        if wanted in installed or any(i.startswith(wanted.split(":")[0]) for i in installed):
            return   # already present
    except Exception:
        return

    try:
        from core.permissions import request_permission
        decision = request_permission(
            "install_package", model,
            {
                "package": model,
                "reason": f"Agent profile needs '{model}' but it's not installed locally.",
                "interpreter": "ollama",
                "command": f"ollama pull {model}",
            },
        )
        if not decision["allowed"]:
            return
        if stream_cb:
            stream_cb({"type": "status", "text": f"pulling Ollama model {model}…"})
        import subprocess
        proc = subprocess.run(["ollama", "pull", model], capture_output=True, text=True, timeout=600)
        if stream_cb:
            stream_cb({
                "type": "status",
                "text": f"ollama pull {model} → exit {proc.returncode}",
            })
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[ollama] auto-pull failed for {model}: {e}")

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

        # ── Assemble the system prompt + cleaned history ───────────
        base_prompt = profile["system_prompt"]
        if (profile.get("verify") or "off") == "high":
            base_prompt = base_prompt + COVE_ADDENDUM
        ctx = assemble_context(
            session_id=session_id,
            base_prompt=base_prompt,
            tools=list(sub_registry),
            history=conversation,
            runtime_block=self._runtime_block(agent_name),
            profile_name=agent_name,
        )

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
        try:
            result = run_agent(messages, sub_registry, stream_cb=stream_cb, session_id=session_id)
        finally:
            if stream_cb:
                stream_cb({"type": "agent_end", "agent": agent_name, "session": session_id, "run_id": run_id})
            self.active_sessions.pop(run_id, None)
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
