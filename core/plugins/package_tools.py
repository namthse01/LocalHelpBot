"""
Dynamic self-extension: let the agent install Python packages it needs.

Flow:
  1. Agent hits a `ModuleNotFoundError` (surfaced by python_exec / run_command).
  2. Agent calls install_package {"name": "pandas", "reason": "need dataframes for CSV stats"}.
  3. request_permission() fires a modal that shows the package name + reason so
     the user can see WHAT is being installed and WHY before approving.
  4. On approval, we pip-install into the currently running interpreter's venv.
  5. If the package exposes tools (has a `register_tools` entrypoint or a sibling
     plugin module dropped by a post-install hook), they become available on the
     next request. For simple libs, the agent just imports them via python_exec.

This is intentionally narrow: we only pip-install from PyPI, one package per call,
and there is no arbitrary-URL install path. That keeps the blast radius small.
"""
from __future__ import annotations

import importlib
import re
import subprocess
import sys
from typing import Any, Dict

from core.permissions import request_permission
from core.tool_schema import Tool, ToolRegistry

# Conservative validator: letters, digits, dot, dash, underscore, plus optional
# PEP 440 version specifier. Blocks shell metacharacters, URLs, file paths.
_PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9,._-]+\])?(?:[<>=!~]=?[A-Za-z0-9._*+-]+)?$")


def _check_already_installed(name: str) -> bool:
    base = re.split(r"[<>=!~\[]", name, 1)[0].strip()
    try:
        importlib.import_module(base)
        return True
    except Exception:
        return False


def _install_package(args: Dict[str, Any]) -> str:
    name = (args.get("name") or "").strip()
    reason = (args.get("reason") or "").strip() or "(no reason given)"
    if not name:
        return "ERROR: install_package requires 'name'."
    if not _PKG_RE.match(name):
        return (f"ERROR: package name {name!r} looks unsafe. "
                "Only PyPI-style names are allowed (letters, digits, ._-, optional [extras] and version specifier).")

    if _check_already_installed(name):
        return f"OK: '{name}' already importable — no install needed."

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
        return f"PERMISSION_DENIED: user declined install of '{name}' ({decision['reason']})."

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", name],
            capture_output=True, text=True, timeout=300,
            encoding="utf-8", errors="replace",
        )
        tail_out = "\n".join(result.stdout.splitlines()[-20:])
        tail_err = "\n".join(result.stderr.splitlines()[-10:])
        if result.returncode != 0:
            return f"ERROR: pip install failed (exit {result.returncode}).\nSTDERR:\n{tail_err}\nSTDOUT:\n{tail_out}"
        # Invalidate import caches so fresh import works immediately.
        importlib.invalidate_caches()
        return f"OK: installed '{name}'. Last lines of pip output:\n{tail_out}"
    except subprocess.TimeoutExpired:
        return "ERROR: pip install timed out (300s)."
    except Exception as e:
        return f"ERROR: {e}"


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
                         "description": "PyPI package name, optional [extras] and version (e.g. 'requests', 'pandas>=2', 'uvicorn[standard]')"},
                "reason": {"type": "string",
                           "description": "Why this package is needed (shown to the user in the approval modal)"},
            },
            "required": ["name", "reason"],
        },
        handler=_install_package,
        requires_permission=True,
        category="meta",
    ))
