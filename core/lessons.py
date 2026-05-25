"""LessonsStore — persistent user corrections for self-learning.

Inspired by the claude-mem progressive-disclosure pattern. The store
captures corrections the user makes ("no, actually X") and explicit
remembrances ("always answer in Vietnamese") and feeds them back as a
`<lessons_learned>` block in T1 of every system prompt.

Storage:
    data/lessons.jsonl  — append-only, one JSON object per line.
    Schema: { ts, scope, trigger, lesson, source_session }
    - ts: float, time.time() at capture
    - scope: "global" (apply to every conversation) | "session:<sid>"
    - trigger: the assistant message text that prompted the correction (≤200 chars)
    - lesson: the correction itself (≤400 chars)
    - source_session: session_id where the lesson was captured

Hard cap: 200 most recent entries. On overflow, the oldest are dropped.

Usage:
    store = get_lessons_store()
    store.record(scope="global", lesson="user prefers Vietnamese", source_session=sid)
    block = store.render_for_prompt()   # the T1 sub-block
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional


MAX_ENTRIES = 200
DEFAULT_PATH = Path("data") / "lessons.jsonl"


@dataclass
class Lesson:
    ts: float
    scope: str          # "global" or "session:<sid>"
    lesson: str
    trigger: str = ""
    source_session: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class LessonsStore:
    """Append-only JSONL-backed lessons store. Thread-safe."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or DEFAULT_PATH
        self._lock = threading.Lock()
        self._entries: List[Lesson] = []
        self._loaded = False

    # ── persistence ──────────────────────────────────────────────────
    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._path.exists():
            return
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self._entries.append(Lesson(
                        ts=float(d.get("ts") or 0),
                        scope=str(d.get("scope") or "global"),
                        lesson=str(d.get("lesson") or "")[:400],
                        trigger=str(d.get("trigger") or "")[:200],
                        source_session=str(d.get("source_session") or ""),
                    ))
        except OSError:
            return
        # Keep only the most-recent MAX_ENTRIES.
        if len(self._entries) > MAX_ENTRIES:
            self._entries = self._entries[-MAX_ENTRIES:]

    def _rewrite(self) -> None:
        """Rewrite the JSONL file from the in-memory list (called after trim)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                for e in self._entries:
                    fh.write(json.dumps(e.to_dict(), ensure_ascii=False) + "\n")
            tmp.replace(self._path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    def _append(self, entry: Lesson) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ── public API ───────────────────────────────────────────────────
    def record(
        self,
        *,
        lesson: str,
        scope: str = "global",
        trigger: str = "",
        source_session: str = "",
    ) -> Optional[Lesson]:
        """Persist a lesson. Returns the entry (or None if the lesson was empty)."""
        lesson = (lesson or "").strip()
        if not lesson:
            return None
        with self._lock:
            self._load()
            entry = Lesson(
                ts=time.time(),
                scope=scope or "global",
                lesson=lesson[:400],
                trigger=(trigger or "")[:200],
                source_session=source_session or "",
            )
            self._entries.append(entry)
            self._append(entry)
            if len(self._entries) > MAX_ENTRIES:
                # Trim from the head, then rewrite the file so it stays bounded.
                self._entries = self._entries[-MAX_ENTRIES:]
                self._rewrite()
            return entry

    def list(
        self,
        *,
        scope: Optional[str] = None,
        session_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Lesson]:
        """Most-recent-first list of lessons that apply to the given session.

        - `scope=None`: include `global` AND `session:<session_id>`.
        - `scope="global"`: only global lessons.
        - `scope="session"`: only the matching `session:<sid>` lessons.
        """
        with self._lock:
            self._load()
            out: List[Lesson] = []
            for e in reversed(self._entries):
                if scope == "global" and e.scope != "global":
                    continue
                if scope == "session":
                    if not session_id or e.scope != f"session:{session_id}":
                        continue
                if scope is None:
                    keep = e.scope == "global" or (
                        session_id is not None and e.scope == f"session:{session_id}"
                    )
                    if not keep:
                        continue
                out.append(e)
                if len(out) >= limit:
                    break
            return out

    def render_for_prompt(
        self,
        *,
        session_id: Optional[str] = None,
        limit: int = 12,
        max_chars: int = 1500,
    ) -> str:
        """Return a `<lessons_learned>` block for T1 injection.

        Empty string if there are no applicable lessons — caller skips the block.
        Bounded by `limit` entries and `max_chars` total so it doesn't blow up T1.
        """
        items = self.list(session_id=session_id, limit=limit)
        if not items:
            return ""
        lines = ["<lessons_learned>"]
        total = len(lines[0])
        for e in items:
            line = f"  - {e.lesson}"
            if total + len(line) > max_chars:
                lines.append("  …(more lessons available, truncated)")
                break
            lines.append(line)
            total += len(line)
        lines.append("</lessons_learned>")
        return "\n".join(lines)

    def all(self) -> List[Lesson]:
        with self._lock:
            self._load()
            return list(self._entries)

    def clear(self) -> int:
        """Wipe the store. Returns number of entries removed."""
        with self._lock:
            self._load()
            n = len(self._entries)
            self._entries = []
            try:
                self._path.unlink(missing_ok=True)
            except OSError:
                pass
            return n


# ───────────────────────────────────────────────────────────────────────
# Module-level singleton
# ───────────────────────────────────────────────────────────────────────

_store: Optional[LessonsStore] = None
_lock = threading.Lock()


def get_lessons_store() -> LessonsStore:
    global _store
    if _store is None:
        with _lock:
            if _store is None:
                _store = LessonsStore()
    return _store


def reset_lessons_store_for_tests(path: Optional[Path] = None) -> LessonsStore:
    """Test helper — swap in a fresh store, optionally with a custom path."""
    global _store
    with _lock:
        _store = LessonsStore(path=path)
    return _store


__all__ = [
    "Lesson",
    "LessonsStore",
    "get_lessons_store",
    "reset_lessons_store_for_tests",
]
