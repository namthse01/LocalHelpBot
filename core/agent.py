"""Agentic loop — ToolResult-aware, Stop-the-Line guarded.

  • One "turn" = model call → parse tool_use → execute (parallel) →
    append `<tool_result>` block → next model call.
  • Terminates when the model emits NO tool_use (end_turn).
  • Errors return as typed `ToolResult` so the model gets structured
    feedback (`is_error="true" code="STALE_OLD_STRING" hint="…"`).
  • **Stop-the-Line**: per (tool_name, args_hash) consecutive-error
    counter. Two failures of the same call are tolerated; the third
    is short-circuited with a `LOOP_BUDGET` synthetic result and the
    model is forced to revise the plan.

Tool-use formats parsed (in priority order):
  1. <tool_use>{"name": "...", "input": {...}}</tool_use>  ← preferred
  2. ```tool_use {"name": "...", "input": {...}}```        ← fenced
  3. bare {"name": "...", "input": {...}}                  ← tolerant
  4. ACTION: {"tool": "...", ...}                          ← legacy

Tool handlers may return either `ToolResult` (preferred — that's what
the new `core.tools` returns) or `str` (legacy). String returns get
adapted via `ToolResult.from_legacy()` so a half-migrated codebase
still works.

Parsing, micro-compaction, the arithmetic fast-path, and tool-result
rendering live in sibling modules (`core.agent_parsing`,
`core.agent_compaction`, `core.agent_fastpath`, `core.agent_results`) and
are re-exported below so existing `from core.agent import …` paths keep
working unchanged.
"""

from __future__ import annotations

import logging
import re
import sys
import time as _time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.providers import SAMPLING_CREATIVE, smart_provider
from core.tool_schema import ErrorCode, Tool, ToolRegistry, ToolResult

# ── Re-exported helpers (split into sibling modules; kept importable from
#    core.agent so existing import paths & monkeypatch targets still work). ──
from core.agent_parsing import (
    ToolsArg,
    _ACTION_RE,
    _BARE_JSON_RE,
    _FENCE_RE,
    _TAG_OPEN_RE,
    _TAG_RE,
    _TOOL_ATTEMPT_MARKERS,
    _detect_malformed_tool_attempt,
    _parse_error_nudge,
    _parse_tool_uses,
    _resolve_tool,
    _scan_json_after,
    _strip_tool_uses,
    _tool_names,
    _write_nudge_text,
)
from core.agent_compaction import (
    MICRO_COMPACT_DIGEST_CHARS,
    MICRO_COMPACT_KEEP_TAIL,
    MICRO_COMPACT_MIN_CHARS,
    MICRO_COMPACT_PLACEHOLDER,
    MICRO_COMPACT_TURNS,
    _extract_digest,
    _micro_compact,
)
from core.agent_fastpath import (
    _FASTPATH_OPS,
    _FASTPATH_RE,
    _NUMBER_RE,
    _extract_last_number,
    _fmt_num,
    _last_user_message,
    _parse_number_token,
    _try_arithmetic_fastpath,
)
from core.agent_results import (
    _ARG_PRIORITY,
    _args_hash,
    _brief_args,
    _format_results,
    _render_result_block,
)

logger = logging.getLogger(__name__)

MAX_TURNS = 8  # Lowered from 20: chat-style requests should converge in 1-4 turns.
               # Research-heavy profiles can override via run_agent(max_turns=N).
MAX_ERROR_STREAK = 5
MAX_PARSE_RETRIES = 3
MAX_WRITE_NUDGES = 3
# Stop-the-Line threshold. After this many consecutive failures of the
# SAME (tool_name, args_hash), the loop refuses to invoke the handler
# and emits a synthetic LOOP_BUDGET result instead. Set to 2 so the
# third attempt is the one that gets blocked.
STOP_THE_LINE_MAX_RETRIES = 2

# Tool-family budget. Stop-the-Line catches identical (tool, args) calls,
# but a model that runs `fetch_url(A)` → `fetch_url(B)` → `fetch_url(C)` →
# … evades it by varying args. The family budget caps consecutive calls
# of the SAME CATEGORY regardless of args, so the model can't endlessly
# explore one direction (e.g. web search) without committing.
TOOL_FAMILIES: Dict[str, str] = {
    # Web / network
    "search_web": "web", "fetch_url": "web", "download_file": "web",
    "extract_text": "web", "github_search_repos": "web",
    "github_read_file": "web", "github_releases": "web",
    "wikipedia_summary": "web", "youtube_transcript": "web",
    "pypi_search": "web", "pypi_info": "web",
    # Filesystem reads
    "read_file": "fs_read", "read_file_chunk": "fs_read",
    "read_pdf": "fs_read", "read_docx": "fs_read",
    "list_dir": "fs_read", "glob_files": "fs_read",
    "grep_file": "fs_read", "find_in_files": "fs_read",
    # Filesystem writes
    "write_file": "fs_write", "write_pdf": "fs_write",
    "write_docx": "fs_write", "edit_file": "fs_write",
    "delete_file": "fs_write", "make_dir": "fs_write",
    "move_file": "fs_write",
    # Image
    "generate_image": "image", "describe_image": "image",
    "screenshot_and_describe": "image",
    # System / exec
    "run_command": "exec", "python_exec": "exec",
    "install_package": "exec", "list_processes": "exec",
    "kill_process": "exec", "system_info": "exec",
    "screenshot": "exec", "open_with_default_app": "exec",
    "list_windows": "exec", "watch_file": "exec",
    "read_env": "exec", "update_self": "exec",
    # RAG / learning
    "query_rag": "rag", "learn_from_file": "rag",
    "learn_from_url": "rag", "save_lesson": "rag",
    "save_skill": "rag", "list_skills": "rag", "delete_skill": "rag",
    # Clipboard
    "clipboard_read": "clipboard", "clipboard_write": "clipboard",
    # Notes / checklists
    "add_note": "notes", "list_notes": "notes", "update_note": "notes",
    "complete_note_item": "notes", "delete_note": "notes",
    # Hardware / model fit (Cookbook / hw-fit)
    "hardware_info": "hardware", "model_fit": "hardware",
    "recommend_models": "hardware",
    # Documents (living docs + version history)
    "create_document": "documents", "list_documents": "documents",
    "get_document": "documents", "update_document": "documents",
    "delete_document": "documents",
    # Meta
    "task": "delegate", "deep_research": "delegate",
}
TOOL_FAMILY_BUDGET = 4  # Max calls of any one family per task.

# CONVERGE injection — after this many turns without a final answer,
# inject a user-role nudge that forbids more tool calls and forces the
# model to produce an answer (or a clarifying question) on the next turn.
CONVERGE_INJECT_AFTER_TURN = 4


# ───────────────────────────────────────────────────────────────────────
# Creative-mode detection
# ───────────────────────────────────────────────────────────────────────
#
# When the user's latest message looks like a request for creative prose
# (story, scene, RP, etc.), we bump the Ollama sampling to SAMPLING_CREATIVE
# (warmer + longer + with repeat-penalty). Everything else stays on
# SAMPLING_DEFAULT, which is still deterministic enough for tool-use JSON.

_CREATIVE_PATTERNS = [
    # English creative requests
    r"\b(write|compose|tell|narrate|continue|expand|describe)\b.{0,30}\b"
    r"(story|tale|scene|narrative|chapter|poem|article|essay|fanfic|"
    r"fiction|character|dialogue|monologue|smut|erotica|lemon)\b",
    r"\b(roleplay|role[- ]play|RP)\b",
    r"\b(NSFW|adult|explicit)\b|18\+",
    # Vietnamese creative requests
    r"\b(viết|kể|sáng tác|miêu tả|tả|tiếp|nối)\b.{0,30}\b"
    r"(truyện|câu chuyện|chuyện|tiểu thuyết|cảnh|nhân vật|đoạn|"
    r"kịch bản|thơ|bài viết|văn)\b",
    r"\b(người lớn|nóng bỏng|nsfw)\b",
]
_CREATIVE_RE = re.compile("|".join(_CREATIVE_PATTERNS), re.IGNORECASE)


def _looks_creative(user_msg: str) -> bool:
    """Heuristic: does this user message ask for creative prose?

    Used by run_agent to switch the LLM sampling preset for the next turn.
    Intentionally lenient — false positives just produce slightly more
    diverse prose; false negatives produce the dry tool-use-friendly
    output the user complained about.
    """
    if not user_msg:
        return False
    return bool(_CREATIVE_RE.search(user_msg))


# ───────────────────────────────────────────────────────────────────────
# Stream helper
# ───────────────────────────────────────────────────────────────────────


def _emit(cb: Optional[Callable], event):
    if not cb:
        return
    try:
        if isinstance(event, str):
            cb({"type": "text", "text": event})
        else:
            cb(event)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[agent] stream_cb failed: {e}")


# ───────────────────────────────────────────────────────────────────────
# Main loop
# ───────────────────────────────────────────────────────────────────────


def run_agent(
    messages: List[Dict[str, str]],
    tools: ToolsArg,
    stream_cb: Optional[Callable] = None,
    max_turns: int = MAX_TURNS,
    *,
    session_id: Optional[str] = None,
    tool_policy=None,
) -> str:
    """Run the agent loop.

    `session_id` (optional) is used to propagate `ToolResult.meta["files_touched"]`
    into the conversation store so the context engine's T2 tier can render
    "files touched this session" in subsequent turns. None means the call
    came from a non-session context (legacy run_agent direct invocation);
    file tracking just no-ops in that case.

    `tool_policy` (optional `core.security.ToolPolicy`) hard-blocks tool calls
    the user forbade this turn (e.g. "don't use any tools"). When a blocked
    tool is requested the loop returns a typed PERMISSION_DENIED result instead
    of invoking the handler — enforcement, not prompt compliance.
    """
    conversation = list(messages)
    error_streak = 0
    tool_uid = 0
    t_start = _time.perf_counter()

    # Per-call retry counter for Stop-the-Line. Map of args_hash → count.
    # Reset to 0 on any success of that same call.
    retry_counts: Dict[str, int] = {}

    # Per-family call counter — caps consecutive exploration in one tool
    # category (e.g., 4 web fetches max). Unlike Stop-the-Line this is
    # not reset on success; once a family hits its budget, the loop
    # refuses further calls for the rest of the task so the model is
    # forced to switch strategy or commit to an answer.
    family_counts: Dict[str, int] = {}

    # CONVERGE injection one-shot flag — flipped True after the loop
    # injects the "answer now" nudge so it doesn't fire every turn.
    converge_injected = False

    _write_tools = {"write_file", "write_pdf", "write_docx", "edit_file"}
    _first_user = next((m.get("content", "") for m in messages if m.get("role") == "user"), "")
    # Heuristic: does the FIRST user message explicitly ask for a file?
    # Conservative — we only nudge the model to write a file when the user
    # named an EXTENSION (`.pdf`/`.md`/...), or used a save verb tied to a
    # file/target (`save to X`, `into report.pdf`, `ghi vào notes.txt`).
    # The bare verb "write" is NOT enough — "write me a poem" is chat-only,
    # not a file request. False positives here cause the agent to bundle
    # surprise files the user didn't ask for.
    _expects_write = bool(
        re.search(
            # Path 1: any of these write/save verbs, followed within 60 chars
            # by a real file extension OR an explicit "into <file>" phrase.
            r"\b(write|save|create|export|output|summariz\w+|report|generate|ghi|lưu|tạo|xuất)\b"
            r".{0,60}(\.(?:pdf|docx|doc|md|txt|csv|json|html|xml|ya?ml)\b"
            r"|\binto\s+(?:a\s+)?(?:file\b|\.?(?:pdf|docx|md|txt|csv|json|html|xml|ya?ml)\b))",
            _first_user, re.IGNORECASE | re.DOTALL,
        )
        # Path 2: explicit "save/lưu/ghi <to|as|vào> <something>" pattern.
        # NOTE: do NOT add a "bare extension anywhere in the message" path —
        # the user may be referring to a file they want to READ ("read
        # guide.pdf for me"), not write. Stick to verb-anchored patterns.
        or bool(re.search(
            r"\b(save|export|output|lưu|ghi|xuất)\s+(to|as|into|vào|vô)\b",
            _first_user, re.IGNORECASE,
        ))
        # Path 3: "into <filename.ext>" anywhere — strong signal of file
        # destination even with verbs we don't list (draft, compose, ...).
        or bool(re.search(
            r"\binto\s+\w+\.(?:pdf|docx|doc|md|txt|csv|json|html|xml|ya?ml)\b",
            _first_user, re.IGNORECASE,
        ))
    )
    _write_done = False
    _write_nudge_count = 0
    _parse_retry_count = 0

    # ── Arithmetic fast-path ─────────────────────────────────────────
    # If the user's latest message is a pure operator-leading expression
    # (e.g. "+ 4") and the session has a numeric anchor from the previous
    # answer, compute it deterministically and return — no LLM call.
    if session_id:
        try:
            from core.conversation_store import get_store as _get_store
            _sess = _get_store().get(session_id)
        except Exception:  # noqa: BLE001
            _sess = None
        if _sess is not None and _sess.last_numeric_value is not None:
            _last_user = _last_user_message(messages)
            _fp = _try_arithmetic_fastpath(_last_user, _sess.last_numeric_value)
            if _fp is not None:
                logger.info(
                    "[agent] math fast-path: %s (anchor=%s)",
                    _fp, _sess.last_numeric_value,
                )
                _emit(stream_cb, {"type": "status", "text": "arithmetic fast-path"})
                _emit(stream_cb, {
                    "type": "final",
                    "text": _fp,
                    "total_ms": int((_time.perf_counter() - t_start) * 1000),
                    "turns": 0,
                })
                try:
                    _get_store().note(
                        session_id,
                        last_short_answer=_fp,
                        last_numeric_value=_extract_last_number(_fp),
                    )
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"fast-path note_answer failed: {e}")
                return _fp

    for turn in range(max_turns):
        if turn >= MICRO_COMPACT_TURNS:
            n = _micro_compact(conversation)
            if n:
                logger.info(f"[agent] micro-compacted {n} old tool-result turns")
                _emit(stream_cb, {"type": "status", "text": f"micro-compacted {n} old tool outputs"})

        # ── CONVERGE injection ──────────────────────────────────────────
        # After CONVERGE_INJECT_AFTER_TURN turns of unresolved tool-use, add a
        # user-role nudge forbidding more tool calls and forcing the model
        # to produce an answer (or a clarifying question) on the next turn.
        # Fires exactly once per task.
        if (
            turn >= CONVERGE_INJECT_AFTER_TURN
            and not converge_injected
        ):
            nudge = (
                "CONVERGE: You have used multiple tool calls. STOP calling tools. "
                "On your next message, emit your FINAL ANSWER using the information "
                "you already have — even if incomplete. If you genuinely cannot "
                "answer, ask ONE short clarifying question instead. Do NOT emit "
                "any <tool_use> tag in your next reply."
            )
            conversation.append({"role": "user", "content": nudge})
            converge_injected = True
            logger.info("[agent] CONVERGE nudge injected after turn %d", turn)
            _emit(stream_cb, {
                "type": "status",
                "text": f"converge nudge — forcing answer after turn {turn}",
            })

        last_user_for_options = _last_user_message(conversation)
        options = SAMPLING_CREATIVE if _looks_creative(last_user_for_options) else None
        _emit(stream_cb, {
            "type": "status",
            "text": f"Turn {turn + 1}: thinking…" + (" [creative]" if options else ""),
            "creative_mode": options is not None,
        })
        t_turn = _time.perf_counter()
        response = smart_provider.chat(conversation, options=options)
        turn_ms = int((_time.perf_counter() - t_turn) * 1000)
        logger.info(f"[agent] turn {turn+1} — {response.provider}/{response.model} {response.timing_str()}")
        content = response.content or ""

        calls = _parse_tool_uses(content)

        # Dedupe retry-storms: when a weak model panics and emits the same
        # call 3-5 times in one assistant message.
        if len(calls) > 1:
            seen = set()
            deduped = []
            for c in calls:
                inp = c.get("input", {}) or {}
                key_val = inp.get("path") or inp.get("command") or inp.get("url") or ""
                key = (c.get("name", ""), str(key_val)[:200])
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(c)
            if len(deduped) < len(calls):
                logger.info(f"[agent] deduped tool calls: {len(calls)} → {len(deduped)}")
            calls = deduped

        narration = _strip_tool_uses(content) if calls else content
        if narration:
            _emit(stream_cb, {"type": "thought", "text": narration, "turn_ms": turn_ms})

        # No tool calls — classify before ending.
        if not calls:
            diag = _detect_malformed_tool_attempt(content)
            if diag and _parse_retry_count < MAX_PARSE_RETRIES:
                _parse_retry_count += 1
                nudge = _parse_error_nudge(diag, _parse_retry_count - 1)
                _emit(stream_cb, {
                    "type": "status",
                    "text": f"malformed tool_use — self-healing ({_parse_retry_count}/{MAX_PARSE_RETRIES})",
                })
                logger.warning(f"[agent] parse retry {_parse_retry_count}: {diag[:200]}")
                conversation.append({"role": "assistant", "content": content})
                conversation.append({"role": "user", "content": nudge})
                continue

            if _expects_write and not _write_done and _write_nudge_count < MAX_WRITE_NUDGES:
                _write_nudge_count += 1
                nudge = _write_nudge_text(_write_nudge_count - 1)
                _emit(stream_cb, {
                    "type": "status",
                    "text": f"nudging agent to write output ({_write_nudge_count}/{MAX_WRITE_NUDGES})…",
                })
                conversation.append({"role": "assistant", "content": content})
                conversation.append({"role": "user", "content": nudge})
                continue

            total_ms = int((_time.perf_counter() - t_start) * 1000)
            logger.info(f"[agent] DONE in {turn+1} turn(s), {total_ms}ms total")
            _emit(stream_cb, {"type": "final", "text": content, "total_ms": total_ms, "turns": turn + 1})
            if session_id:
                try:
                    from core.conversation_store import get_store as _get_store
                    _stripped = (content or "").strip()
                    _short = _stripped[-200:] if _stripped else ""
                    _num = _extract_last_number(_stripped)
                    _get_store().note(
                        session_id,
                        last_short_answer=_short,
                        last_numeric_value=_num,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"final-answer capture failed: {e}")
            return content

        _parse_retry_count = 0

        # Execute all tool calls from this turn (in declared order).
        results: List[Dict[str, Any]] = []
        all_errors = True
        for call in calls:
            tool_uid += 1
            name = call["name"]
            args = call["input"] or {}

            _emit(stream_cb, {"type": "tool_call", "id": tool_uid, "tool": name, "args": args})

            # Parse error from earlier? Render directly.
            if "__parse_error__" in args:
                result = ToolResult.error(
                    ErrorCode.INVALID_ARGS,
                    f"Could not parse tool_use JSON: {args['__parse_error__']}",
                    hint="Re-emit the tool_use with valid JSON.",
                    retryable=True,
                )
            elif tool_policy is not None and tool_policy.blocks(name):
                # Guide-only / denylisted turn — refuse without invoking.
                result = ToolResult.error(
                    ErrorCode.PERMISSION_DENIED,
                    f"Tool '{name}' is blocked this turn: {tool_policy.reason_for(name)}",
                    hint="The user forbade tool use this turn. Answer in plain text instead.",
                    retryable=False,
                )
                logger.info("[agent] tool_policy blocked '%s' (mode=%s)", name, getattr(tool_policy, "mode", "?"))
            else:
                handler = _resolve_tool(tools, name)
                if handler is None:
                    result = ToolResult.error(
                        ErrorCode.INVALID_ARGS,
                        f"Unknown tool '{name}'. Available: {', '.join(_tool_names(tools))}",
                        retryable=False,
                    )
                else:
                    # ── Family-budget gate ─────────────────────────────
                    # Caps consecutive exploration within one tool category
                    # (e.g. 4 web fetches max). Independent of args, so a
                    # model that varies URLs each turn still hits the wall.
                    family = TOOL_FAMILIES.get(name)
                    if family and family_counts.get(family, 0) >= TOOL_FAMILY_BUDGET:
                        prior = family_counts[family]
                        result = ToolResult.error(
                            ErrorCode.LOOP_BUDGET,
                            (
                                f"Family budget exhausted: '{family}' tools have been "
                                f"called {prior} times in this task. Refusing further "
                                f"'{family}' calls — switch to a different strategy or "
                                f"commit to a final answer with what you already have."
                            ),
                            hint="Stop exploring in this direction; either pick a different family of tool or write your final answer now.",
                            retryable=False,
                        )
                        logger.warning(
                            "[agent] FAMILY-BUDGET hit: family=%s tool=%s count=%d",
                            family, name, prior,
                        )
                        # Track for telemetry but DO NOT actually call the handler.
                        family_counts[family] = prior + 1
                        results.append({"name": name, "args": args, "result": result})
                        _emit(stream_cb, {
                            "type": "tool_result",
                            "id": tool_uid,
                            "tool": name,
                            "ok": False,
                            "code": ErrorCode.LOOP_BUDGET.value,
                            "preview": result.preview(400),
                        })
                        continue
                    # ── Stop-the-Line gate ─────────────────────────────
                    call_hash = _args_hash(name, args)
                    if retry_counts.get(call_hash, 0) >= STOP_THE_LINE_MAX_RETRIES:
                        prior = retry_counts[call_hash]
                        result = ToolResult.error(
                            ErrorCode.LOOP_BUDGET,
                            (
                                f"Stop-the-Line: tool '{name}' with these EXACT args has "
                                f"failed {prior} times in a row. Refusing to invoke a "
                                f"{prior + 1}th attempt — revise the plan or pick a "
                                f"different tool / different args."
                            ),
                            hint="Re-read source files, change at least one argument, or switch tools.",
                            retryable=False,
                        )
                        logger.warning(f"[agent] STOP-THE-LINE for {name} hash={call_hash} after {prior} retries")
                    else:
                        try:
                            raw = handler(args)
                        except Exception as e:  # noqa: BLE001
                            raw = ToolResult.error(ErrorCode.UNKNOWN, f"Tool '{name}' raised: {e}", retryable=False)
                        result = ToolResult.from_legacy(raw)

                    # Track consecutive errors of this exact call.
                    if result.ok:
                        retry_counts[call_hash] = 0
                    else:
                        retry_counts[call_hash] = retry_counts.get(call_hash, 0) + 1

                    # Count this tool's family for the family-budget gate.
                    family = TOOL_FAMILIES.get(name)
                    if family:
                        family_counts[family] = family_counts.get(family, 0) + 1

            if result.ok:
                all_errors = False
                if name in _write_tools:
                    _write_done = True
                # Slice 0.1: propagate files_touched into the session store so
                # the context engine's T2 tier can show "files touched this
                # session" in subsequent turns. No-op if no session_id (legacy
                # callers) or the tool didn't report files_touched.
                if session_id:
                    touched = result.meta.get("files_touched") if result.meta else None
                    if touched:
                        try:
                            from core.conversation_store import get_store
                            get_store().note(session_id, files_touched=list(touched))
                        except Exception as e:  # noqa: BLE001
                            logger.debug(f"files_touched propagation failed: {e}")

            _emit(stream_cb, {
                "type": "tool_result",
                "id": tool_uid,
                "tool": name,
                "ok": result.ok,
                "code": result.error_code.value if result.error_code is not None else None,
                "preview": result.preview(800),
            })
            results.append({"name": name, "args": args, "result": result})

        # Error-streak safety valve (separate from per-call Stop-the-Line)
        if all_errors:
            error_streak += 1
            if error_streak >= MAX_ERROR_STREAK:
                final_msg = (
                    f"Stopping: {MAX_ERROR_STREAK} consecutive all-error turns. "
                    f"Last errors: {[r['result'].preview(200) for r in results]}"
                )
                _emit(stream_cb, {"type": "final", "text": final_msg})
                return final_msg
        else:
            error_streak = 0

        feedback = _format_results(results, all_errors)
        conversation.append({"role": "assistant", "content": content})
        conversation.append({"role": "user", "content": feedback})

    final_msg = f"ERROR: Max turns ({max_turns}) reached without a final answer."
    _emit(stream_cb, {"type": "final", "text": final_msg})
    return final_msg
