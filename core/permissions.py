"""Interactive permission gate for risky local tools.

Flow:
  1. Tool call arrives → `request_permission()` creates a pending entry
     and BLOCKS the agent thread on a `threading.Event`, up to TIMEOUT_SEC.
  2. UI polls `GET /api/permissions/pending` and shows a modal.
  3. User clicks Allow/Deny → `POST /api/permissions/resolve` sets the event.
  4. Tool thread unblocks; `request_permission()` returns
     `{"allowed": bool, "reason": str}`.

Session policies:
  - "once":              approve this single call
  - "session":           approve all future calls for (tool, subject)
  - "always-this-tool":  blanket approve the tool name for this session
  - "deny":              refuse

What's new in this rewrite (Slice 2 of the upgrade plan):
  • Each pending entry now ships a structured `preview` payload so the
    UI can render per-tool views — colored diff for edit_file, shell
    box for run_command, hostname highlight for fetch_url, etc.
  • A `risk` field (`low|medium|high`) so the UI can colour the modal
    header by severity instead of always showing the same amber.
"""
from __future__ import annotations

import difflib
import threading
import time
import urllib.parse
import uuid
from typing import Any, Dict, List, Optional

TIMEOUT_SEC = 120

_LOCK = threading.Lock()
_PENDING: Dict[str, Dict[str, Any]] = {}      # id -> entry
_EVENTS: Dict[str, threading.Event] = {}
_SESSION_ALLOW: Dict[str, bool] = {}          # "tool:subject" -> bool


# ───────────────────────────────────────────────────────────────────────
# Risk + preview builders
# ───────────────────────────────────────────────────────────────────────


_HIGH_RISK_TOOLS = {"delete_file", "kill_process", "run_command", "python_exec"}
_MEDIUM_RISK_TOOLS = {"edit_file", "move_file", "install_package", "write_pdf", "write_docx"}
_DANGEROUS_CMD_RE = (
    "rm -rf", "rmdir /s", "format ", "del /f /s",
    "shutdown", "reboot", "mkfs", "dd if=", ":(){:|:&};:",
    "curl ", "wget ", "pip uninstall", "DROP TABLE",
)


def _risk_for(tool: str, details: Dict[str, Any]) -> str:
    if tool in _HIGH_RISK_TOOLS:
        if tool == "run_command":
            cmd = (details.get("command") or "").lower()
            if any(t in cmd for t in (s.lower() for s in _DANGEROUS_CMD_RE)):
                return "high"
            return "medium"
        return "high"
    if tool in _MEDIUM_RISK_TOOLS:
        return "medium"
    return "low"


def _short(text: str, n: int = 600) -> str:
    if not text:
        return ""
    return text if len(text) <= n else text[: n - 1] + "…"


def _build_preview(tool: str, details: Dict[str, Any]) -> Dict[str, Any]:
    """Per-tool structured preview the UI knows how to render.

    Returns a dict with at least `kind` and `text` so older UI code
    that only reads `text` still works. Per-`kind` extras:

    kind="diff"     — old/new strings + unified diff hunk
    kind="command"  — command string + cwd
    kind="url"      — url + hostname
    kind="write"    — path + bytes + preview
    kind="install"  — package + reason + pip command
    kind="exec"     — python code preview + line count
    kind="delete"   — path + is_dir + size
    kind="move"     — src + dst + overwrite flag
    kind="kill"     — pid + label
    kind="generic"  — fallback
    """
    d = details or {}

    if tool == "edit_file":
        old = d.get("old_preview") or ""
        new = d.get("new_preview") or ""
        diff_lines = list(difflib.unified_diff(
            old.splitlines(), new.splitlines(),
            fromfile="(before)", tofile="(after)", lineterm="", n=1,
        ))
        return {
            "kind": "diff",
            "path": d.get("path"),
            "old": _short(old, 600),
            "new": _short(new, 600),
            "diff": "\n".join(diff_lines)[:1200],
            "replace_all": bool(d.get("replace_all")),
            "text": f"Edit {d.get('path')}: replace {len(old)} chars → {len(new)} chars",
        }

    if tool == "run_command":
        cmd = d.get("command") or ""
        cwd = d.get("cwd")
        return {
            "kind": "command",
            "command": cmd,
            "cwd": cwd,
            "text": f"Run: {cmd}" + (f" (in {cwd})" if cwd else ""),
        }

    if tool == "python_exec":
        code = d.get("code_preview") or ""
        return {
            "kind": "exec",
            "code": _short(code, 800),
            "lines": d.get("lines"),
            "timeout": d.get("timeout"),
            "text": f"Run Python ({d.get('lines')} lines, timeout {d.get('timeout')}s)",
        }

    if tool == "write_file" or tool == "write_pdf" or tool == "write_docx":
        return {
            "kind": "write",
            "path": d.get("path"),
            "bytes": d.get("bytes") or d.get("chars"),
            "preview": _short(d.get("preview") or "", 600),
            "text": f"Write {d.get('path')} ({d.get('bytes') or d.get('chars')} chars)",
        }

    if tool == "delete_file":
        return {
            "kind": "delete",
            "path": d.get("path"),
            "is_dir": bool(d.get("is_dir")),
            "size": d.get("size"),
            "text": f"Delete {'dir' if d.get('is_dir') else 'file'} {d.get('path')}",
        }

    if tool == "move_file":
        return {
            "kind": "move",
            "src": d.get("src"),
            "dst": d.get("dst"),
            "overwrite": bool(d.get("overwrite")),
            "text": f"Move {d.get('src')} → {d.get('dst')}",
        }

    if tool == "install_package":
        return {
            "kind": "install",
            "package": d.get("package"),
            "reason": d.get("reason"),
            "command": d.get("command"),
            "text": f"pip install {d.get('package')}",
        }

    if tool == "kill_process":
        return {
            "kind": "kill",
            "pid": d.get("pid"),
            "label": d.get("label") or f"pid={d.get('pid')}",
            "text": f"Terminate {d.get('label') or d.get('pid')}",
        }

    if tool == "fetch_url":
        url = d.get("url") or d.get("subject") or ""
        host = ""
        try:
            host = urllib.parse.urlparse(url).hostname or ""
        except Exception:
            host = ""
        return {"kind": "url", "url": url, "host": host, "text": f"Fetch {url}"}

    return {"kind": "generic", "text": _short(str(d), 300)}


# ───────────────────────────────────────────────────────────────────────
# Public API
# ───────────────────────────────────────────────────────────────────────


def _session_key(tool: str, subject: str) -> str:
    return f"{tool}:{subject}"


def request_permission(tool: str, subject: str, details: Dict[str, Any]) -> Dict[str, Any]:
    """Block until user decides (or timeout).

    Returns {"allowed": bool, "reason": str}.
    """
    if _SESSION_ALLOW.get(_session_key(tool, "*")):
        return {"allowed": True, "reason": "session blanket allow"}
    if _SESSION_ALLOW.get(_session_key(tool, subject)):
        return {"allowed": True, "reason": "session subject allow"}

    pid = uuid.uuid4().hex[:12]
    ev = threading.Event()
    entry = {
        "id": pid,
        "tool": tool,
        "subject": subject,
        "details": details,
        "preview": _build_preview(tool, details),
        "risk": _risk_for(tool, details),
        "created_at": time.time(),
        "status": "pending",
        "scope": None,
    }
    with _LOCK:
        _PENDING[pid] = entry
        _EVENTS[pid] = ev

    fired = ev.wait(TIMEOUT_SEC)
    with _LOCK:
        entry = _PENDING.pop(pid, entry)
        _EVENTS.pop(pid, None)

    if not fired:
        return {"allowed": False, "reason": "timeout — user did not respond"}
    return {"allowed": entry["status"] == "approved", "reason": entry.get("reason", entry["status"])}


def list_pending() -> List[Dict[str, Any]]:
    with _LOCK:
        return [
            {
                "id": e["id"],
                "tool": e["tool"],
                "subject": e["subject"],
                "details": e["details"],
                "preview": e.get("preview") or _build_preview(e["tool"], e["details"]),
                "risk": e.get("risk") or _risk_for(e["tool"], e["details"]),
                "age_sec": int(time.time() - e["created_at"]),
            }
            for e in _PENDING.values() if e["status"] == "pending"
        ]


def resolve(pid: str, approved: bool, scope: str = "once") -> bool:
    """scope: once | session | always-this-tool"""
    with _LOCK:
        entry = _PENDING.get(pid)
        ev = _EVENTS.get(pid)
        if not entry or not ev:
            return False
        entry["status"] = "approved" if approved else "denied"
        entry["scope"] = scope
        entry["reason"] = f"{entry['status']} ({scope})"
        if approved:
            if scope == "session":
                _SESSION_ALLOW[_session_key(entry["tool"], entry["subject"])] = True
            elif scope == "always-this-tool":
                _SESSION_ALLOW[_session_key(entry["tool"], "*")] = True
    ev.set()
    return True


def clear_session_allows() -> None:
    with _LOCK:
        _SESSION_ALLOW.clear()
