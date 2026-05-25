"""Structured subsystem logging.

Borrowed from `openclaw/src/logging/subsystem.ts`: every module asks for
a logger by *subsystem name*, gets back a `logging.Logger` configured
with three sinks at once:

  1. **Rich console handler** — colored, single-line with subsystem
     tag. Replaces the ad-hoc `print(...)` calls peppered around the
     codebase. Lets a developer eyeballing the terminal see at a glance
     which subsystem emitted what.

  2. **JSONL file handler** — `proxy.log.jsonl` next to `proxy.py`.
     One event per line, fields: ts, subsystem, level, msg, session_id,
     turn, tool, extra. `jq` -friendly. Survives process restart.

  3. **In-memory ring buffer** (cap 1000) — feeds `/api/logs/tail` so
     the UI Logs tab can show live entries without reading the file.

Usage:

    from core.logs import get_logger
    log = get_logger("agent")
    log.info("starting turn", extra={"session_id": sid, "turn": n})

All loggers share a single root configuration that's idempotent —
calling `get_logger(...)` from multiple modules at import time is safe.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

# These are the subsystem names we expect — the UI filter dropdown lists
# them. Adding a new subsystem at runtime works fine; it just won't show
# up in the dropdown until you add it here.
SUBSYSTEMS: List[str] = [
    "proxy", "agent", "tools", "rag", "provider",
    "memory", "context", "permissions", "scheduler", "discord",
    "healthcheck",
]

DEFAULT_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
DEFAULT_JSONL_PATH: Optional[Path] = None  # set by configure()
RING_CAPACITY = 1000


# ───────────────────────────────────────────────────────────────────────
# Ring buffer (lock-free reads, brief lock on writes)
# ───────────────────────────────────────────────────────────────────────


class _RingBuffer:
    def __init__(self, capacity: int = RING_CAPACITY) -> None:
        self._buf: Deque[Dict[str, Any]] = collections.deque(maxlen=capacity)
        self._lock = threading.Lock()

    def append(self, event: Dict[str, Any]) -> None:
        with self._lock:
            self._buf.append(event)

    def tail(
        self,
        *,
        subsystem: Optional[str] = None,
        since_ts: Optional[float] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            snapshot = list(self._buf)
        if subsystem:
            snapshot = [e for e in snapshot if e.get("subsystem") == subsystem]
        if since_ts is not None:
            snapshot = [e for e in snapshot if e.get("ts", 0) > since_ts]
        return snapshot[-limit:]


_ring = _RingBuffer()


def ring() -> _RingBuffer:
    """Singleton ring buffer — used by the proxy's /api/logs/tail."""
    return _ring


# ───────────────────────────────────────────────────────────────────────
# Handlers
# ───────────────────────────────────────────────────────────────────────


class _RingHandler(logging.Handler):
    """Pushes formatted events into the in-memory ring buffer."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            event = {
                "ts": record.created,
                "subsystem": getattr(record, "subsystem", record.name),
                "level": record.levelname,
                "msg": record.getMessage(),
                "session_id": getattr(record, "session_id", None),
                "turn": getattr(record, "turn", None),
                "tool": getattr(record, "tool", None),
            }
            _ring.append(event)
        except Exception:  # noqa: BLE001
            # Never let the ring buffer break logging — drop the event.
            pass


class _JsonlHandler(logging.Handler):
    """Append-only JSON-line file handler. Best-effort; never blocks the
    request thread for long."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = json.dumps({
                "ts": record.created,
                "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
                "subsystem": getattr(record, "subsystem", record.name),
                "level": record.levelname,
                "msg": record.getMessage(),
                "session_id": getattr(record, "session_id", None),
                "turn": getattr(record, "turn", None),
                "tool": getattr(record, "tool", None),
            }, ensure_ascii=False)
            with self._lock:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception:  # noqa: BLE001
            pass


def _try_rich_handler() -> Optional[logging.Handler]:
    """Build a rich.logging.RichHandler if rich is available; else None.

    Falls back gracefully so the project boots in environments without
    rich installed (CI checks, packagers).
    """
    try:
        from rich.logging import RichHandler
        from rich.console import Console
        # show_path=False keeps lines short — subsystem already identifies origin.
        return RichHandler(
            console=Console(stderr=False),
            show_time=True,
            show_path=False,
            show_level=True,
            markup=False,
            rich_tracebacks=True,
            keywords=[],
        )
    except Exception:  # noqa: BLE001
        return None


# ───────────────────────────────────────────────────────────────────────
# Configuration (idempotent)
# ───────────────────────────────────────────────────────────────────────


_CONFIGURED = False
_CONFIG_LOCK = threading.Lock()


class _SubsystemFilter(logging.Filter):
    """Stamps `subsystem` onto every record so handlers can read it."""

    def __init__(self, subsystem: str) -> None:
        super().__init__()
        self._subsystem = subsystem

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "subsystem"):
            record.subsystem = self._subsystem
        return True


def configure(
    *,
    level: Optional[str] = None,
    jsonl_path: Optional[Path] = None,
    force: bool = False,
) -> None:
    """Set up the root + subsystem handlers. Called once at startup.

    Safe to call multiple times — only the first call (or `force=True`)
    installs handlers. Subsequent calls just adjust level.
    """
    global _CONFIGURED, DEFAULT_JSONL_PATH

    with _CONFIG_LOCK:
        # Always honour level changes.
        lvl = (level or DEFAULT_LEVEL).upper()
        logging.getLogger().setLevel(lvl)
        for sub in SUBSYSTEMS:
            logging.getLogger(f"localhelpbot.{sub}").setLevel(lvl)

        if _CONFIGURED and not force:
            return

        DEFAULT_JSONL_PATH = jsonl_path or (Path(__file__).parent.parent / "proxy.log.jsonl")

        # Clear pre-existing handlers if forcing (tests).
        root = logging.getLogger()
        if force:
            for h in list(root.handlers):
                root.removeHandler(h)

        # Console (rich if available, else stderr StreamHandler).
        console_handler = _try_rich_handler()
        if console_handler is None:
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(logging.Formatter(
                "%(asctime)s  %(levelname)-5s  [%(name)s]  %(message)s",
                datefmt="%H:%M:%S",
            ))
        # Force the formatter even on rich; we only show subsystem + msg.
        if not isinstance(console_handler, logging.StreamHandler) or hasattr(console_handler, "console"):
            try:
                console_handler.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
            except Exception:  # noqa: BLE001
                pass

        root.addHandler(console_handler)

        # JSONL file.
        try:
            jsonl_handler = _JsonlHandler(DEFAULT_JSONL_PATH)
            root.addHandler(jsonl_handler)
        except Exception:  # noqa: BLE001
            pass

        # In-memory ring (for the UI Logs tab).
        root.addHandler(_RingHandler())

        _CONFIGURED = True


# ───────────────────────────────────────────────────────────────────────
# Public API
# ───────────────────────────────────────────────────────────────────────


def get_logger(subsystem: str) -> logging.Logger:
    """Return a logger named `localhelpbot.<subsystem>`.

    The first call lazily configures the global handler chain.
    Adds a `_SubsystemFilter` so every record carries `subsystem` info.
    """
    if not _CONFIGURED:
        configure()
    logger = logging.getLogger(f"localhelpbot.{subsystem}")
    # Avoid double-adding the same filter on every call.
    if not any(isinstance(f, _SubsystemFilter) for f in logger.filters):
        logger.addFilter(_SubsystemFilter(subsystem))
    logger.propagate = True
    return logger


def tail_events(
    *,
    subsystem: Optional[str] = None,
    since_ts: Optional[float] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Public read against the in-memory ring buffer. Used by /api/logs/tail."""
    return _ring.tail(subsystem=subsystem, since_ts=since_ts, limit=limit)


__all__ = [
    "configure",
    "get_logger",
    "ring",
    "tail_events",
    "SUBSYSTEMS",
]
