"""
Tool schema registry — ports the `Tool` shape from claude-code (src/Tool.ts)
into Python: {name, description, input_schema, handler, requires_permission}.

The existing tool callables in core/tools.py and core/browser.py are wrapped
into Tool objects so the agent loop can:
  - Render a proper catalog into the system prompt (see core/context.py)
  - Validate tool names / arguments centrally
  - Expose metadata (permission gating) to the UI
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Tool:
    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: Callable[[Dict[str, Any]], str]
    requires_permission: bool = False
    category: str = "general"

    def run(self, args: Dict[str, Any]) -> str:
        return self.handler(args or {})


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
        """Return a subset registry (used for per-specialist tool allowlists)."""
        wanted = set(names)
        return ToolRegistry(t for n, t in self._tools.items() if n in wanted)

    def __iter__(self):
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools


# ──────────────────────────────────────────
# Wrap the legacy callables from core/tools.py into Tool objects.
# ──────────────────────────────────────────

def _str(desc: str = "") -> Dict[str, Any]:
    return {"type": "string", "description": desc}


def build_default_registry() -> ToolRegistry:
    from core import tools as T

    reg = ToolRegistry([
        Tool(
            name="read_file",
            description="Read a UTF-8 text file from disk (max 200 KB). Returns content or ERROR:.",
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
    Returns the list of plugin module names loaded (useful for logging/tests).
    Failures in one plugin never abort the rest — they're logged and skipped.
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
