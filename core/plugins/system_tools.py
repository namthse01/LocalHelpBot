"""Deep computer-access tools — Slice 2 of the v4 upgrade.

Tools (all return typed `ToolResult`, all destructive ops permission-gated):

  screenshot              — capture screen → save PNG. Pillow's ImageGrab.
  clipboard_read          — read system clipboard (gated; clipboard often holds secrets).
  clipboard_write         — write to system clipboard (gated).
  system_info             — CPU/RAM/GPU/disks/network/OS/Python info.
  open_with_default_app   — `os.startfile` / `open` / `xdg-open` (gated).
  list_windows            — top-level windows (title, pid, focused) via pywin32 on Windows.
  watch_file              — block until file/dir changes; bounded timeout.
  find_in_files           — recursive grep with bounded result count (faster than read_file).
  read_env                — read a specific env var by name (gated; values may be secrets).

Each tool gracefully degrades when its optional dep is missing — the
ToolResult tells the agent how to install what's needed.
"""
from __future__ import annotations

import json
import os
import platform
import re
import socket
import subprocess
import sys
import time
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.permissions import request_permission
from core.tool_schema import ErrorCode, Tool, ToolRegistry, ToolResult


# ───────────────────────────────────────────────────────────────────────
# screenshot
# ───────────────────────────────────────────────────────────────────────


def _screenshot(args: Dict[str, Any]) -> ToolResult:
    path = (args.get("path") or "").strip()
    region = args.get("region")   # "x,y,w,h" string OR None for full screen
    if not path:
        return ToolResult.error(ErrorCode.INVALID_ARGS, "screenshot requires 'path' (save destination).", retryable=False)

    decision = request_permission("screenshot", path, {"path": path, "region": region})
    if not decision["allowed"]:
        return ToolResult.error(
            ErrorCode.PERMISSION_DENIED,
            f"User declined screenshot ({decision['reason']}).",
            retryable=False,
        )

    try:
        from PIL import ImageGrab  # type: ignore
    except ImportError:
        return ToolResult.error(
            ErrorCode.INVALID_ARGS,
            "Pillow not installed.",
            hint="Call install_package with name='Pillow' and reason='take screenshots'.",
            retryable=False,
        )

    try:
        bbox = None
        if region:
            try:
                parts = [int(x.strip()) for x in str(region).split(",")]
                if len(parts) == 4:
                    x, y, w, h = parts
                    bbox = (x, y, x + w, y + h)
            except Exception:
                return ToolResult.error(
                    ErrorCode.INVALID_ARGS,
                    f"region must be 'x,y,w,h' (got {region!r}).",
                    retryable=False,
                )
        img = ImageGrab.grab(bbox=bbox) if bbox is not None else ImageGrab.grab()
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        img.save(dest, format="PNG")
        size = dest.stat().st_size
        return ToolResult.success(
            f"OK: screenshot saved to {dest} ({size:,} bytes, {img.width}x{img.height})",
            path=str(dest.resolve()),
            width=img.width,
            height=img.height,
            bytes_written=size,
            files_touched=[str(dest.resolve())],
        )
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"screenshot failed: {e}")


# ───────────────────────────────────────────────────────────────────────
# clipboard
# ───────────────────────────────────────────────────────────────────────


def _clipboard_read(args: Dict[str, Any]) -> ToolResult:
    decision = request_permission(
        "clipboard_read", "(clipboard)",
        {"action": "read", "note": "clipboard often contains secrets"},
    )
    if not decision["allowed"]:
        return ToolResult.error(
            ErrorCode.PERMISSION_DENIED,
            f"User declined clipboard_read ({decision['reason']}).",
            retryable=False,
        )
    try:
        import pyperclip  # type: ignore
        text = pyperclip.paste()
        if not isinstance(text, str):
            text = str(text or "")
        return ToolResult.success(text, length=len(text))
    except ImportError:
        return ToolResult.error(
            ErrorCode.INVALID_ARGS,
            "pyperclip not installed.",
            hint="Call install_package with name='pyperclip' and a reason.",
            retryable=False,
        )
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"clipboard_read failed: {e}")


def _clipboard_write(args: Dict[str, Any]) -> ToolResult:
    text = args.get("text")
    if not isinstance(text, str):
        return ToolResult.error(ErrorCode.INVALID_ARGS, "clipboard_write requires 'text' (string).", retryable=False)
    preview = text[:200]
    decision = request_permission(
        "clipboard_write", preview,
        {"action": "write", "length": len(text), "preview": preview},
    )
    if not decision["allowed"]:
        return ToolResult.error(
            ErrorCode.PERMISSION_DENIED,
            f"User declined clipboard_write ({decision['reason']}).",
            retryable=False,
        )
    try:
        import pyperclip  # type: ignore
        pyperclip.copy(text)
        return ToolResult.success(f"OK: copied {len(text)} chars to clipboard.", length=len(text))
    except ImportError:
        return ToolResult.error(
            ErrorCode.INVALID_ARGS,
            "pyperclip not installed.",
            hint="Call install_package with name='pyperclip' and a reason.",
            retryable=False,
        )
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"clipboard_write failed: {e}")


# ───────────────────────────────────────────────────────────────────────
# system_info
# ───────────────────────────────────────────────────────────────────────


def _system_info(args: Dict[str, Any]) -> ToolResult:
    include_env_values = bool(args.get("include_env_values"))
    info: Dict[str, Any] = {}
    info["os"] = {
        "name": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "hostname": socket.gethostname(),
    }
    info["python"] = {
        "version": sys.version.split()[0],
        "executable": sys.executable,
        "platform": sys.platform,
    }
    try:
        import psutil  # type: ignore
        info["cpu"] = {
            "logical_cores": psutil.cpu_count(logical=True),
            "physical_cores": psutil.cpu_count(logical=False),
            "load_pct": psutil.cpu_percent(interval=0.2),
        }
        vm = psutil.virtual_memory()
        info["memory"] = {
            "total_gb": round(vm.total / 1024**3, 2),
            "available_gb": round(vm.available / 1024**3, 2),
            "used_pct": vm.percent,
        }
        info["disks"] = []
        for p in psutil.disk_partitions(all=False):
            try:
                u = psutil.disk_usage(p.mountpoint)
                info["disks"].append({
                    "mount": p.mountpoint,
                    "fstype": p.fstype,
                    "total_gb": round(u.total / 1024**3, 2),
                    "free_gb": round(u.free / 1024**3, 2),
                    "used_pct": u.percent,
                })
            except Exception:
                continue
        info["network"] = []
        for ifname, addrs in psutil.net_if_addrs().items():
            ips = [a.address for a in addrs if a.family in (socket.AF_INET, socket.AF_INET6)]
            if ips:
                info["network"].append({"interface": ifname, "addrs": ips})
    except Exception as e:  # noqa: BLE001
        info["psutil_error"] = str(e)

    # GPU (best-effort — only succeeds if nvidia-smi is on PATH).
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4,
        )
        if out.returncode == 0 and out.stdout.strip():
            gpus = []
            for line in out.stdout.splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 4:
                    gpus.append({
                        "name": parts[0],
                        "mem_total_mb": int(parts[1]),
                        "mem_used_mb": int(parts[2]),
                        "util_pct": int(parts[3]),
                    })
            info["gpus"] = gpus
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    except Exception:
        pass

    # Env vars — just names by default, values only if explicitly requested.
    env_keys = sorted(os.environ.keys())
    if include_env_values:
        info["env"] = {k: os.environ.get(k) for k in env_keys}
    else:
        info["env_keys"] = env_keys

    body = json.dumps(info, indent=2, default=str)
    return ToolResult.success(body, summary="system_info collected")


# ───────────────────────────────────────────────────────────────────────
# open_with_default_app
# ───────────────────────────────────────────────────────────────────────


def _open_with_default_app(args: Dict[str, Any]) -> ToolResult:
    path = (args.get("path") or "").strip()
    if not path:
        return ToolResult.error(ErrorCode.INVALID_ARGS, "open_with_default_app requires 'path'.", retryable=False)
    p = Path(path)
    if not p.exists():
        return ToolResult.error(ErrorCode.FILE_NOT_FOUND, f"Path not found: {path}")

    decision = request_permission("open_with_default_app", str(p), {"path": str(p)})
    if not decision["allowed"]:
        return ToolResult.error(
            ErrorCode.PERMISSION_DENIED,
            f"User declined open_with_default_app ({decision['reason']}).",
            retryable=False,
        )

    try:
        if os.name == "nt":
            os.startfile(str(p))   # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
        return ToolResult.success(f"OK: opened {p} in default app.", path=str(p.resolve()))
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"open_with_default_app failed: {e}")


# ───────────────────────────────────────────────────────────────────────
# list_windows
# ───────────────────────────────────────────────────────────────────────


def _list_windows(args: Dict[str, Any]) -> ToolResult:
    title_filter = (args.get("title_contains") or "").lower()
    decision = request_permission(
        "list_windows", "(window enumeration)",
        {"note": "lists window titles — may reveal what apps you have open"},
    )
    if not decision["allowed"]:
        return ToolResult.error(
            ErrorCode.PERMISSION_DENIED,
            f"User declined list_windows ({decision['reason']}).",
            retryable=False,
        )

    if os.name != "nt":
        return ToolResult.error(
            ErrorCode.INVALID_ARGS,
            "list_windows currently only supports Windows.",
            retryable=False,
        )
    try:
        import win32gui  # type: ignore
        import win32process  # type: ignore
    except ImportError:
        return ToolResult.error(
            ErrorCode.INVALID_ARGS,
            "pywin32 not installed.",
            hint="Call install_package with name='pywin32' and a reason.",
            retryable=False,
        )

    rows: List[Dict[str, Any]] = []
    foreground = None
    try:
        foreground = win32gui.GetForegroundWindow()
    except Exception:
        pass

    def _enum_handler(hwnd: int, _arg: Any) -> None:
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd)
            if not title:
                return
            if title_filter and title_filter not in title.lower():
                return
            try:
                _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                pid = 0
            rows.append({
                "hwnd": hwnd,
                "title": title[:120],
                "pid": pid,
                "focused": hwnd == foreground,
            })
        except Exception:
            return

    try:
        win32gui.EnumWindows(_enum_handler, None)
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"list_windows EnumWindows failed: {e}")

    rows.sort(key=lambda r: (not r["focused"], r["title"]))
    rows = rows[:60]
    lines = [f"{'HWND':>9}  {'PID':>7}  FOC  TITLE", "-" * 60]
    for r in rows:
        focus_mark = " *" if r["focused"] else "  "
        lines.append(f"{r['hwnd']:>9}  {r['pid']:>7}  {focus_mark}  {r['title']}")
    body = "\n".join(lines) if rows else "(no windows matched)"
    return ToolResult.success(body, count=len(rows))


# ───────────────────────────────────────────────────────────────────────
# watch_file
# ───────────────────────────────────────────────────────────────────────


def _watch_file(args: Dict[str, Any]) -> ToolResult:
    path = (args.get("path") or "").strip()
    timeout = float(args.get("timeout") or 30.0)
    if not path:
        return ToolResult.error(ErrorCode.INVALID_ARGS, "watch_file requires 'path'.", retryable=False)
    if timeout > 300:
        timeout = 300
    p = Path(path)
    if not p.exists():
        return ToolResult.error(ErrorCode.FILE_NOT_FOUND, f"Path not found: {path}")

    initial: Dict[str, float] = {}
    if p.is_file():
        initial[str(p)] = p.stat().st_mtime
    else:
        for f in p.rglob("*"):
            if f.is_file():
                try:
                    initial[str(f)] = f.stat().st_mtime
                except OSError:
                    continue

    deadline = time.time() + timeout
    poll_interval = 0.5
    while time.time() < deadline:
        time.sleep(poll_interval)
        # Recompute mtimes
        if p.is_file():
            try:
                cur_m = p.stat().st_mtime
            except OSError:
                return ToolResult.success(f"watch_file: {p} was DELETED", path=str(p), change="deleted")
            if cur_m != initial.get(str(p)):
                return ToolResult.success(f"watch_file: {p} was MODIFIED", path=str(p), change="modified")
        else:
            # Directory — check new/deleted/modified files
            cur: Dict[str, float] = {}
            for f in p.rglob("*"):
                if f.is_file():
                    try:
                        cur[str(f)] = f.stat().st_mtime
                    except OSError:
                        continue
            added = set(cur) - set(initial)
            removed = set(initial) - set(cur)
            modified = [k for k in (set(cur) & set(initial)) if cur[k] != initial[k]]
            if added or removed or modified:
                lines = [f"watch_file changes in {p}:"]
                if added: lines.append(f"  + added ({len(added)}): " + ", ".join(sorted(added)[:5]))
                if removed: lines.append(f"  - removed ({len(removed)}): " + ", ".join(sorted(removed)[:5]))
                if modified: lines.append(f"  ~ modified ({len(modified)}): " + ", ".join(sorted(modified)[:5]))
                return ToolResult.success("\n".join(lines), path=str(p),
                                          added=len(added), removed=len(removed), modified=len(modified))
    return ToolResult.success(f"watch_file: no changes within {timeout}s", path=str(p), change="timeout")


# ───────────────────────────────────────────────────────────────────────
# find_in_files
# ───────────────────────────────────────────────────────────────────────


def _find_in_files(args: Dict[str, Any]) -> ToolResult:
    pattern = (args.get("pattern") or "").strip()
    root = (args.get("path") or ".").strip()
    glob = (args.get("glob") or "*").strip()
    max_results = int(args.get("max_results") or 80)
    if not pattern:
        return ToolResult.error(ErrorCode.INVALID_ARGS, "find_in_files requires 'pattern' (regex).", retryable=False)
    root_p = Path(root)
    if not root_p.exists():
        return ToolResult.error(ErrorCode.FILE_NOT_FOUND, f"Root path not found: {root}")
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return ToolResult.error(ErrorCode.INVALID_ARGS, f"Invalid regex {pattern!r}: {e}", retryable=False)

    # Skip well-known noise dirs to keep this snappy
    SKIP_DIRS = {".git", "__pycache__", "node_modules", "venv", ".venv", "dist", "build", ".pytest_cache"}

    hits: List[str] = []
    files_searched = 0
    for f in root_p.rglob("*"):
        if not f.is_file():
            continue
        # Skip noise dirs anywhere in the path.
        if any(part in SKIP_DIRS for part in f.parts):
            continue
        if glob != "*" and not fnmatch(f.name, glob):
            continue
        # Skip non-text by size + extension heuristic
        try:
            if f.stat().st_size > 2_000_000:
                continue
        except OSError:
            continue
        try:
            with f.open("r", encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh, 1):
                    if rx.search(line):
                        hits.append(f"{f}:{i}: {line.rstrip()[:200]}")
                        if len(hits) >= max_results:
                            return ToolResult.success(
                                f"find_in_files: {len(hits)} matches (capped at {max_results}) in {files_searched + 1} files\n\n"
                                + "\n".join(hits),
                                count=len(hits), capped=True,
                            )
        except OSError:
            continue
        files_searched += 1

    if not hits:
        return ToolResult.success(
            f"find_in_files: no matches for {pattern!r} in {files_searched} files under {root_p}",
            count=0, files_searched=files_searched,
        )
    return ToolResult.success(
        f"find_in_files: {len(hits)} matches in {files_searched} files\n\n" + "\n".join(hits),
        count=len(hits), files_searched=files_searched,
    )


# ───────────────────────────────────────────────────────────────────────
# read_env
# ───────────────────────────────────────────────────────────────────────


def _read_env(args: Dict[str, Any]) -> ToolResult:
    name = (args.get("name") or "").strip()
    if not name:
        return ToolResult.error(ErrorCode.INVALID_ARGS, "read_env requires 'name'.", retryable=False)
    if name not in os.environ:
        return ToolResult.error(
            ErrorCode.FILE_NOT_FOUND,
            f"Env var {name!r} is not set.",
            retryable=False,
        )
    val = os.environ[name]
    masked = val if len(val) < 8 else f"{val[:3]}…{val[-3:]} (len={len(val)})"
    decision = request_permission(
        "read_env", name,
        {"name": name, "preview_masked": masked, "note": "env vars may contain secrets"},
    )
    if not decision["allowed"]:
        return ToolResult.error(
            ErrorCode.PERMISSION_DENIED,
            f"User declined read_env for {name} ({decision['reason']}).",
            retryable=False,
        )
    return ToolResult.success(val, name=name, length=len(val))


# ───────────────────────────────────────────────────────────────────────
# Register
# ───────────────────────────────────────────────────────────────────────


def register(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="screenshot",
        description=(
            "Capture the screen → save as PNG. Asks user permission. "
            "Optional `region` as 'x,y,w,h' for a sub-region; defaults to full screen. "
            "Requires Pillow."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path":   {"type": "string", "description": "Output PNG path"},
                "region": {"type": "string", "description": "Optional 'x,y,w,h'"},
            },
            "required": ["path"],
        },
        handler=_screenshot,
        requires_permission=True,
        category="system",
    ))
    registry.register(Tool(
        name="clipboard_read",
        description="Read text from the system clipboard. Asks permission (may contain secrets).",
        input_schema={"type": "object", "properties": {}},
        handler=_clipboard_read,
        requires_permission=True,
        category="system",
    ))
    registry.register(Tool(
        name="clipboard_write",
        description="Write text to the system clipboard. Asks permission.",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        handler=_clipboard_write,
        requires_permission=True,
        category="system",
    ))
    registry.register(Tool(
        name="system_info",
        description=(
            "Collect CPU/RAM/GPU/disks/network/OS/Python info. Env-var NAMES only "
            "unless `include_env_values=true`. Read-only; no permission prompt."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "include_env_values": {"type": "boolean", "description": "Set true to include env-var values (sensitive!)"},
            },
        },
        handler=_system_info,
        category="system",
    ))
    registry.register(Tool(
        name="open_with_default_app",
        description=(
            "Open a file/folder in its OS default application "
            "(os.startfile on Windows, 'open' on macOS, 'xdg-open' on Linux). Asks permission."
        ),
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        handler=_open_with_default_app,
        requires_permission=True,
        category="system",
    ))
    registry.register(Tool(
        name="list_windows",
        description=(
            "List top-level Windows windows (title, pid, focused). "
            "Windows only — uses pywin32. Asks permission."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title_contains": {"type": "string", "description": "Optional substring filter (case-insensitive)"},
            },
        },
        handler=_list_windows,
        requires_permission=True,
        category="system",
    ))
    registry.register(Tool(
        name="watch_file",
        description=(
            "Block until a file or directory changes. Returns the change kind. "
            "Timeout default 30s, max 300s."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path":    {"type": "string"},
                "timeout": {"type": "number", "description": "Seconds (default 30, max 300)"},
            },
            "required": ["path"],
        },
        handler=_watch_file,
        category="system",
    ))
    registry.register(Tool(
        name="find_in_files",
        description=(
            "Recursive regex search across a folder. Skips noise dirs "
            "(.git/__pycache__/node_modules/venv). Caps at max_results matches."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern":     {"type": "string", "description": "Python regex (case-insensitive)"},
                "path":        {"type": "string", "description": "Root dir (default '.')"},
                "glob":        {"type": "string", "description": "Filename glob (default '*')"},
                "max_results": {"type": "integer", "description": "Default 80"},
            },
            "required": ["pattern"],
        },
        handler=_find_in_files,
        category="system",
    ))
    registry.register(Tool(
        name="read_env",
        description=(
            "Read a specific environment variable. Asks permission (env vars may "
            "contain secrets). Returns the value."
        ),
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        handler=_read_env,
        requires_permission=True,
        category="system",
    ))
