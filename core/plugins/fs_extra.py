"""Extra filesystem CRUD tools: delete_file, make_dir, move_file.

All destructive ops go through the permission gate. Results are typed
`ToolResult` envelopes so the agent loop can route errors via error_code
and Stop-the-Line.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict

from core.permissions import request_permission
from core.tool_schema import ErrorCode, Tool, ToolRegistry, ToolResult


def _delete_file(args: Dict[str, Any]) -> ToolResult:
    path = args.get("path")
    if not path:
        return ToolResult.error(ErrorCode.INVALID_ARGS, "delete_file requires 'path'.", retryable=False)
    p = Path(path)
    if not p.exists():
        return ToolResult.error(ErrorCode.FILE_NOT_FOUND, f"Path not found: {path}")
    decision = request_permission(
        "delete_file", str(p),
        {"path": str(p), "is_dir": p.is_dir(),
         "size": p.stat().st_size if p.is_file() else None},
    )
    if not decision["allowed"]:
        return ToolResult.error(
            ErrorCode.PERMISSION_DENIED,
            f"User declined delete_file for {path} ({decision['reason']}).",
            retryable=False,
        )
    try:
        if p.is_dir():
            shutil.rmtree(p)
            return ToolResult.success(f"OK: deleted directory {path}", path=str(p), kind="dir")
        p.unlink()
        return ToolResult.success(f"OK: deleted file {path}", path=str(p), kind="file")
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"delete_file failed: {e}")


def _make_dir(args: Dict[str, Any]) -> ToolResult:
    path = args.get("path")
    if not path:
        return ToolResult.error(ErrorCode.INVALID_ARGS, "make_dir requires 'path'.", retryable=False)
    try:
        p = Path(path)
        if p.exists():
            return ToolResult.success(f"OK: directory already exists: {path}", path=str(p), existed=True)
        p.mkdir(parents=True, exist_ok=True)
        return ToolResult.success(f"OK: created {path}", path=str(p), existed=False)
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"make_dir failed: {e}")


def _move_file(args: Dict[str, Any]) -> ToolResult:
    src = args.get("src") or args.get("from")
    dst = args.get("dst") or args.get("to")
    if not src or not dst:
        return ToolResult.error(ErrorCode.INVALID_ARGS, "move_file requires 'src' and 'dst'.", retryable=False)
    sp, dp = Path(src), Path(dst)
    if not sp.exists():
        return ToolResult.error(ErrorCode.FILE_NOT_FOUND, f"Source not found: {src}")
    decision = request_permission(
        "move_file", f"{src} -> {dst}",
        {"src": str(sp), "dst": str(dp), "overwrite": dp.exists()},
    )
    if not decision["allowed"]:
        return ToolResult.error(
            ErrorCode.PERMISSION_DENIED,
            f"User declined move_file ({decision['reason']}).",
            retryable=False,
        )
    try:
        dp.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(sp), str(dp))
        return ToolResult.success(f"OK: moved {src} -> {dst}", src=str(sp), dst=str(dp), files_touched=[str(dp)])
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"move_file failed: {e}")


def register(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="delete_file",
        description="Delete a file or directory (recursive for dirs). Asks user permission.",
        input_schema={"type": "object",
                      "properties": {"path": {"type": "string"}},
                      "required": ["path"]},
        handler=_delete_file,
        requires_permission=True,
        category="fs",
    ))
    registry.register(Tool(
        name="make_dir",
        description="Create a directory (including parents). Idempotent. No permission prompt.",
        input_schema={"type": "object",
                      "properties": {"path": {"type": "string"}},
                      "required": ["path"]},
        handler=_make_dir,
        category="fs",
    ))
    registry.register(Tool(
        name="move_file",
        description="Move or rename a file/directory. Asks user permission.",
        input_schema={
            "type": "object",
            "properties": {"src": {"type": "string"}, "dst": {"type": "string"}},
            "required": ["src", "dst"],
        },
        handler=_move_file,
        requires_permission=True,
        category="fs",
    ))
