"""Tool-use / JSON parsing helpers for the agent loop.

Extracted from :mod:`core.agent` (behavior-preserving move — no logic
changes). Handles the four tool_use formats, strips tool-use blocks from
narration, detects malformed tool attempts, and builds the parse-error /
write-output nudge escalation text. All symbols are re-exported from
``core.agent`` so existing import paths keep working.

Tool-use formats parsed (in priority order):
  1. <tool_use>{"name": "...", "input": {...}}</tool_use>  ← preferred
  2. ```tool_use {"name": "...", "input": {...}}```        ← fenced
  3. bare {"name": "...", "input": {...}}                  ← tolerant
  4. ACTION: {"tool": "...", ...}                          ← legacy
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional, Union

from core.tool_schema import ToolRegistry


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


def _scan_json_after(text: str, start: int) -> Optional[tuple]:
    i = text.find("{", start)
    if i < 0:
        return None
    try:
        obj, end = json.JSONDecoder().raw_decode(text[i:])
        return obj, i + end
    except json.JSONDecodeError:
        return None


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
