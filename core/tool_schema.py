"""Tool schema + typed result envelope.

Ports the `Tool` shape from claude-code (src/Tool.ts) but adds the
typed `ToolResult` envelope inspired by `openclaw/src/agents/bash-tools.exec.ts`
and `openclaw/src/acp/runtime/errors.ts`.

Why this matters: before this rewrite every tool returned a free-form
string. "ERROR: x" was the only signal of failure and the agent loop had
no idea whether the call was retryable, what category of error it hit,
or how to recover. With `ToolResult` the agent loop can:

  • Render `<tool_result is_error="true" code="STALE_OLD_STRING" hint="...">`
    so the model gets structured failure feedback.
  • Implement Stop-the-Line: count consecutive errors per
    (tool_name, args_hash) and refuse the third retry of a doomed call.
  • Track files touched per session for the context engine's T2 tier.

Legacy handlers that still return raw strings are tolerated via
`ToolResult.from_legacy(text)` — it sniffs the historical "ERROR:" /
"PERMISSION_DENIED:" / "OK:" prefixes and maps them onto error codes.
The plugin loader uses this so a half-migrated codebase still works.
"""

from __future__ import annotations

import enum
import importlib
import logging
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Union

logger = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────────────────
# Error codes (stable enum)
# ───────────────────────────────────────────────────────────────────────


class ErrorCode(str, enum.Enum):
    """Stable error codes — the agent's system prompt has a recovery
    table keyed on these. Adding a new code is fine; renaming an
    existing one breaks the prompt table, so don't."""

    FILE_NOT_FOUND     = "FILE_NOT_FOUND"
    PERMISSION_DENIED  = "PERMISSION_DENIED"
    STALE_OLD_STRING   = "STALE_OLD_STRING"
    AMBIGUOUS_MATCH    = "AMBIGUOUS_MATCH"
    INVALID_ARGS       = "INVALID_ARGS"
    TOO_LARGE          = "TOO_LARGE"
    EXTERNAL_TIMEOUT   = "EXTERNAL_TIMEOUT"
    RATE_LIMITED       = "RATE_LIMITED"
    NO_RESULTS         = "NO_RESULTS"
    LOOP_BUDGET        = "LOOP_BUDGET"     # Stop-the-Line synth result
    UNKNOWN            = "UNKNOWN"

    def __str__(self) -> str:  # so f"{code}" prints just the value
        return self.value


# Per-code recovery hints injected into the system-prompt tool catalog.
# Keep these short — they show up in every system prompt that includes
# tools.
RECOVERY_HINTS: Dict[ErrorCode, str] = {
    ErrorCode.FILE_NOT_FOUND:    "Re-list the directory or correct the path before retrying.",
    ErrorCode.PERMISSION_DENIED: "User declined. Do NOT retry — report back and ask the user.",
    ErrorCode.STALE_OLD_STRING:  "Re-read the file and copy the exact text (whitespace included).",
    ErrorCode.AMBIGUOUS_MATCH:   "Add surrounding context so old_string is unique, or set replace_all=true.",
    ErrorCode.INVALID_ARGS:      "Check the tool schema; arg names/types must match.",
    ErrorCode.TOO_LARGE:         "Chunk the payload — write a header first, then append with edit_file.",
    ErrorCode.EXTERNAL_TIMEOUT:  "Network/IO is slow. Try once more with a simpler command or different URL.",
    ErrorCode.RATE_LIMITED:      "Provider is throttling. Back off or fall back to local.",
    ErrorCode.NO_RESULTS:        "Empty result. Refine the query or admit the corpus has no hit.",
    ErrorCode.LOOP_BUDGET:       "Stop-the-Line fired: same call failed twice. Revise plan.",
    ErrorCode.UNKNOWN:           "Inspect the message and decide whether the call can be reshaped.",
}


# ───────────────────────────────────────────────────────────────────────
# Tool result envelope
# ───────────────────────────────────────────────────────────────────────


@dataclass
class ToolResult:
    """Structured result of one tool invocation.

    Convention:
      • ok=True  → output is the real return value (text the agent reads).
      • ok=False → output is the error message, error_code is set, hint
        is the per-call recovery suggestion.

    `meta` is for non-displayed bookkeeping (e.g. files touched, bytes
    written, status codes). The agent loop forwards a subset to the
    conversation store so the context engine can use it in T2/T4.
    """

    ok: bool
    output: str
    error_code: Optional[ErrorCode] = None
    hint: Optional[str] = None
    retryable: bool = True
    meta: Dict[str, Any] = field(default_factory=dict)

    # ── constructors ────────────────────────────────────────────────
    @classmethod
    def success(cls, output: str, **meta: Any) -> "ToolResult":
        return cls(ok=True, output=output, meta=dict(meta))

    @classmethod
    def error(
        cls,
        code: ErrorCode,
        output: str,
        *,
        hint: Optional[str] = None,
        retryable: bool = True,
        **meta: Any,
    ) -> "ToolResult":
        hint = hint or RECOVERY_HINTS.get(code)
        return cls(ok=False, output=output, error_code=code, hint=hint, retryable=retryable, meta=dict(meta))

    @classmethod
    def from_legacy(cls, raw: Union[str, "ToolResult", None]) -> "ToolResult":
        """Adapt a string return value (from a not-yet-migrated handler)
        to a ToolResult.

        Conventions sniffed:
          • "ERROR: File not found: …" → FILE_NOT_FOUND
          • "PERMISSION_DENIED: …"     → PERMISSION_DENIED
          • "ERROR: old_string not found …" → STALE_OLD_STRING
          • "ERROR: old_string appears N times" → AMBIGUOUS_MATCH
          • "ERROR: write_file content too large" → TOO_LARGE
          • "ERROR: Command timed out" → EXTERNAL_TIMEOUT
          • "No matches"/"No results"/"NO_DATA" → success with NO_RESULTS-ish output
          • otherwise "ERROR: ..." → UNKNOWN
          • everything else                  → success
        """
        if raw is None:
            return cls.success("")
        if isinstance(raw, ToolResult):
            return raw

        text = str(raw)
        low = text.lower()

        if text.startswith("PERMISSION_DENIED"):
            return cls.error(ErrorCode.PERMISSION_DENIED, text, retryable=False)

        if text.startswith("ERROR"):
            if "file not found" in low or "path not found" in low:
                return cls.error(ErrorCode.FILE_NOT_FOUND, text)
            if "old_string not found" in low:
                return cls.error(ErrorCode.STALE_OLD_STRING, text)
            if "old_string appears" in low and "times" in low:
                return cls.error(ErrorCode.AMBIGUOUS_MATCH, text)
            if "too large" in low or "content too large" in low:
                return cls.error(ErrorCode.TOO_LARGE, text)
            if "timed out" in low or "timeout" in low:
                return cls.error(ErrorCode.EXTERNAL_TIMEOUT, text)
            if "invalid" in low and ("arg" in low or "param" in low or "regex" in low):
                return cls.error(ErrorCode.INVALID_ARGS, text)
            return cls.error(ErrorCode.UNKNOWN, text)

        return cls.success(text)

    # ── rendering for the agent's tool_result tag ────────────────────
    def render_xml(self) -> str:
        """Render into `<tool_result is_error="…" code="…" hint="…">body</tool_result>`."""
        attrs = []
        if not self.ok:
            attrs.append('is_error="true"')
            if self.error_code is not None:
                # Force the enum's `.value` so f-string doesn't emit "ErrorCode.X".
                attrs.append(f'code="{self.error_code.value}"')
            if self.hint:
                # Hints are short — escape quotes only.
                safe_hint = self.hint.replace('"', "'")
                attrs.append(f'hint="{safe_hint}"')
            if not self.retryable:
                attrs.append('retryable="false"')
        attr_str = (" " + " ".join(attrs)) if attrs else ""
        return f"<tool_result{attr_str}>\n{self.output}\n</tool_result>"

    # ── compact one-line preview for streams / logs ──────────────────
    def preview(self, limit: int = 200) -> str:
        head = (self.output or "").splitlines()[0] if self.output else ""
        if len(head) > limit:
            head = head[: limit - 1] + "…"
        if self.ok:
            return f"OK {head}"
        return f"ERR[{self.error_code or '?'}] {head}"


# ───────────────────────────────────────────────────────────────────────
# Tool / ToolRegistry
# ───────────────────────────────────────────────────────────────────────


@dataclass
class Tool:
    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: Callable[[Dict[str, Any]], Union[ToolResult, str]]
    requires_permission: bool = False
    category: str = "general"

    def run(self, args: Dict[str, Any]) -> ToolResult:
        """Invoke the handler and normalise the return value to a ToolResult.

        Legacy handlers that still return strings get adapted via
        `ToolResult.from_legacy(...)` so a half-migrated codebase still
        compiles. Exceptions inside the handler become an UNKNOWN
        ToolResult (the agent loop logs and surfaces them).
        """
        try:
            raw = self.handler(args or {})
        except Exception as e:  # noqa: BLE001 — tool handlers are user code
            return ToolResult.error(ErrorCode.UNKNOWN, f"Tool '{self.name}' raised: {e}", retryable=False)
        return ToolResult.from_legacy(raw)


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()):
        self._tools: Dict[str, Tool] = {}
        for t in tools:
            self.register(t)

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return sorted(self._tools.keys())

    def filter(self, names: Iterable[str]) -> "ToolRegistry":
        """Subset registry — used for per-specialist tool allowlists."""
        wanted = set(names)
        return ToolRegistry(t for n, t in self._tools.items() if n in wanted)

    def __iter__(self):
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools


# ───────────────────────────────────────────────────────────────────────
# Default registry assembly
# ───────────────────────────────────────────────────────────────────────


def _str(desc: str = "") -> Dict[str, Any]:
    return {"type": "string", "description": desc}


def build_default_registry() -> ToolRegistry:
    """Build the canonical ToolRegistry: file/web tools wrapped + plugins."""
    from core import tools as T

    reg = ToolRegistry([
        Tool(
            name="read_file",
            description="Read a UTF-8 text file from disk (max 200 KB). Returns content or a typed error.",
            input_schema={"type": "object", "properties": {"path": _str("Absolute or relative file path")}, "required": ["path"]},
            handler=lambda a: T.read_file(a["path"]),
            category="fs",
        ),
        Tool(
            name="write_file",
            description="Overwrite a file with the given content. Asks user permission.",
            input_schema={"type": "object", "properties": {"path": _str(), "content": _str()}, "required": ["path", "content"]},
            handler=T._gated_write_file,
            requires_permission=True,
            category="fs",
        ),
        Tool(
            name="edit_file",
            description="Exact-string replace in a file; old_string must be unique unless replace_all=true. Asks permission.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": _str(), "old_string": _str(), "new_string": _str(),
                    "replace_all": {"type": "boolean"},
                },
                "required": ["path", "old_string", "new_string"],
            },
            handler=T._gated_edit_file,
            requires_permission=True,
            category="fs",
        ),
        Tool(
            name="list_dir",
            description="List files and folders at a path.",
            input_schema={"type": "object", "properties": {"path": _str()}},
            handler=lambda a: T.list_dir(a.get("path", ".")),
            category="fs",
        ),
        Tool(
            name="grep_file",
            description="Regex search within a single file; returns matching lines with line numbers.",
            input_schema={"type": "object", "properties": {"path": _str(), "pattern": _str()}, "required": ["path", "pattern"]},
            handler=lambda a: T.grep_file(a["path"], a["pattern"]),
            category="fs",
        ),
        Tool(
            name="glob_files",
            description="Find files matching a glob pattern (e.g. '**/*.py'). Sorted by mtime desc, max 200.",
            input_schema={"type": "object", "properties": {"pattern": _str(), "path": _str()}, "required": ["pattern"]},
            handler=lambda a: T.glob_files(a["pattern"], a.get("path", ".")),
            category="fs",
        ),
        Tool(
            name="run_command",
            description="Execute a shell command (60s timeout). Asks user permission.",
            input_schema={"type": "object", "properties": {"command": _str(), "cwd": _str()}, "required": ["command"]},
            handler=T._gated_run_command,
            requires_permission=True,
            category="shell",
        ),
        Tool(
            name="search_web",
            description="DuckDuckGo web search. Returns top results with titles, URLs, snippets.",
            input_schema={"type": "object", "properties": {"query": _str()}, "required": ["query"]},
            handler=lambda a: T.search_web(a["query"]),
            category="web",
        ),
        Tool(
            name="query_rag",
            description=(
                "Search the local RAG corpus (CAD/AutoCAD docs by default). "
                "Use when the answer likely lives in the indexed knowledge base — "
                "otherwise answer from base knowledge. Returns chunks with score "
                "and source. Output starts with NO_DATA when nothing's above min_score."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": _str("Search query"),
                    "top_k": {"type": "integer", "description": "Max chunks (default 3, cap 10)"},
                    "min_score": {"type": "number", "description": "Reject chunks below this (default 0.4)"},
                },
                "required": ["query"],
            },
            handler=lambda a: T.query_rag(
                a["query"],
                top_k=int(a.get("top_k", 3) or 3),
                min_score=float(a.get("min_score", 0.4) or 0.4),
            ),
            category="rag",
        ),
        Tool(
            name="fetch_url",
            description="Fetch a URL and return stripped text (max 6K chars).",
            input_schema={"type": "object", "properties": {"url": _str()}, "required": ["url"]},
            handler=lambda a: T.fetch_url(a["url"]),
            category="web",
        ),
    ])
    load_plugins(reg)
    return reg


def load_plugins(registry: "ToolRegistry") -> List[str]:
    """Discover and register every plugin in core.plugins.

    Each plugin module must expose `register(registry: ToolRegistry) -> None`.
    Returns the list of plugin module names loaded.
    Failures in one plugin never abort the rest — logged and skipped.
    """
    loaded: List[str] = []
    try:
        pkg = importlib.import_module("core.plugins")
    except ModuleNotFoundError:
        return loaded
    for info in pkgutil.iter_modules(pkg.__path__):
        if info.name.startswith("_"):
            continue
        mod_name = f"core.plugins.{info.name}"
        try:
            mod = importlib.import_module(mod_name)
            reg_fn = getattr(mod, "register", None)
            if callable(reg_fn):
                reg_fn(registry)
                loaded.append(info.name)
            else:
                logger.warning(f"[plugins] {mod_name} has no register() — skipping")
        except Exception as e:
            logger.exception(f"[plugins] failed to load {mod_name}: {e}")
    if loaded:
        logger.info(f"[plugins] loaded: {', '.join(loaded)}")
    return loaded


__all__ = [
    "ErrorCode",
    "RECOVERY_HINTS",
    "ToolResult",
    "Tool",
    "ToolRegistry",
    "build_default_registry",
    "load_plugins",
]
