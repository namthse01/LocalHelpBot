"""
Extra filesystem CRUD tools: delete_file, make_dir, move_file.

All destructive operations go through the existing permission gate
(`core.permissions.request_permission`) so the UI modal fires exactly
like write_file / run_command.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict

from core.permissions import request_permission
from core.tool_schema import Tool, ToolRegistry


def _delete_file(args: Dict[str, Any]) -> str:
    path = args.get("path")
    if not path:
        return "ERROR: delete_file requires 'path'."
    p = Path(path)
    if not p.exists():
        return f"ERROR: Path not found: {path}"
    decision = request_permission(
        "delete_file", str(p),
        {"path": str(p), "is_dir": p.is_dir(),
         "size": p.stat().st_size if p.is_file() else None},
    )
    if not decision["allowed"]:
        return f"PERMISSION_DENIED: user declined delete_file for {path} ({decision['reason']})."
    try:
        if p.is_dir():
            shutil.rmtree(p)
            return f"OK: deleted directory {path}"
        p.unlink()
        return f"OK: deleted file {path}"
    except Exception as e:
        return f"ERROR: {e}"


def _make_dir(args: Dict[str, Any]) -> str:
    path = args.get("path")
    if not path:
        return "ERROR: make_dir requires 'path'."
    try:
        p = Path(path)
        if p.exists():
            return f"OK: directory already exists: {path}"
        p.mkdir(parents=True, exist_ok=True)
        return f"OK: created {path}"
    except Exception as e:
        return f"ERROR: {e}"


def _move_file(args: Dict[str, Any]) -> str:
    src = args.get("src") or args.get("from")
    dst = args.get("dst") or args.get("to")
    if not src or not dst:
        return "ERROR: move_file requires 'src' and 'dst'."
    sp, dp = Path(src), Path(dst)
    if not sp.exists():
        return f"ERROR: source not found: {src}"
    decision = request_permission(
        "move_file", f"{src} -> {dst}",
        {"src": str(sp), "dst": str(dp), "overwrite": dp.exists()},
    )
    if not decision["allowed"]:
        return f"PERMISSION_DENIED: user declined move_file ({decision['reason']})."
    try:
        dp.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(sp), str(dp))
        return f"OK: moved {src} → {dst}"
    except Exception as e:
        return f"ERROR: {e}"


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
