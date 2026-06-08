"""Tool-result rendering / packing + args hashing for the agent loop.

Extracted from :mod:`core.agent` (behavior-preserving move — no logic
changes). Hashes (tool, args) for Stop-the-Line detection, builds compact
arg one-liners, and renders the <tool_result …>body</tool_result> blocks fed
back to the model each turn. Re-exported from ``core.agent``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List

from core.tool_schema import ToolResult


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
