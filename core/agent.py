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
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import time as _time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.providers import smart_provider
from core.tool_schema import ErrorCode, Tool, ToolRegistry, ToolResult

logger = logging.getLogger(__name__)

MAX_TURNS = 20
MAX_ERROR_STREAK = 5
MAX_PARSE_RETRIES = 3
MAX_WRITE_NUDGES = 3
# Stop-the-Line threshold. After this many consecutive failures of the
# SAME (tool_name, args_hash), the loop refuses to invoke the handler
# and emits a synthetic LOOP_BUDGET result instead. Set to 2 so the
# third attempt is the one that gets blocked.
STOP_THE_LINE_MAX_RETRIES = 2

# Micro-compaction — replace old verbose tool-result bodies with a
# placeholder + digest once the conversation gets long. Keeps the most
# recent results intact.
MICRO_COMPACT_TURNS     = 6
MICRO_COMPACT_MIN_CHARS = 1500
MICRO_COMPACT_KEEP_TAIL = 2
MICRO_COMPACT_PLACEHOLDER = (
    "[Old tool result content cleared for context window — "
    "re-run the tool if you need the full output again.]"
)
MICRO_COMPACT_DIGEST_CHARS = 180

# Signatures suggesting the model TRIED to call a tool but emitted
# malformed JSON. Used to distinguish "truly done" vs "broken syntax".
_TOOL_ATTEMPT_MARKERS = re.compile(
    r'(<tool_use\b|```tool_use\b|\bACTION:\s*\{|"(?:name|tool)"\s*:\s*"'
    r'(?:write_file|edit_file|read_file|read_pdf|write_pdf|read_docx|write_docx|'
    r'run_command|search_web|fetch_url|grep_file|glob_files|list_dir|python_exec|'
    r'install_package|read_file_chunk)")',
    re.IGNORECASE,
)

_TAG_RE      = re.compile(r"<tool_use>\s*(\{.*?\})\s*</tool_use>", re.DOTALL)
_FENCE_RE    = re.compile(r"```tool_use\s*(\{.*?\})\s*```", re.DOTALL)
_ACTION_RE   = re.compile(r"ACTION:\s*(\{.*?\})\s*(?=\n|$)", re.DOTALL)
_TAG_OPEN_RE = re.compile(r"<tool_use>\s*", re.IGNORECASE)
_BARE_JSON_RE = re.compile(r'\{\s*"(?:name|tool)"\s*:', re.IGNORECASE)


ToolsArg = Union[Dict[str, Callable[[Dict[str, Any]], Any]], ToolRegistry]


# ───────────────────────────────────────────────────────────────────────
# JSON / tool_use parsing helpers
# ───────────────────────────────────────────────────────────────────────


def _scan_json_after(text: str, start: int) -> Optional[tuple]:
    i = text.find("{", start)
    if i < 0:
        return None
    try:
        obj, end = json.JSONDecoder().raw_decode(text[i:])
        return obj, i + end
    except json.JSONDecodeError:
        return None


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


def _resolve_tool(tools: ToolsArg, name: str):
    """Return a callable(args) -> ToolResult|str|None for the named tool."""
    if isinstance(tools, ToolRegistry):
        t = tools.get(name)
        return t.run if t else None
    if isinstance(tools, dict):
        fn = tools.get(name)
        return fn if callable(fn) else None
    return None


def _tool_names(tools: ToolsArg) -> List[str]:
    if isinstance(tools, ToolRegistry):
        return tools.names()
    if isinstance(tools, dict):
        return sorted(tools.keys())
    return []


def _parse_tool_uses(content: str) -> List[Dict[str, Any]]:
    """Extract all tool calls from an assistant message, in order, deduped.

    Returns list of {"name": str, "input": dict}. Empty list = end_turn.
    Invalid JSON sub-blocks get input={"__parse_error__": "…"}.
    """
    calls: List[Dict[str, Any]] = []

    def _push(obj: Any) -> None:
        if not isinstance(obj, dict):
            return
        name = obj.get("name") or obj.get("tool")
        inp = obj.get("input") or obj.get("arguments") or {}
        if not isinstance(inp, dict):
            inp = {}
        inp.pop("tool", None)
        if name:
            calls.append({"name": name, "input": inp})

    pos = 0
    for m in _TAG_OPEN_RE.finditer(content):
        if m.start() < pos:
            continue
        parsed = _scan_json_after(content, m.end())
        if not parsed:
            continue
        obj, end_idx = parsed
        pos = end_idx
        _push(obj)

    if not calls:
        for m in _FENCE_RE.finditer(content):
            parsed = _scan_json_after(content, m.start(1) if m.lastindex else m.start())
            if parsed:
                _push(parsed[0])

    if not calls:
        pos = 0
        for m in _BARE_JSON_RE.finditer(content):
            if m.start() < pos:
                continue
            parsed = _scan_json_after(content, m.start())
            if not parsed:
                continue
            obj, end_idx = parsed
            if isinstance(obj, dict) and (obj.get("name") or obj.get("tool")) \
               and (isinstance(obj.get("input"), dict) or isinstance(obj.get("arguments"), dict)):
                pos = end_idx
                _push(obj)

    if not calls:
        for m in _ACTION_RE.finditer(content):
            try:
                obj = json.loads(m.group(1))
                name = obj.pop("tool", None)
                if name:
                    calls.append({"name": name, "input": obj})
            except json.JSONDecodeError as e:
                calls.append({"name": "?", "input": {"__parse_error__": str(e)}})

    return calls


def _strip_tool_uses(content: str) -> str:
    """Remove tool-use blocks from assistant text — keep narration only."""
    content = _TAG_RE.sub("", content)
    content = _FENCE_RE.sub("", content)
    content = _ACTION_RE.sub("", content)

    def _strip_open(text: str) -> str:
        out = []
        i = 0
        while True:
            m = _TAG_OPEN_RE.search(text, i)
            if not m:
                out.append(text[i:])
                break
            out.append(text[i:m.start()])
            parsed = _scan_json_after(text, m.end())
            if not parsed:
                out.append(text[m.start():m.end()])
                i = m.end()
                continue
            _, end_idx = parsed
            tail = text[end_idx:end_idx + 20]
            closer = re.match(r"\s*</?tool_use>", tail, re.IGNORECASE)
            i = end_idx + (closer.end() if closer else 0)
        return "".join(out)
    content = _strip_open(content)

    def _strip_bare(text: str) -> str:
        out = []
        i = 0
        while True:
            m = _BARE_JSON_RE.search(text, i)
            if not m:
                out.append(text[i:])
                break
            parsed = _scan_json_after(text, m.start())
            if not parsed:
                out.append(text[i:m.end()])
                i = m.end()
                continue
            obj, end_idx = parsed
            if isinstance(obj, dict) and (obj.get("name") or obj.get("tool")) \
               and (isinstance(obj.get("input"), dict) or isinstance(obj.get("arguments"), dict)):
                out.append(text[i:m.start()])
                i = end_idx
            else:
                out.append(text[i:m.end()])
                i = m.end()
        return "".join(out)
    return _strip_bare(content).strip()


def _detect_malformed_tool_attempt(content: str) -> Optional[str]:
    if not content or not _TOOL_ATTEMPT_MARKERS.search(content):
        return None
    for m in _BARE_JSON_RE.finditer(content):
        i = content.find("{", m.start())
        if i < 0:
            continue
        try:
            json.JSONDecoder().raw_decode(content[i:])
            return None
        except json.JSONDecodeError as e:
            snippet = content[i:i + 240].replace("\n", "\\n")
            return f"JSONDecodeError at pos {e.pos}: {e.msg}. Offending snippet: {snippet}…"
    return "Tool-use marker present but no valid JSON object could be extracted."


def _parse_error_nudge(diag: str, retry_idx: int) -> str:
    escalations = [
        ("Your previous message looked like a tool_use call but its JSON was "
         "malformed. Re-emit using this EXACT format:\n"
         "<tool_use>{\"name\": \"<tool>\", \"input\": {\"path\": \"...\", \"content\": \"...\"}}</tool_use>\n"
         "Rules for the `content` string:\n"
         "  • Escape every newline as \\n (NOT a raw line break).\n"
         "  • Escape every double-quote as \\\".\n"
         "  • Escape every backslash as \\\\.\n"
         "Emit ONE tool_use block only."),
        ("Still malformed. Your content string is probably too long with too many "
         "fragile escapes. Strategy: write the first ~2KB with write_file, then "
         "append the rest with edit_file. Split the work."),
        ("Final retry. Emit a MINIMAL <tool_use> with very short content (<500 chars). "
         "If you cannot produce valid JSON, stop and tell the user which tool you were "
         "trying to call and why it failed."),
    ]
    idx = min(retry_idx, len(escalations) - 1)
    return f"{escalations[idx]}\n\nParser diagnostic: {diag}"


def _write_nudge_text(retry_idx: int) -> str:
    escalations = [
        ("You have NOT called a write_* tool yet, but the user asked to save the "
         "result to a file. Emit ONE write_file / write_pdf / write_docx tool_use "
         "now with the final content at the EXACT path the user requested."),
        ("Still no write_* call. Use the EXACT filename and EXACT directory the "
         "user specified. If content is long, write the first chunk now and "
         "append with edit_file later."),
        ("Last attempt. Either emit a valid write_* tool_use at the exact path, "
         "or report clearly to the user that you cannot produce the file and why."),
    ]
    return escalations[min(retry_idx, len(escalations) - 1)]


# ───────────────────────────────────────────────────────────────────────
# Tool result rendering / packing
# ───────────────────────────────────────────────────────────────────────


_ARG_PRIORITY = ("path", "command", "url", "query", "pattern", "agent")


def _args_hash(name: str, args: Dict[str, Any]) -> str:
    """Stable hash of (tool_name, args). Used to detect Stop-the-Line."""
    try:
        canonical = json.dumps({"n": name, "a": args}, sort_keys=True, default=str)
    except Exception:
        canonical = f"{name}::{args!r}"
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


def _brief_args(args: Dict[str, Any], limit: int = 160) -> str:
    """Compact arg one-liner — keeps identifying keys so micro-compacted
    placeholders still tell the model which target was queried."""
    if not isinstance(args, dict) or not args:
        return ""
    pieces: List[str] = []
    seen = set()
    for k in _ARG_PRIORITY:
        if k in args:
            seen.add(k)
            v = str(args[k])[:80].replace("\n", " ").replace('"', "'")
            pieces.append(f"{k}={v}")
    for k, v in args.items():
        if k in seen:
            continue
        s = str(v)[:40].replace("\n", " ").replace('"', "'")
        pieces.append(f"{k}={s}")
        if len(pieces) >= 4:
            break
    out = " ".join(pieces).replace("<", "‹").replace(">", "›")
    return out[:limit]


def _extract_digest(body: str, limit: int = MICRO_COMPACT_DIGEST_CHARS) -> str:
    if not body:
        return ""
    text = re.sub(r"\s+", " ", body).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _micro_compact(conversation: List[Dict[str, Any]]) -> int:
    """Replace old verbose tool_result bodies with a placeholder + digest."""
    tool_feedback_indices: List[int] = []
    for i, m in enumerate(conversation):
        if m.get("role") != "user":
            continue
        body = m.get("content", "")
        if isinstance(body, str) and "<tool_result" in body and len(body) > MICRO_COMPACT_MIN_CHARS:
            tool_feedback_indices.append(i)
    to_compact = (
        tool_feedback_indices[:-MICRO_COMPACT_KEEP_TAIL]
        if len(tool_feedback_indices) > MICRO_COMPACT_KEEP_TAIL else []
    )
    compacted = 0
    for idx in to_compact:
        if MICRO_COMPACT_PLACEHOLDER in (conversation[idx].get("content", "") or ""):
            continue
        orig = conversation[idx]["content"]

        def _replace(m: "re.Match") -> str:
            open_tag = m.group(1)
            body = m.group(2)
            close_tag = m.group(3)
            digest = _extract_digest(body)
            digest_line = f"Digest: {digest}" if digest else ""
            return (
                f"{open_tag}\n"
                f"{MICRO_COMPACT_PLACEHOLDER}"
                f"{chr(10) + digest_line if digest_line else ''}\n"
                f"{close_tag}"
            )

        compact_body = re.sub(
            r'(<tool_result[^>]*>)([\s\S]*?)(</tool_result>)',
            _replace,
            orig,
        )
        conversation[idx] = {"role": "user", "content": compact_body}
        compacted += 1
    return compacted


def _render_result_block(name: str, args: Dict[str, Any], result: ToolResult) -> str:
    """Render a single tool result as `<tool_result name=… args=… ...>body</tool_result>`."""
    arg_str = _brief_args(args)
    args_attr = f' args="{arg_str}"' if arg_str else ""
    body = (result.output or "")[:2000]
    if not result.ok:
        attrs = [f'name="{name}"{args_attr}', 'is_error="true"']
        if result.error_code is not None:
            attrs.append(f'code="{result.error_code.value}"')
        if result.hint:
            safe_hint = result.hint.replace('"', "'")[:200]
            attrs.append(f'hint="{safe_hint}"')
        if not result.retryable:
            attrs.append('retryable="false"')
        return f"<tool_result {' '.join(attrs)}>\n{body}\n</tool_result>"
    return f'<tool_result name="{name}"{args_attr} is_error="false">\n{body}\n</tool_result>'


def _format_results(results: List[Dict[str, Any]], all_errors: bool) -> str:
    blocks = [_render_result_block(r["name"], r["args"], r["result"]) for r in results]
    footer = (
        "\n\nAll tools errored — analyze each (see `code` / `hint` attrs) and emit "
        "a corrected tool_use, or write your final answer if you're stuck."
        if all_errors else
        "\n\nContinue with the next tool_use, or write your final answer if done."
    )
    return "\n".join(blocks) + footer


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
) -> str:
    """Run the agent loop.

    `session_id` (optional) is used to propagate `ToolResult.meta["files_touched"]`
    into the conversation store so the context engine's T2 tier can render
    "files touched this session" in subsequent turns. None means the call
    came from a non-session context (legacy run_agent direct invocation);
    file tracking just no-ops in that case.
    """
    conversation = list(messages)
    error_streak = 0
    tool_uid = 0
    t_start = _time.perf_counter()

    # Per-call retry counter for Stop-the-Line. Map of args_hash → count.
    # Reset to 0 on any success of that same call.
    retry_counts: Dict[str, int] = {}

    _write_tools = {"write_file", "write_pdf", "write_docx", "edit_file"}
    _first_user = next((m.get("content", "") for m in messages if m.get("role") == "user"), "")
    _expects_write = bool(
        re.search(
            r"\b(write|save|create|export|output|summariz\w+|report|generate|ghi|lưu|tạo|xuất)\b"
            r".{0,60}(\.(?:pdf|docx|doc|md|txt|csv|json|html|xml|ya?ml)\b|\binto\b|\bto\s+\w+|\bvào\b)",
            _first_user, re.IGNORECASE | re.DOTALL,
        )
        or bool(re.search(r"\binto\s+a?\s*\.?(?:pdf|docx|md|txt|csv|json)\b", _first_user, re.IGNORECASE))
    )
    _write_done = False
    _write_nudge_count = 0
    _parse_retry_count = 0

    for turn in range(max_turns):
        if turn >= MICRO_COMPACT_TURNS:
            n = _micro_compact(conversation)
            if n:
                logger.info(f"[agent] micro-compacted {n} old tool-result turns")
                _emit(stream_cb, {"type": "status", "text": f"micro-compacted {n} old tool outputs"})

        _emit(stream_cb, {"type": "status", "text": f"Turn {turn + 1}: thinking…"})
        t_turn = _time.perf_counter()
        response = smart_provider.chat(conversation)
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
            else:
                handler = _resolve_tool(tools, name)
                if handler is None:
                    result = ToolResult.error(
                        ErrorCode.INVALID_ARGS,
                        f"Unknown tool '{name}'. Available: {', '.join(_tool_names(tools))}",
                        retryable=False,
                    )
                else:
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
