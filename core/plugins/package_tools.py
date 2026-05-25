"""Dynamic self-extension: let the agent install Python packages it needs.

Flow:
  1. Agent hits a `ModuleNotFoundError` (surfaced by python_exec / run_command).
  2. Agent calls install_package {"name": "pandas", "reason": "need dataframes"}.
  3. request_permission() shows the package + reason for user approval.
  4. On approval, we pip-install into the running interpreter's venv.

Narrow scope: PyPI only, one package per call, no arbitrary-URL install.
Returns typed `ToolResult`.
"""
from __future__ import annotations

import importlib
import re
import subprocess
import sys
from typing import Any, Dict

from core.permissions import request_permission
from core.tool_schema import ErrorCode, Tool, ToolRegistry, ToolResult

# Conservative validator: letters, digits, dot, dash, underscore, plus
# optional PEP 440 version specifier. Blocks shell metas, URLs, paths.
_PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9,._-]+\])?(?:[<>=!~]=?[A-Za-z0-9._*+-]+)?$")


def _check_already_installed(name: str) -> bool:
    base = re.split(r"[<>=!~\[]", name, 1)[0].strip()
    try:
        importlib.import_module(base)
        return True
    except Exception:
        return False


def _install_package(args: Dict[str, Any]) -> ToolResult:
    name = (args.get("name") or "").strip()
    reason = (args.get("reason") or "").strip() or "(no reason given)"
    if not name:
        return ToolResult.error(ErrorCode.INVALID_ARGS, "install_package requires 'name'.", retryable=False)
    if not _PKG_RE.match(name):
        return ToolResult.error(
            ErrorCode.INVALID_ARGS,
            f"Package name {name!r} looks unsafe. "
            "Only PyPI-style names allowed (letters, digits, ._-, optional [extras] and version).",
            retryable=False,
        )

    if _check_already_installed(name):
        return ToolResult.success(f"OK: '{name}' already importable — no install needed.", package=name, installed=False)

    decision = request_permission(
        "install_package", name,
        {
            "package": name,
            "reason": reason,
            "interpreter": sys.executable,
            "command": f"{sys.executable} -m pip install {name}",
        },
    )
    if not decision["allowed"]:
        return ToolResult.error(
            ErrorCode.PERMISSION_DENIED,
            f"User declined install of '{name}' ({decision['reason']}).",
            retryable=False,
        )

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", name],
            capture_output=True, text=True, timeout=300,
            encoding="utf-8", errors="replace",
        )
        tail_out = "\n".join(result.stdout.splitlines()[-20:])
        tail_err = "\n".join(result.stderr.splitlines()[-10:])
        if result.returncode != 0:
            return ToolResult.error(
                ErrorCode.UNKNOWN,
                f"pip install failed (exit {result.returncode}).\nSTDERR:\n{tail_err}\nSTDOUT:\n{tail_out}",
                hint="Inspect STDERR — wrong name, network issue, or conflicting deps.",
                package=name,
                exit_code=result.returncode,
            )
        importlib.invalidate_caches()
        return ToolResult.success(
            f"OK: installed '{name}'. Last lines of pip output:\n{tail_out}",
            package=name,
            installed=True,
        )
    except subprocess.TimeoutExpired:
        return ToolResult.error(ErrorCode.EXTERNAL_TIMEOUT, "pip install timed out (300s).")
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"install_package failed: {e}")


def register(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="install_package",
        description=(
            "Install a Python package from PyPI into the current interpreter. "
            "Asks user permission — ALWAYS provide a clear 'reason' so the user "
            "can evaluate the request. Use when a required module is missing."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "PyPI name, optional [extras] and version (e.g. 'requests', 'pandas>=2')"},
                "reason": {"type": "string",
                           "description": "Why this package is needed (shown to user in approval modal)"},
            },
            "required": ["name", "reason"],
        },
        handler=_install_package,
        requires_permission=True,
        category="meta",
    ))
