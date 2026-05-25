"""Execution + process-management tools.

  python_exec    — run a short Python snippet in a subprocess (permission-gated).
  list_processes — snapshot of running processes (name, pid, cpu, memory).
  kill_process   — terminate a pid (permission-gated).

`list_processes` / `kill_process` prefer `psutil` for richer info but fall
back to platform utilities (tasklist / taskkill on Windows, ps / kill on
Unix). If psutil is missing the agent can call `install_package`.

All handlers return typed `ToolResult` envelopes.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
from typing import Any, Dict

from core.permissions import request_permission
from core.tool_schema import ErrorCode, Tool, ToolRegistry, ToolResult


# ──────────────────────────────────────────
#  python_exec
# ──────────────────────────────────────────


def _python_exec(args: Dict[str, Any]) -> ToolResult:
    code = args.get("code")
    timeout = int(args.get("timeout", 30))
    if not code:
        return ToolResult.error(ErrorCode.INVALID_ARGS, "python_exec requires 'code'.", retryable=False)
    if timeout > 300:
        timeout = 300

    decision = request_permission(
        "python_exec",
        code[:80].replace("\n", " | "),
        {"code_preview": code[:500], "lines": code.count("\n") + 1, "timeout": timeout},
    )
    if not decision["allowed"]:
        return ToolResult.error(
            ErrorCode.PERMISSION_DENIED,
            f"User declined python_exec ({decision['reason']}).",
            retryable=False,
        )

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(code)
        script = f.name
    try:
        result = subprocess.run(
            [sys.executable, script],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        parts = []
        if result.stdout.strip():
            parts.append(f"STDOUT:\n{result.stdout.strip()}")
        if result.stderr.strip():
            parts.append(f"STDERR:\n{result.stderr.strip()}")
        parts.append(f"EXIT: {result.returncode}")
        body = "\n".join(parts)
        if result.returncode != 0:
            return ToolResult.error(
                ErrorCode.UNKNOWN, body,
                hint="Inspect STDERR for the underlying failure.",
                exit_code=result.returncode,
            )
        return ToolResult.success(body, exit_code=0)
    except subprocess.TimeoutExpired:
        return ToolResult.error(
            ErrorCode.EXTERNAL_TIMEOUT,
            f"python_exec timed out after {timeout}s.",
            hint="Reduce the work or raise timeout (max 300).",
        )
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"python_exec failed: {e}")
    finally:
        try:
            os.unlink(script)
        except OSError:
            pass


# ──────────────────────────────────────────
#  Process management
# ──────────────────────────────────────────


def _try_psutil():
    try:
        import psutil  # type: ignore
        return psutil
    except ImportError:
        return None


def _list_processes(args: Dict[str, Any]) -> ToolResult:
    name_filter = (args.get("name_contains") or "").lower()
    limit = int(args.get("limit", 40))
    psutil = _try_psutil()
    if psutil:
        rows = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
            info = p.info
            n = (info.get("name") or "").lower()
            if name_filter and name_filter not in n:
                continue
            mem_mb = (info["memory_info"].rss / 1024 / 1024) if info.get("memory_info") else 0
            rows.append((info["pid"], info.get("name") or "?", info.get("cpu_percent") or 0, mem_mb))
        rows.sort(key=lambda r: r[3], reverse=True)
        rows = rows[:limit]
        header = f"{'PID':>7}  {'CPU%':>5}  {'MEM_MB':>8}  NAME"
        lines = [header] + [f"{pid:>7}  {cpu:>5.1f}  {mem:>8.1f}  {name}" for pid, name, cpu, mem in rows]
        return ToolResult.success("\n".join(lines) or "(no processes matched)", count=len(rows))

    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True, timeout=10)
            lines = out.stdout.splitlines()
            rows = []
            for line in lines:
                parts = [x.strip('"') for x in line.split('","')]
                if len(parts) < 5:
                    continue
                name, pid, _sess, _snum, mem = parts[:5]
                if name_filter and name_filter not in name.lower():
                    continue
                rows.append(f"{pid:>7}  {mem:>12}  {name}")
            body = "\n".join(["    PID        MEMORY  NAME"] + rows[:limit]) or "(no processes matched)"
            return ToolResult.success(body, count=len(rows))
        else:
            out = subprocess.run(["ps", "-eo", "pid,pcpu,rss,comm"],
                                 capture_output=True, text=True, timeout=10)
            lines = out.stdout.splitlines()[1:]
            rows = [l for l in lines if not name_filter or name_filter in l.lower()]
            return ToolResult.success("\n".join(rows[:limit]) or "(no processes matched)", count=len(rows))
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(
            ErrorCode.UNKNOWN, f"list_processes failed: {e}",
            hint="Install psutil for better process listing (`install_package` name=psutil).",
        )


def _kill_process(args: Dict[str, Any]) -> ToolResult:
    pid = args.get("pid")
    if not pid:
        return ToolResult.error(ErrorCode.INVALID_ARGS, "kill_process requires 'pid'.", retryable=False)
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return ToolResult.error(ErrorCode.INVALID_ARGS, f"pid must be an integer, got {pid!r}.", retryable=False)

    label = f"pid={pid}"
    psutil = _try_psutil()
    if psutil:
        try:
            label = f"pid={pid} ({psutil.Process(pid).name()})"
        except Exception:
            pass

    decision = request_permission("kill_process", str(pid), {"pid": pid, "label": label})
    if not decision["allowed"]:
        return ToolResult.error(
            ErrorCode.PERMISSION_DENIED,
            f"User declined kill_process for {label} ({decision['reason']}).",
            retryable=False,
        )

    try:
        if psutil:
            p = psutil.Process(pid)
            p.terminate()
            try:
                p.wait(timeout=3)
            except psutil.TimeoutExpired:
                p.kill()
            return ToolResult.success(f"OK: terminated {label}", pid=pid)
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, text=True, timeout=5)
            return ToolResult.success(f"OK: taskkill sent to {label}", pid=pid)
        os.kill(pid, signal.SIGTERM)
        return ToolResult.success(f"OK: SIGTERM sent to {label}", pid=pid)
    except ProcessLookupError:
        return ToolResult.error(ErrorCode.FILE_NOT_FOUND, f"no such process {pid}", retryable=False)
    except PermissionError:
        return ToolResult.error(
            ErrorCode.PERMISSION_DENIED,
            f"OS denied killing {pid} (try running as admin)",
            retryable=False,
        )
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"kill_process failed: {e}")


# ──────────────────────────────────────────
#  Register
# ──────────────────────────────────────────


def register(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="python_exec",
        description=(
            "Run a Python snippet in a subprocess. Asks user permission. "
            "Returns stdout/stderr/exit_code. Default timeout 30s (max 300s)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python source to execute"},
                "timeout": {"type": "integer", "description": "Seconds (default 30, max 300)"},
            },
            "required": ["code"],
        },
        handler=_python_exec,
        requires_permission=True,
        category="exec",
    ))
    registry.register(Tool(
        name="list_processes",
        description="List running processes (uses psutil if available, else tasklist/ps).",
        input_schema={
            "type": "object",
            "properties": {
                "name_contains": {"type": "string", "description": "Case-insensitive substring filter"},
                "limit": {"type": "integer", "description": "Max rows (default 40)"},
            },
        },
        handler=_list_processes,
        category="proc",
    ))
    registry.register(Tool(
        name="kill_process",
        description="Terminate a process by PID. Asks user permission.",
        input_schema={
            "type": "object",
            "properties": {"pid": {"type": "integer"}},
            "required": ["pid"],
        },
        handler=_kill_process,
        requires_permission=True,
        category="proc",
    ))
