"""Notes — a Google-Keep-style notes + checklists store.

Ported (adapted, not copied) from odysseus ``routes/note_routes.py``. The
odysseus version is FastAPI + SQLAlchemy + multi-user auth + reminder dispatch
(email/ntfy/webhook) + LLM classification; TheAgent0 is a single-user local
tool, so this is a synchronous, stdlib-first, JSONL-backed re-implementation
that keeps the *concepts*:

  • a note is either free text (``note_type="note"``) or a checklist
    (``note_type="checklist"`` with ``items=[{text, done}]``),
  • notes can be pinned, archived, labelled, coloured and given a due date,
  • the board orders pinned-first, then by manual ``sort_order``, then recency.

odysseus "tasks" (``task_routes.py``) are *scheduled jobs* — TheAgent0 already
has those (Daily Tasks / ``config.AUTOMATION_TASKS``), so they are deliberately
NOT re-ported here. A checklist note covers the "to-do list" use-case without
colliding with the scheduler or the ``task`` sub-agent tool.

Storage mirrors :mod:`core.compare` / :mod:`core.lessons` exactly (append-only
JSONL, ``threading`` lock, a ``MAX_ENTRIES`` cap read from
``config.NOTES_MAX_ENTRIES``, a module singleton + a ``reset_*_for_tests``
helper).

Public surface:
    Note, NotesStore
    get_notes_store() / reset_notes_store_for_tests(path)
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_MAX_ENTRIES = 500
DEFAULT_PATH = Path("data") / "notes" / "notes.jsonl"

NOTE = "note"
CHECKLIST = "checklist"


def _cfg(name: str, default: Any) -> Any:
    """Read a config tunable defensively (config may be partially loaded)."""
    try:
        import config  # type: ignore
        return getattr(config, name, default)
    except Exception:
        return default


def _clean_items(raw: Any) -> List[Dict[str, Any]]:
    """Normalise a checklist into ``[{text:str, done:bool}]``.

    Tolerates: a list of dicts, a list of plain strings, or a single newline
    string (one item per line). Blank lines/items are dropped.
    """
    items: List[Dict[str, Any]] = []
    if raw is None:
        return items
    if isinstance(raw, str):
        raw = [ln for ln in raw.splitlines()]
    if not isinstance(raw, (list, tuple)):
        return items
    for it in raw:
        if isinstance(it, dict):
            text = str(it.get("text") or it.get("title") or "").strip()
            done = bool(it.get("done") or it.get("checked") or False)
        else:
            text = str(it or "").strip()
            done = False
        if text:
            items.append({"text": text, "done": done})
    return items


# ───────────────────────────────────────────────────────────────────────
# Data model
# ───────────────────────────────────────────────────────────────────────
@dataclass
class Note:
    """One note (free text) or checklist (``items``)."""

    id: str
    ts: float                       # created
    updated: float
    title: str = ""
    content: str = ""               # body for note_type="note"
    items: List[Dict[str, Any]] = field(default_factory=list)  # [{text, done}]
    note_type: str = NOTE           # "note" | "checklist"
    color: str = ""
    label: str = ""
    pinned: bool = False
    archived: bool = False
    due_date: str = ""              # optional ISO date string, free-form
    source: str = "ui"             # "ui" | "agent"
    sort_order: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    def open_items(self) -> int:
        return sum(1 for it in self.items if not it.get("done"))

    def done_items(self) -> int:
        return sum(1 for it in self.items if it.get("done"))


# ───────────────────────────────────────────────────────────────────────
# Store (JSONL, append-only — mirrors core.compare.ComparisonStore)
# ───────────────────────────────────────────────────────────────────────
class NotesStore:
    """Append-only JSONL-backed notes store. Thread-safe."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or DEFAULT_PATH
        self._lock = threading.Lock()
        self._entries: List[Note] = []
        self._loaded = False

    # ── persistence ──────────────────────────────────────────────────
    def _max_entries(self) -> int:
        try:
            return max(1, int(_cfg("NOTES_MAX_ENTRIES", DEFAULT_MAX_ENTRIES)))
        except (TypeError, ValueError):
            return DEFAULT_MAX_ENTRIES

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
                    self._entries.append(Note(
                        id=str(d.get("id") or ""),
                        ts=float(d.get("ts") or 0),
                        updated=float(d.get("updated") or d.get("ts") or 0),
                        title=str(d.get("title") or ""),
                        content=str(d.get("content") or ""),
                        items=_clean_items(d.get("items")),
                        note_type=str(d.get("note_type") or NOTE),
                        color=str(d.get("color") or ""),
                        label=str(d.get("label") or ""),
                        pinned=bool(d.get("pinned", False)),
                        archived=bool(d.get("archived", False)),
                        due_date=str(d.get("due_date") or ""),
                        source=str(d.get("source") or "ui"),
                        sort_order=int(d.get("sort_order") or 0),
                    ))
        except OSError:
            return
        cap = self._max_entries()
        if len(self._entries) > cap:
            self._entries = self._entries[-cap:]

    def _rewrite(self) -> None:
        """Rewrite the JSONL file from the in-memory list (after edit/trim)."""
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

    def _append(self, entry: Note) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _find(self, note_id: str) -> Optional[Note]:
        for n in self._entries:
            if n.id == note_id:
                return n
        return None

    # ── public API ───────────────────────────────────────────────────
    def add(
        self,
        *,
        title: str = "",
        content: str = "",
        items: Any = None,
        note_type: Optional[str] = None,
        label: str = "",
        color: str = "",
        due_date: str = "",
        pinned: bool = False,
        source: str = "ui",
    ) -> Note:
        """Create a note. ``note_type`` is auto-detected as ``checklist`` when
        ``items`` are supplied (unless explicitly given)."""
        title = (title or "").strip()
        content = (content or "").strip()
        clean = _clean_items(items)
        if note_type:
            ntype = CHECKLIST if str(note_type).strip().lower() == CHECKLIST else NOTE
        else:
            ntype = CHECKLIST if clean else NOTE
        now = time.time()
        with self._lock:
            self._load()
            # New notes float to the top of the manual order.
            min_order = min((n.sort_order for n in self._entries), default=0)
            note = Note(
                id=uuid.uuid4().hex[:12],
                ts=now,
                updated=now,
                title=title,
                content=content,
                items=clean,
                note_type=ntype,
                color=(color or "").strip(),
                label=(label or "").strip(),
                pinned=bool(pinned),
                archived=False,
                due_date=(due_date or "").strip(),
                source=(source or "ui").strip() or "ui",
                sort_order=min_order - 1,
            )
            self._entries.append(note)
            self._append(note)
            cap = self._max_entries()
            if len(self._entries) > cap:
                self._entries = self._entries[-cap:]
                self._rewrite()
            return note

    def get(self, note_id: str) -> Optional[Note]:
        with self._lock:
            self._load()
            return self._find(note_id)

    def update(self, note_id: str, **fields: Any) -> Optional[Note]:
        """Patch a note. Recognised fields: title, content, items, note_type,
        label, color, due_date, pinned, archived. Unknown keys are ignored.
        Bumps ``updated`` and persists."""
        with self._lock:
            self._load()
            note = self._find(note_id)
            if note is None:
                return None
            if "title" in fields:
                note.title = str(fields["title"] or "").strip()
            if "content" in fields:
                note.content = str(fields["content"] or "").strip()
            if "items" in fields:
                note.items = _clean_items(fields["items"])
            if "note_type" in fields and fields["note_type"]:
                note.note_type = (
                    CHECKLIST if str(fields["note_type"]).strip().lower() == CHECKLIST else NOTE
                )
            elif "items" in fields and note.items and note.note_type == NOTE:
                # Adding items to a plain note promotes it to a checklist.
                note.note_type = CHECKLIST
            if "label" in fields:
                note.label = str(fields["label"] or "").strip()
            if "color" in fields:
                note.color = str(fields["color"] or "").strip()
            if "due_date" in fields:
                note.due_date = str(fields["due_date"] or "").strip()
            if "pinned" in fields:
                note.pinned = bool(fields["pinned"])
            if "archived" in fields:
                note.archived = bool(fields["archived"])
            note.updated = time.time()
            self._rewrite()
            return note

    def delete(self, note_id: str) -> bool:
        with self._lock:
            self._load()
            before = len(self._entries)
            self._entries = [n for n in self._entries if n.id != note_id]
            if len(self._entries) == before:
                return False
            self._rewrite()
            return True

    def toggle_pin(self, note_id: str) -> Optional[Note]:
        with self._lock:
            self._load()
            note = self._find(note_id)
            if note is None:
                return None
            note.pinned = not note.pinned
            note.updated = time.time()
            self._rewrite()
            return note

    def toggle_archive(self, note_id: str) -> Optional[Note]:
        with self._lock:
            self._load()
            note = self._find(note_id)
            if note is None:
                return None
            note.archived = not note.archived
            note.updated = time.time()
            self._rewrite()
            return note

    def toggle_item(self, note_id: str, index: int) -> Optional[Note]:
        """Flip the ``done`` flag of checklist item ``index``. Returns None if
        the note or index is missing."""
        with self._lock:
            self._load()
            note = self._find(note_id)
            if note is None:
                return None
            if index < 0 or index >= len(note.items):
                return None
            note.items[index]["done"] = not note.items[index].get("done", False)
            note.updated = time.time()
            self._rewrite()
            return note

    def reorder(self, ordered_ids: List[str]) -> int:
        """Assign ``sort_order`` from the given id order (0,1,2…). Ids not in
        the store are ignored; notes not listed keep their order after the
        listed ones. Returns the count of notes repositioned."""
        with self._lock:
            self._load()
            pos = {nid: i for i, nid in enumerate(ordered_ids or [])}
            tail = len(pos)
            moved = 0
            for n in self._entries:
                if n.id in pos:
                    n.sort_order = pos[n.id]
                    moved += 1
                else:
                    n.sort_order = tail
                    tail += 1
            if moved:
                self._rewrite()
            return moved

    def list(
        self,
        *,
        include_archived: bool = False,
        label: Optional[str] = None,
        query: Optional[str] = None,
    ) -> List[Note]:
        """Board order: pinned first, then manual ``sort_order``, then most
        recently updated. Filters by archived flag, label, and a substring
        query over title/content/items."""
        with self._lock:
            self._load()
            rows = list(self._entries)
        if not include_archived:
            rows = [n for n in rows if not n.archived]
        if label:
            lab = label.strip().lower()
            rows = [n for n in rows if n.label.lower() == lab]
        if query:
            q = query.strip().lower()
            def _hit(n: Note) -> bool:
                if q in n.title.lower() or q in n.content.lower():
                    return True
                return any(q in str(it.get("text", "")).lower() for it in n.items)
            rows = [n for n in rows if _hit(n)]
        rows.sort(key=lambda n: (not n.pinned, n.sort_order, -n.updated))
        return rows

    def labels(self) -> List[str]:
        """Distinct non-empty labels currently in use, alphabetical."""
        with self._lock:
            self._load()
            return sorted({n.label for n in self._entries if n.label})

    def all(self) -> List[Note]:
        with self._lock:
            self._load()
            return list(self._entries)

    def clear(self) -> int:
        """Wipe the store. Returns the number of entries removed."""
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
_store: Optional[NotesStore] = None
_singleton_lock = threading.Lock()


def get_notes_store() -> NotesStore:
    global _store
    if _store is None:
        with _singleton_lock:
            if _store is None:
                _store = NotesStore()
    return _store


def reset_notes_store_for_tests(path: Optional[Path] = None) -> NotesStore:
    """Test helper — swap in a fresh store, optionally with a custom path."""
    global _store
    with _singleton_lock:
        _store = NotesStore(path=path)
    return _store


__all__ = [
    "Note",
    "NotesStore",
    "NOTE",
    "CHECKLIST",
    "get_notes_store",
    "reset_notes_store_for_tests",
]
