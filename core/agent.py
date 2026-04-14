"""
Agentic loop — ported patterns from claude-code's src/query.ts + QueryEngine.ts:

  • One "turn" = model call → parse tool_use blocks → execute (parallel) →
    append tool_result blocks → next model call.
  • Terminates when the model emits NO tool_use (claude-code's end_turn).
  • Errors are surfaced as structured tool_result with is_error=True so the
    model can self-correct (same pattern as Anthropic's tool-use API).
  • Emits structured stream events for the UI / ndjson bridge.

The loop accepts EITHER a dict-of-callables (legacy) or a ToolRegistry
(new). Both work, so existing call sites in proxy.py and orchestrator.py
keep functioning.

Tool-use formats understood (in priority order):
  1. <tool_use>{"name": "...", "input": {...}}</tool_use>        ← new, claude-code style, multi-call
  2. ```tool_use {"name": "...", "input": {...}}```              ← fenced variant
  3. ACTION: {"tool": "...", "arg1": ..., ...}                   ← legacy, backward compat
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time as _time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.providers import smart_provider
from core.tool_schema import Tool, ToolRegistry

logger = logging.getLogger(__name__)

MAX_TURNS = 20
MAX_ERROR_STREAK = 5

# Parsers for the 3 supported tool-use encodings
_TAG_RE = re.compile(r"<tool_use>\s*(\{.*?\})\s*</tool_use>", re.DOTALL)
_FENCE_RE = re.compile(r"```tool_use\s*(\{.*?\})\s*```", re.DOTALL)
_ACTION_RE = re.compile(r"ACTION:\s*(\{.*?\})\s*(?=\n|$)", re.DOTALL)
# Tolerant: matches <tool_use>{...}  with ANY/MISSING closer (handles
# models that emit <tool_use>...<tool_use> or forget the closing tag).
_TAG_OPEN_RE = re.compile(r"<tool_use>\s*", re.IGNORECASE)
# Bare JSON tool-use: {"name": "...", "input": {...}} with no wrapping tag.
_BARE_JSON_RE = re.compile(r'\{\s*"(?:name|tool)"\s*:', re.IGNORECASE)


def _scan_json_after(text: str, start: int) -> Optional[tuple]:
    """From `start`, find the next '{' and return (obj, end_index) for the first
    complete JSON object parsed there. Returns None if nothing parseable."""
    i = text.find("{", start)
    if i < 0:
        return None
    try:
        obj, end = json.JSONDecoder().raw_decode(text[i:])
        return obj, i + end
    except json.JSONDecodeError:
        return None

_ERROR_SIGNALS = (
    "ERROR:", "Error:", "Traceback", "SyntaxError", "NameError", "TypeError",
    "ValueError", "AttributeError", "ImportError", "ModuleNotFoundError",
    "FileNotFoundError", "PermissionError", "ConnectionError", "TimeoutError",
    "EXIT: 1", "EXIT: 2", "EXIT: 127", "not found", "cannot find",
    "No such file", "failed with", "PERMISSION_DENIED",
)

ToolsArg = Union[Dict[str, Callable[[Dict[str, Any]], str]], ToolRegistry]


# ──────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────

def _is_error(result: str) -> bool:
    if not result:
        return False
    return any(sig in result[:500] for sig in _ERROR_SIGNALS)


def _emit(cb: Optional[Callable], event):
    if not cb:
        return
    try:
        if isinstance(event, str):
            cb({"type": "text", "text": event})
        else:
            cb(event)
    except Exception as e:
        logger.warning(f"[agent] stream_cb failed: {e}")


def _resolve_tool(tools: ToolsArg, name: str):
    """Return a callable(args) -> str for the given tool name, or None."""
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

    Returns list of {"name": str, "input": dict}. Empty list means "end_turn".
    Invalid JSON blocks are returned with input={"__parse_error__": "..."} so
    the loop can feed the error back.
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

    # Primary: scan every <tool_use> opener and JSON-decode from there.
    # Handles: proper </tool_use>, missing closer, malformed <tool_use>
    # reopener, nested braces, and trailing garbage after the object.
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

    # Format 2: ```tool_use {...}```  (use raw_decode for nested-brace safety)
    if not calls:
        for m in _FENCE_RE.finditer(content):
            parsed = _scan_json_after(content, m.start(1) if m.lastindex else m.start())
            if parsed:
                _push(parsed[0])

    # Format 2b: bare {"name":"...", "input":{...}} with no wrapping tag.
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

    # Format 3 (legacy): ACTION: {"tool": "...", ...}
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
    """Remove all tool-use tags/fences/ACTION lines — leaves only narration."""
    content = _TAG_RE.sub("", content)
    content = _FENCE_RE.sub("", content)
    content = _ACTION_RE.sub("", content)
    # Also strip any malformed <tool_use>{...} blocks the tolerant parser handled
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
            # Also consume a trailing </tool_use> or <tool_use> closer if present
            tail = text[end_idx:end_idx+20]
            closer = re.match(r"\s*</?tool_use>", tail, re.IGNORECASE)
            i = end_idx + (closer.end() if closer else 0)
        return "".join(out)
    content = _strip_open(content)
    # Strip bare {"name":..., "input":{...}} tool-use objects.
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
    content = _strip_bare(content)
    return content.strip()


def _suggest_alternative(tool_name: str, result: str) -> str:
    """Heuristic: map common failure signatures to a concrete next-step tool."""
    r = result or ""
    # Missing Python module → install_package
    m = re.search(r"ModuleNotFoundError: No module named ['\"]([A-Za-z0-9_.-]+)['\"]", r)
    if m:
        pkg = m.group(1).split(".")[0]
        return (f"Hint: the missing module `{pkg}` can be installed by calling "
                f"`install_package` with name=\"{pkg}\" and a clear reason. "
                "The user will be asked to approve.")
    # Command not found (shell)
    if "EXIT: 127" in r or "command not found" in r or "is not recognized as" in r:
        return ("Hint: the shell command isn't on PATH. Consider `install_package` "
                "for a Python equivalent, or use `python_exec` instead of `run_command`.")
    # File-not-found on edit
    if tool_name == "edit_file" and "old_string not found" in r:
        return ("Hint: re-run `read_file` on the target and copy the EXACT text "
                "(including whitespace) before retrying edit_file.")
    # Large file
    if "File too large" in r:
        return "Hint: use `grep_file` to search, or read a slice via python_exec."
    # Permission denied
    if "PERMISSION_DENIED" in r:
        return ("Hint: the user declined this action. Do NOT retry the same call — "
                "either try a safer alternative or stop and report.")
    return ""


def _recovery_hint(tool_name: str, result: str) -> str:
    alt = _suggest_alternative(tool_name, result)
    alt_block = f"\n{alt}\n" if alt else ""
    return (
        f"Tool `{tool_name}` failed. Diagnose and retry:\n"
        "1. ANALYZE the error (path? unique-match? syntax? missing dep?).\n"
        "2. FIX — emit a corrected tool_use, or switch to a different tool.\n"
        "3. VERIFY once the fix applies.\n"
        f"{alt_block}\n"
        f"Error output:\n```\n{result[:1500]}\n```"
    )


def _format_tool_results(results: List[Dict[str, Any]], all_errors: bool) -> str:
    """Pack 1+ tool results into a single user-turn feedback message."""
    lines = []
    for r in results:
        body = r["output"][:2000]
        if r["is_error"]:
            hint = _suggest_alternative(r["name"], r["output"])
            if hint:
                body = f"{body}\n\n{hint}"
        lines.append(f"<tool_result name=\"{r['name']}\" is_error=\"{str(r['is_error']).lower()}\">\n{body}\n</tool_result>")
    footer = (
        "\n\nAll tools errored — analyze each and emit a corrected tool_use, "
        "or write your final answer if you're stuck."
        if all_errors else
        "\n\nContinue with the next tool_use, or write your final answer if done."
    )
    return "\n".join(lines) + footer


# ──────────────────────────────────────────
#  Main loop
# ──────────────────────────────────────────

def run_agent(
    messages: List[Dict[str, str]],
    tools: ToolsArg,
    stream_cb: Optional[Callable] = None,
    max_turns: int = MAX_TURNS,
) -> str:
    conversation = list(messages)
    error_streak = 0
    tool_uid = 0
    t_start = _time.perf_counter()

    # Detect if the user's task requires producing a file as output.
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
    _write_nudged = False

    for turn in range(max_turns):
        _emit(stream_cb, {"type": "status", "text": f"Turn {turn + 1}: thinking…"})
        t_turn = _time.perf_counter()
        response = smart_provider.chat(conversation)
        turn_ms = int((_time.perf_counter() - t_turn) * 1000)
        logger.info(f"[agent] turn {turn+1} — {response.provider}/{response.model} {response.timing_str()}")
        content = response.content or ""

        calls = _parse_tool_uses(content)
        narration = _strip_tool_uses(content) if calls else content

        if narration:
            _emit(stream_cb, {"type": "thought", "text": narration, "turn_ms": turn_ms})

        # No tool calls → END TURN, UNLESS the user asked for a file output
        # and no write_* tool has fired yet. Nudge the model once.
        if not calls:
            if _expects_write and not _write_done and not _write_nudged:
                _write_nudged = True
                nudge = (
                    "You have NOT called a write_* tool yet, but the user asked you to "
                    "save/write the result to a file. Do NOT stop here. Emit ONE write_file / "
                    "write_pdf / write_docx tool_use now with the final content at the path "
                    "the user requested. If only a directory was given, pick a reasonable "
                    "filename (e.g. summary.pdf) inside it."
                )
                _emit(stream_cb, {"type": "status", "text": "nudging agent to write output…"})
                conversation.append({"role": "assistant", "content": content})
                conversation.append({"role": "user", "content": nudge})
                continue
            total_ms = int((_time.perf_counter() - t_start) * 1000)
            logger.info(f"[agent] DONE in {turn+1} turn(s), {total_ms}ms total")
            _emit(stream_cb, {"type": "final", "text": content, "total_ms": total_ms, "turns": turn + 1})
            return content

        # Execute all tool calls from this turn (serially but in declared order)
        results = []
        all_errors = True
        for call in calls:
            tool_uid += 1
            name = call["name"]
            args = call["input"] or {}

            _emit(stream_cb, {"type": "tool_call", "id": tool_uid, "tool": name, "args": args})

            if "__parse_error__" in args:
                out = f"ERROR: Could not parse tool_use JSON: {args['__parse_error__']}"
            else:
                handler = _resolve_tool(tools, name)
                if handler is None:
                    out = f"ERROR: Unknown tool '{name}'. Available: {', '.join(_tool_names(tools))}"
                else:
                    try:
                        out = handler(args)
                    except Exception as e:
                        out = f"ERROR executing {name}: {e}"

            is_err = _is_error(out)
            if not is_err:
                all_errors = False
                if name in _write_tools:
                    _write_done = True
            _emit(stream_cb, {
                "type": "tool_result",
                "id": tool_uid,
                "tool": name,
                "ok": not is_err,
                "preview": (out or "")[:800],
            })
            results.append({"name": name, "output": out, "is_error": is_err})

        # Error-streak safety valve
        if all_errors:
            error_streak += 1
            if error_streak >= MAX_ERROR_STREAK:
                final_msg = (
                    f"Stopping: {MAX_ERROR_STREAK} consecutive all-error turns. "
                    f"Last errors: {[r['output'][:200] for r in results]}"
                )
                _emit(stream_cb, {"type": "final", "text": final_msg})
                return final_msg
        else:
            error_streak = 0

        # Feedback message mirrors Anthropic's tool_result content blocks
        if len(results) == 1 and results[0]["is_error"]:
            feedback = _recovery_hint(results[0]["name"], results[0]["output"])
        else:
            feedback = _format_tool_results(results, all_errors)

        conversation.append({"role": "assistant", "content": content})
        conversation.append({"role": "user", "content": feedback})

    final_msg = f"ERROR: Max turns ({max_turns}) reached without a final answer."
    _emit(stream_cb, {"type": "final", "text": final_msg})
    return final_msg
