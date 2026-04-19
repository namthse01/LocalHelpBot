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
# Self-healing budgets (bounded retries — pattern borrowed from openclaw's
# auto-fix loops: classify the failure, retry with escalating guidance,
# escalate to the user only after the budget is spent).
MAX_PARSE_RETRIES = 3  # malformed tool_use JSON
MAX_WRITE_NUDGES = 3   # user asked to write a file but agent keeps skipping

# Micro-compaction (port of claude-code's microCompact.ts).
# When the conversation grows past MICRO_COMPACT_TURNS, older tool-result
# feedback blocks whose content exceeds MICRO_COMPACT_MIN_CHARS are replaced
# with a short placeholder. Keeps the model's working memory lean while
# preserving the narrative thread (user/assistant messages untouched).
MICRO_COMPACT_TURNS     = 6
MICRO_COMPACT_MIN_CHARS = 1500
MICRO_COMPACT_KEEP_TAIL = 2   # most recent tool-result turns kept verbatim
MICRO_COMPACT_PLACEHOLDER = (
    "[Old tool result content cleared for context window — "
    "re-run the tool if you need the full output again.]"
)
# How many chars of the original body to keep as a one-line digest inside the
# placeholder. Lets the model remember *what* was in the result (first match,
# first error, first file line) so it doesn't re-issue the same tool call.
MICRO_COMPACT_DIGEST_CHARS = 180

# Signatures that strongly suggest the model TRIED to call a tool but emitted
# malformed JSON (so our 3 parsers all silently missed it). Used to distinguish
# "truly done" from "attempted-but-broken-syntax".
_TOOL_ATTEMPT_MARKERS = re.compile(
    r'(<tool_use\b|```tool_use\b|\bACTION:\s*\{|"(?:name|tool)"\s*:\s*"'
    r'(?:write_file|edit_file|read_file|read_pdf|write_pdf|read_docx|write_docx|'
    r'run_command|search_web|fetch_url|grep_file|glob_files|list_dir|python_exec|'
    r'install_package|read_file_chunk)")',
    re.IGNORECASE,
)

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


def _detect_malformed_tool_attempt(content: str) -> Optional[str]:
    """Return a diagnostic string if the content *looks like* an attempted tool
    call that failed to parse, else None. Classifies the failure so the nudge
    back to the model is specific (openclaw-style: name the error → fix it)."""
    if not content:
        return None
    if not _TOOL_ATTEMPT_MARKERS.search(content):
        return None
    # Try to locate the first bare-JSON-looking block and capture the decode error.
    for m in _BARE_JSON_RE.finditer(content):
        i = content.find("{", m.start())
        if i < 0:
            continue
        try:
            json.JSONDecoder().raw_decode(content[i:])
            return None  # parses fine — real issue is elsewhere (not malformed)
        except json.JSONDecodeError as e:
            snippet = content[i:i + 240].replace("\n", "\\n")
            return (f"JSONDecodeError at pos {e.pos}: {e.msg}. "
                    f"Offending snippet: {snippet}…")
    # No bare-JSON match but a marker was found (e.g. unclosed <tool_use>)
    return "Tool-use marker present but no valid JSON object could be extracted."


def _parse_error_nudge(diag: str, retry_idx: int) -> str:
    escalations = [
        # First try: gentle, explain the encoding.
        ("Your previous message looked like a tool_use call but its JSON was "
         "malformed and could not be parsed. Re-emit it using this EXACT format:\n"
         "<tool_use>{\"name\": \"<tool>\", \"input\": {\"path\": \"...\", \"content\": \"...\"}}</tool_use>\n"
         "Rules for the `content` string:\n"
         "  • Escape every newline as \\n (NOT a raw line break).\n"
         "  • Escape every double-quote as \\\".\n"
         "  • Escape every backslash as \\\\.\n"
         "Emit ONE tool_use block only."),
        # Second try: blame length, suggest chunking.
        ("Still malformed. Your content string is probably too long with too "
         "many fragile escapes. Strategy: emit write_file with a SHORT content "
         "(just the first section ~2KB). On the next turn, call edit_file to "
         "append the rest. Split the work."),
        # Third try: last chance.
        ("Final retry. Emit a MINIMAL <tool_use> with very short content (<500 "
         "chars). If you cannot produce valid JSON, stop and tell the user "
         "exactly which tool you were trying to call and why it failed."),
    ]
    idx = min(retry_idx, len(escalations) - 1)
    return f"{escalations[idx]}\n\nParser diagnostic: {diag}"


def _write_nudge_text(retry_idx: int) -> str:
    escalations = [
        ("You have NOT called a write_* tool yet, but the user asked you to "
         "save the result to a file. Do NOT stop here. Emit ONE write_file / "
         "write_pdf / write_docx tool_use now with the final content at the "
         "EXACT path the user requested — do NOT rename the file or change "
         "the directory."),
        ("Still no write_* call. Check the user's original request again: "
         "use the EXACT filename and EXACT directory they specified. If the "
         "content is long, emit write_file with the first chunk now, then "
         "append with edit_file on subsequent turns."),
        ("Last attempt. Either emit a valid write_* tool_use at the exact "
         "path requested, or report clearly to the user that you cannot "
         "produce the file and explain why."),
    ]
    return escalations[min(retry_idx, len(escalations) - 1)]


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


_ARG_PRIORITY = ("path", "command", "url", "query", "pattern", "agent")


def _brief_args(args: Dict[str, Any], limit: int = 160) -> str:
    """Flatten args into a compact, attribute-safe one-liner for the tool_result
    wrapper. Keeps identifying keys (path/url/cmd) so micro-compacted placeholders
    still tell the model *which* target was queried — no re-issue needed."""
    if not isinstance(args, dict) or not args:
        return ""
    pieces: List[str] = []
    seen_keys = set()
    for k in _ARG_PRIORITY:
        if k in args:
            seen_keys.add(k)
            v = str(args[k])[:80].replace("\n", " ").replace('"', "'")
            pieces.append(f"{k}={v}")
    for k, v in args.items():
        if k in seen_keys:
            continue
        s = str(v)[:40].replace("\n", " ").replace('"', "'")
        pieces.append(f"{k}={s}")
        if len(pieces) >= 4:
            break
    out = " ".join(pieces)
    # Escape angle brackets to avoid breaking XML-ish parsers that look for
    # `<tool_result`. Ampersand last so we don't double-encode.
    out = out.replace("<", "‹").replace(">", "›")
    return out[:limit]


def _extract_digest(body: str, limit: int = MICRO_COMPACT_DIGEST_CHARS) -> str:
    """One-line digest of a tool_result body — collapses whitespace, drops leading
    error-signal noise when possible, truncates. Used when micro-compacting so
    the placeholder still carries a hint of what the result contained."""
    if not body:
        return ""
    text = re.sub(r"\s+", " ", body).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _micro_compact(conversation: List[Dict[str, Any]]) -> int:
    """Replace old, large tool_result feedback bodies with a placeholder +
    one-line digest. Walks the conversation backwards. User messages that
    contain a `<tool_result` block AND are older than the last
    MICRO_COMPACT_KEEP_TAIL such messages get their body replaced — BUT we
    keep the structural wrapper (name, args, is_error) AND a short digest of
    the original body so the model still knows *what* was in the result and
    which target it applied to.

    Returns the number of messages compacted (for telemetry).
    """
    tool_feedback_indices: List[int] = []
    for i, m in enumerate(conversation):
        if m.get("role") != "user":
            continue
        body = m.get("content", "")
        if isinstance(body, str) and "<tool_result" in body and len(body) > MICRO_COMPACT_MIN_CHARS:
            tool_feedback_indices.append(i)
    # Keep the most recent MICRO_COMPACT_KEEP_TAIL intact
    to_compact = tool_feedback_indices[:-MICRO_COMPACT_KEEP_TAIL] if len(tool_feedback_indices) > MICRO_COMPACT_KEEP_TAIL else []
    compacted = 0
    for idx in to_compact:
        # Already compacted? skip.
        if MICRO_COMPACT_PLACEHOLDER in (conversation[idx].get("content", "") or ""):
            continue
        orig = conversation[idx]["content"]

        def _replace(m: "re.Match") -> str:
            open_tag = m.group(1)  # <tool_result name="X" args="..." is_error="...">
            body     = m.group(2)
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


def _format_tool_results(results: List[Dict[str, Any]], all_errors: bool) -> str:
    """Pack 1+ tool results into a single user-turn feedback message."""
    lines = []
    for r in results:
        body = r["output"][:2000]
        if r["is_error"]:
            hint = _suggest_alternative(r["name"], r["output"])
            if hint:
                body = f"{body}\n\n{hint}"
        arg_str = _brief_args(r.get("args") or {})
        args_attr = f' args="{arg_str}"' if arg_str else ""
        lines.append(
            f'<tool_result name="{r["name"]}"{args_attr} '
            f'is_error="{str(r["is_error"]).lower()}">\n{body}\n</tool_result>'
        )
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
    _write_nudge_count = 0
    _parse_retry_count = 0

    for turn in range(max_turns):
        # Micro-compact before each LLM call once the loop gets long. Keeps
        # the tail (recent tool outputs) intact — model still sees what it
        # just did — but strips oldest verbose bodies to a placeholder.
        if turn >= MICRO_COMPACT_TURNS:
            n = _micro_compact(conversation)
            if n:
                logger.info(f"[agent] micro-compacted {n} old tool-result turns")
                _emit(stream_cb, {
                    "type": "status",
                    "text": f"micro-compacted {n} old tool outputs",
                })
        _emit(stream_cb, {"type": "status", "text": f"Turn {turn + 1}: thinking…"})
        t_turn = _time.perf_counter()
        response = smart_provider.chat(conversation)
        turn_ms = int((_time.perf_counter() - t_turn) * 1000)
        logger.info(f"[agent] turn {turn+1} — {response.provider}/{response.model} {response.timing_str()}")
        content = response.content or ""

        calls = _parse_tool_uses(content)
        # Dedupe retry-storms: when a weak local model panics and re-emits the
        # same tool call 3-5 times in one assistant message (usually because
        # its prior JSON was truncated), keep only the FIRST occurrence per
        # (tool_name, path-or-command). This prevents duplicate writes, wasted
        # permission prompts, and cascading errors.
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

        # No tool calls parsed. Three possibilities — classify before ending:
        #   (a) Attempted tool_use but malformed JSON — retry with diagnostic
        #   (b) User expected a file write and none happened yet — re-nudge
        #   (c) Genuinely done — emit final
        if not calls:
            # (a) Malformed tool_use detection — self-heal via specific nudge
            diag = _detect_malformed_tool_attempt(content)
            if diag and _parse_retry_count < MAX_PARSE_RETRIES:
                _parse_retry_count += 1
                nudge = _parse_error_nudge(diag, _parse_retry_count - 1)
                _emit(stream_cb, {
                    "type": "status",
                    "text": f"malformed tool_use — self-healing (attempt {_parse_retry_count}/{MAX_PARSE_RETRIES})",
                })
                logger.warning(f"[agent] parse retry {_parse_retry_count}: {diag[:200]}")
                conversation.append({"role": "assistant", "content": content})
                conversation.append({"role": "user", "content": nudge})
                continue

            # (b) Write expected but not done — re-armable nudge (up to MAX_WRITE_NUDGES)
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

            # (c) Truly done
            total_ms = int((_time.perf_counter() - t_start) * 1000)
            logger.info(f"[agent] DONE in {turn+1} turn(s), {total_ms}ms total")
            _emit(stream_cb, {"type": "final", "text": content, "total_ms": total_ms, "turns": turn + 1})
            return content

        # A successful parse resets the parse-retry counter.
        _parse_retry_count = 0

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
            results.append({"name": name, "args": args, "output": out, "is_error": is_err})

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
