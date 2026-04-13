"""
Interactive permission gate for risky local tools (write_file, run_command).

Flow:
  1. Tool call arrives -> request_permission() creates a pending entry and BLOCKS
     the agent thread on a threading.Event, up to TIMEOUT_SEC.
  2. UI polls GET /api/permissions/pending and shows a modal.
  3. User clicks Allow/Deny -> POST /api/permissions/resolve sets the event.
  4. Tool thread unblocks, returns True/False.

Session policies:
  - "once": approve this one call
  - "session": approve all future calls for the same (tool, path-or-command) key
  - "always-this-tool": blanket approve the tool name for this session
  - "deny": refuse
"""
import threading
import time
import uuid
from typing import Dict, Any, Optional, List

TIMEOUT_SEC = 120
_LOCK = threading.Lock()
_PENDING: Dict[str, Dict[str, Any]] = {}     # id -> entry
_EVENTS: Dict[str, threading.Event] = {}
_SESSION_ALLOW: Dict[str, bool] = {}         # key "tool:subject" or "tool:*"


def _session_key(tool: str, subject: str) -> str:
    return f"{tool}:{subject}"


def request_permission(tool: str, subject: str, details: Dict[str, Any]) -> Dict[str, Any]:
    """Block until user decides (or timeout). Returns {allowed: bool, reason: str}."""
    # Check session-wide pre-approvals first
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
            {"id": e["id"], "tool": e["tool"], "subject": e["subject"],
             "details": e["details"], "age_sec": int(time.time() - e["created_at"])}
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


def clear_session_allows():
    with _LOCK:
        _SESSION_ALLOW.clear()
