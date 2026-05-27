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
import operator as _op
import re
import sys
import time as _time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.providers import SAMPLING_CREATIVE, smart_provider
from core.tool_schema import ErrorCode, Tool, ToolRegistry, ToolResult

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
    # Clipboard
    "clipboard_read": "clipboard", "clipboard_write": "clipboard",
    # Meta
    "task": "delegate",
}
TOOL_FAMILY_BUDGET = 4  # Max calls of any one family per task.

# CONVERGE injection — after this many turns without a final answer,
# inject a user-role nudge that forbids more tool calls and forces the
# model to produce an answer (or a clarifying question) on the next turn.
CONVERGE_INJECT_AFTER_TURN = 4

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
    """Replace old verbose tool_result bodies with a placeholder + digest.

    Defense-in-depth: never touch the last assistant message in the
    conversation. The current implementation only mutates user-role
    messages with `<tool_result>` blocks, but if compaction ever extends
    to assistant turns we want to keep the most recent final answer
    around — it's the anchor for "our last result" reference resolution.
    """
    last_assistant_idx = -1
    for i in range(len(conversation) - 1, -1, -1):
        if conversation[i].get("role") == "assistant":
            last_assistant_idx = i
            break
    tool_feedback_indices: List[int] = []
    for i, m in enumerate(conversation):
        if i == last_assistant_idx:
            continue
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
# Recent-exchange capture & arithmetic fast-path
# ───────────────────────────────────────────────────────────────────────

# Matches "+ 4", "- 2.5", "* 10", "/3" — operator followed by a number,
# possibly with whitespace, nothing else on the line.
_FASTPATH_RE = re.compile(r"^\s*([+\-*/])\s*(-?\d+(?:\.\d+)?)\s*$")

# Matches any number in a string. Vietnamese / European thousand separators
# use "." and decimal commas; English uses commas as thousand separators.
# We accept both by stripping commas before float() and treating "," as the
# decimal point only when it's the LAST grouping in the token.
_NUMBER_RE = re.compile(r"-?\d{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?|-?\d+(?:[.,]\d+)?")

_FASTPATH_OPS = {
    "+": _op.add,
    "-": _op.sub,
    "*": _op.mul,
    "/": _op.truediv,
}


def _parse_number_token(tok: str) -> Optional[float]:
    """Parse a number that may use comma or dot as decimal/thousand separator."""
    if not tok:
        return None
    # Strategy: if both `,` and `.` appear, treat the LAST one as the decimal
    # and the others as grouping. If only one appears with 3 digits after it,
    # it's a thousand separator; otherwise it's the decimal point.
    last_dot = tok.rfind(".")
    last_com = tok.rfind(",")
    if last_dot >= 0 and last_com >= 0:
        if last_dot > last_com:
            cleaned = tok.replace(",", "")
        else:
            cleaned = tok.replace(".", "").replace(",", ".")
    elif last_com >= 0:
        # only commas
        tail = tok[last_com + 1:]
        if len(tail) == 3 and tail.isdigit():
            cleaned = tok.replace(",", "")  # thousand grouping
        else:
            cleaned = tok.replace(",", ".")  # decimal
    elif last_dot >= 0:
        # only dots — assume decimal point
        cleaned = tok
    else:
        cleaned = tok
    try:
        return float(cleaned)
    except ValueError:
        return None


def _extract_last_number(text: str) -> Optional[float]:
    """Return the LAST number that appears in `text`, or None if none found.

    Used to capture the numeric answer from an assistant turn like
    "1+1 = 2" → 2.0 or "the total is 1,234.5" → 1234.5.
    """
    if not text:
        return None
    matches = _NUMBER_RE.findall(text)
    for tok in reversed(matches):
        val = _parse_number_token(tok)
        if val is not None:
            return val
    return None


def _last_user_message(messages: List[Dict[str, Any]]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content", "")
            if isinstance(c, list):
                return " ".join(
                    p.get("text", "") for p in c
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            return str(c or "")
    return ""


def _try_arithmetic_fastpath(user_msg: str, prev_numeric: Optional[float]) -> Optional[str]:
    """If `user_msg` is a pure operator-leading expression (e.g. "+ 4") and
    `prev_numeric` is known, evaluate and return a formatted answer.
    Returns None when the message doesn't qualify or no anchor exists."""
    if prev_numeric is None or not user_msg:
        return None
    m = _FASTPATH_RE.match(user_msg)
    if not m:
        return None
    op_sym, operand_tok = m.group(1), m.group(2)
    try:
        operand = float(operand_tok)
    except ValueError:
        return None
    fn = _FASTPATH_OPS.get(op_sym)
    if fn is None:
        return None
    if op_sym == "/" and operand == 0.0:
        return f"{_fmt_num(prev_numeric)} / 0 = undefined (cannot divide by zero)"
    try:
        result = fn(prev_numeric, operand)
    except (ZeroDivisionError, OverflowError, ValueError) as e:
        return f"{_fmt_num(prev_numeric)} {op_sym} {operand_tok} = error ({e})"
    return f"{_fmt_num(prev_numeric)} {op_sym} {_fmt_num(operand)} = {_fmt_num(result)}"


def _fmt_num(x: float) -> str:
    """Render a float without trailing ".0" when it's whole."""
    if x == int(x) and abs(x) < 1e16:
        return str(int(x))
    return f"{x:g}"


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
