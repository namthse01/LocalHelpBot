"""Session-keyed conversation store.

The Ollama wire protocol is stateless: every `/api/chat` call ships the
whole message list from the client. That means we historically *had no
server-side identity* for a conversation — every request rebuilt from
scratch and any state we wanted to remember (the user's goal, files
touched this session, sticky decisions) had to be folded back into the
prompt by the client. The client doesn't always cooperate.

This module gives the proxy a real session identity:

    sid = derive_session_id(messages, hint=request_header)
    store = get_store()
    sess = store.get_or_create(sid)
    sess.note_user_goal(...)
    sess.record_files_touched([...])

Session-id derivation prefers, in order:
  1. An explicit `X-Session-Id` HTTP header (UI / Discord adapter)
  2. A stable hash of (first user message + today's date)

The second branch means "the same logical thread" stays on one session
for the whole day even if the client never tells us who it is, while two
different users typing "hello" don't collide because their first user
messages diverge after turn 1.

Storage is in-memory by default; calling `enable_jsonl_persistence()`
writes append-only JSONL files to `./data/sessions/<sid>.jsonl` so a
crash mid-task doesn't lose the user's stated goal.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ───────────────────────────────────────────────────────────────────────
# Session record
# ───────────────────────────────────────────────────────────────────────


@dataclass
class Session:
    """Server-side state for one logical conversation.

    Held in memory and (optionally) mirrored to a JSONL append log.
    None of these fields are required for the bot to *answer* a turn —
    they exist so follow-up turns can be smarter. The context engine
    (core.context) reads them to populate Tier 2 (Session) of the
    assembled system prompt.
    """

    sid: str
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    # User-stated goal (first user message, kept verbatim up to 600 chars).
    goal: str = ""

    # Compaction summary, owned by core.memory.MemoryEngine.
    summary: str = ""

    # Files written / edited this session, deduped, newest last.
    files_touched: List[str] = field(default_factory=list)

    # Sticky decisions ("output dir = ./out", "user prefers Vietnamese").
    sticky: Dict[str, str] = field(default_factory=dict)

    # Source channel — "ui", "discord:<guild>:<channel>", "mcp", "api".
    source: str = "ui"

    # Profile (agent name) most recently used. Helps the orchestrator pick
    # a sensible default when the request doesn't name a virtual model.
    last_profile: Optional[str] = None

    # Turn counter — incremented by record_turn().
    turn_count: int = 0

    # ── mutators ────────────────────────────────────────────────────
    def note_user_goal(self, msg: str) -> None:
        if self.goal:
            return  # locked after first turn
        cleaned = (msg or "").strip()
        if cleaned:
            self.goal = cleaned[:600]

    def record_files_touched(self, paths: List[str]) -> None:
        for p in paths:
            if not p:
                continue
            if p in self.files_touched:
                self.files_touched.remove(p)
            self.files_touched.append(p)
        # Cap to last 50 to keep the prompt bounded.
        if len(self.files_touched) > 50:
            self.files_touched = self.files_touched[-50:]

    def set_sticky(self, key: str, value: str) -> None:
        if not key:
            return
        self.sticky[key] = value

    def record_turn(self, profile: Optional[str] = None) -> None:
        self.last_seen = time.time()
        self.turn_count += 1
        if profile:
            self.last_profile = profile

    def update_summary(self, summary: str) -> None:
        self.summary = (summary or "").strip()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ───────────────────────────────────────────────────────────────────────
# Store
# ───────────────────────────────────────────────────────────────────────


class ConversationStore:
    """Thread-safe singleton-style session registry.

    Use `get_store()` to retrieve the process-wide instance; call
    `enable_jsonl_persistence(dir)` once at proxy startup to mirror
    every mutation to disk.
    """

    def __init__(self) -> None:
        self._sessions: Dict[str, Session] = {}
        self._lock = threading.Lock()
        self._jsonl_dir: Optional[Path] = None

    def enable_jsonl_persistence(self, base_dir: Path) -> None:
        base_dir.mkdir(parents=True, exist_ok=True)
        self._jsonl_dir = base_dir
        # Eagerly rehydrate so a restarted proxy still recognises in-flight
        # conversations from earlier in the day.
        for path in base_dir.glob("*.jsonl"):
            sid = path.stem
            sess = self._replay(path, sid)
            if sess is not None:
                self._sessions[sid] = sess

    def get_or_create(self, sid: str, source: str = "ui") -> Session:
        with self._lock:
            sess = self._sessions.get(sid)
            if sess is None:
                sess = Session(sid=sid, source=source)
                self._sessions[sid] = sess
                self._append_event(sess, {"event": "create", "source": source})
            return sess

    def get(self, sid: str) -> Optional[Session]:
        with self._lock:
            return self._sessions.get(sid)

    def note(self, sid: str, **changes: Any) -> None:
        """Apply changes and persist a single event line.

        Supported keys: goal, summary, files_touched (append list),
        sticky (dict merge), profile, source.
        """
        with self._lock:
            sess = self._sessions.get(sid)
            if sess is None:
                return
            event: Dict[str, Any] = {"event": "note"}
            if "goal" in changes:
                sess.note_user_goal(changes["goal"])
                event["goal"] = sess.goal
            if "summary" in changes:
                sess.update_summary(changes["summary"])
                event["summary"] = sess.summary[:120] + "…" if len(sess.summary) > 120 else sess.summary
            if "files_touched" in changes:
                sess.record_files_touched(list(changes["files_touched"]))
                event["files_touched"] = changes["files_touched"]
            if "sticky" in changes:
                for k, v in (changes["sticky"] or {}).items():
                    sess.set_sticky(k, v)
                event["sticky"] = changes["sticky"]
            if "profile" in changes:
                sess.record_turn(changes["profile"])
                event["profile"] = changes["profile"]
            self._append_event(sess, event)

    def all_sessions(self) -> List[Session]:
        with self._lock:
            return list(self._sessions.values())

    def reset(self, sid: str) -> None:
        with self._lock:
            self._sessions.pop(sid, None)
            if self._jsonl_dir is not None:
                p = self._jsonl_dir / f"{sid}.jsonl"
                if p.exists():
                    p.unlink()

    # ── internals ────────────────────────────────────────────────────
    def _append_event(self, sess: Session, event: Dict[str, Any]) -> None:
        if self._jsonl_dir is None:
            return
        event = dict(event)
        event["ts"] = time.time()
        path = self._jsonl_dir / f"{sess.sid}.jsonl"
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            # Persistence is best-effort — never block a request on disk IO.
            pass

    def _replay(self, path: Path, sid: str) -> Optional[Session]:
        try:
            sess = Session(sid=sid)
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("source"):
                        sess.source = ev["source"]
                    if "goal" in ev and not sess.goal:
                        sess.note_user_goal(ev["goal"])
                    if "summary" in ev:
                        sess.update_summary(ev["summary"])
                    if "files_touched" in ev:
                        sess.record_files_touched(list(ev["files_touched"]))
                    if "sticky" in ev:
                        for k, v in (ev["sticky"] or {}).items():
                            sess.set_sticky(k, v)
                    if "profile" in ev:
                        sess.record_turn(ev["profile"])
            return sess
        except OSError:
            return None


# ───────────────────────────────────────────────────────────────────────
# Module-level singleton + session-id derivation
# ───────────────────────────────────────────────────────────────────────

_store: Optional[ConversationStore] = None
_store_lock = threading.Lock()


def get_store() -> ConversationStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = ConversationStore()
    return _store


def _first_user_text(messages: List[Dict[str, Any]]) -> str:
    for m in messages or []:
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            text = " ".join(
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        else:
            text = str(content or "")
        text = text.strip()
        if text:
            return text
    return ""


def derive_session_id(
    messages: List[Dict[str, Any]],
    *,
    explicit: Optional[str] = None,
    day_salt: Optional[str] = None,
) -> str:
    """Return a stable session id for this conversation.

    Order of preference:
      1. `explicit` — if the caller (UI / Discord adapter / MCP) passes a
         pre-derived id, trust it.
      2. Hash of (first_user_text + today's UTC date). Empty conversations
         get a fresh UUID-style id so we don't lump all empty calls into
         one session.
    """
    if explicit:
        explicit = explicit.strip()
        if explicit:
            return explicit[:64]

    first = _first_user_text(messages)
    if not first:
        # No content yet — synthesize a one-off id. Clients that re-issue
        # the same empty body get the same id thanks to the hash; the day
        # salt keeps these short-lived.
        first = "(empty)"

    salt = day_salt or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    digest = hashlib.sha1(f"{first}\n{salt}".encode("utf-8")).hexdigest()
    return digest[:16]


def derive_discord_session_id(
    guild_id: Any, channel_id: Any, user_id: Any
) -> str:
    """Stable id for a Discord conversation thread.

    Discord adapter calls this directly so the bot remembers per (guild,
    channel, user) tuple — matching how humans think of a thread.
    """
    raw = f"discord:{guild_id}:{channel_id}:{user_id}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    return f"d-{digest[:14]}"
