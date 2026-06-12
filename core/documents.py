"""Documents — a living-documents store with version history.

Ported (adapted, not copied) from odysseus ``routes/document_routes.py`` +
``routes/document_helpers.py`` + ``src/document_actions.py``. The odysseus
version is FastAPI + SQLAlchemy (``Document`` + ``DocumentVersion`` tables) +
multi-user owner scoping + heavy PDF-form / e-signature / vision-annotation
routes. TheAgent0 is a single-user local tool, so this is a synchronous,
stdlib-first, JSONL-backed re-implementation that keeps the *concepts*:

  • a document has a current markdown ``content`` and a capped ``versions``
    history (each snapshot: version_number, content, summary, source, ts),
  • saving within ``VERSION_COALESCE_SECONDS`` of the previous edit by the
    same source coalesces in place instead of spawning a new version (mirrors
    odysseus' 60-second window) — so rapid keystroke-saves don't explode the
    history,
  • a document can be archived (soft hide) or deleted (hard remove),
  • the library lists with substring search (AND across terms), a language
    facet, and a sort (recent / oldest / edits / alpha),
  • a previous version can be restored (copied forward as a NEW version),
  • ``tidy`` flags conservative junk (blank / throwaway-titled docs) and exact
    content duplicates — dry-run by default; it never deletes on its own.

The PDF-form / e-signature / vision-annotation routes, the email-attachment
provenance, and the owner/auth scoping from odysseus are deliberately OUT OF
SCOPE here (single-user, no auth).

Storage mirrors :mod:`core.notes` / :mod:`core.compare` exactly (JSONL with a
``threading`` lock, a ``MAX_ENTRIES`` cap read from
``config.DOCUMENTS_MAX_ENTRIES``, a module singleton + a ``reset_*_for_tests``
helper).

Public surface:
    Document, DocumentsStore
    derive_title() / detect_language()
    get_documents_store() / reset_documents_store_for_tests(path)
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_MAX_ENTRIES = 200
DEFAULT_MAX_VERSIONS = 50
VERSION_COALESCE_SECONDS = 60.0
DEFAULT_PATH = Path("data") / "documents" / "documents.jsonl"


def _cfg(name: str, default: Any) -> Any:
    """Read a config tunable defensively (config may be partially loaded)."""
    try:
        import config  # type: ignore
        return getattr(config, name, default)
    except Exception:
        return default


# ───────────────────────────────────────────────────────────────────────
# Title / language helpers (ported from document_helpers.py:_derive_title)
# ───────────────────────────────────────────────────────────────────────
def _clip(title: str, n: int = 80) -> str:
    title = title or ""
    return title if len(title) <= n else title[: n - 1] + "…"


def derive_title(content: str) -> str:
    """Derive a human title from document content.

    markdown header → HTML heading → first short line → ``"Untitled"``.
    """
    if not isinstance(content, str):
        return "Untitled"
    text = content.strip()
    if not text:
        return "Untitled"
    md = re.match(r"^#{1,6}\s+(.+)", text, re.MULTILINE)
    if md:
        return _clip(md.group(1).strip()) or "Untitled"
    h = re.search(r"<h[1-6][^>]*>([^<]+)</h[1-6]>", text, re.IGNORECASE)
    if h:
        return _clip(h.group(1).strip()) or "Untitled"
    for line in text.split("\n"):
        line = line.strip()
        if line and 2 <= len(line) <= 80:
            cleaned = re.sub(r"[:#*`]+$", "", line).strip()
            return _clip(cleaned) if cleaned else "Untitled"
    return "Untitled"


def detect_language(content: str) -> str:
    """Light format sniff for the library facet. Defaults to ``markdown`` (the
    editor's native format)."""
    t = (content or "").strip()
    if not t:
        return "markdown"
    low = t.lower()
    if low.startswith("<!doctype html") or "<html" in low[:200]:
        return "html"
    if (t.startswith("{") and t.rstrip().endswith("}")) or (
        t.startswith("[") and t.rstrip().endswith("]")
    ):
        try:
            json.loads(t)
            return "json"
        except Exception:
            pass
    if re.search(r"^\s*(def |class |import |from \w+ import )", t, re.MULTILINE):
        return "python"
    if re.search(r"^#{1,6}\s|\*\*|\[.+\]\(.+\)|^[-*]\s", t, re.MULTILINE):
        return "markdown"
    return "text"


# ───────────────────────────────────────────────────────────────────────
# Tidy helpers (ported from src/document_actions.py — conservative junk/dupe)
# ───────────────────────────────────────────────────────────────────────
_JUNK_TITLES = {
    "", "untitled", "untitled document", "untitled-1", "test", "testing",
    "asdf", "qwer", "foo", "bar", "draft", "new document", "new doc",
    "temp", "tmp", "scratch", "aaa",
}


def _norm_title(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip().lower())


def _real_len(content: str) -> int:
    """Length of content with code fences / markdown punctuation / whitespace
    stripped — a rough proxy for 'is there actually anything here'."""
    if not content:
        return 0
    s = re.sub(r"```.*?```", "", content, flags=re.DOTALL)   # code fences
    s = re.sub(r"[#>*`_\-\[\]()!]", "", s)                    # md punctuation
    s = re.sub(r"\s+", "", s)                                  # all whitespace
    return len(s)


def _fingerprint(content: str) -> str:
    return re.sub(r"\s+", " ", (content or "").strip().lower())


# ───────────────────────────────────────────────────────────────────────
# Data model
# ───────────────────────────────────────────────────────────────────────
@dataclass
class Document:
    """One living document: current ``content`` + a ``versions`` history."""

    id: str
    ts: float                       # created
    updated: float
    title: str = "Untitled"
    content: str = ""               # current live markdown
    language: str = "markdown"
    source: str = "ui"             # "ui" | "agent"
    archived: bool = False
    # each version: {version_number:int, content:str, summary:str,
    #                source:str, ts:float}; versions[-1] mirrors `content`.
    versions: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def version_count(self) -> int:
        return len(self.versions)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["version_count"] = self.version_count
        return d

    def summary_dict(self) -> dict:
        """Library row — omits the heavy ``content`` / ``versions`` blobs."""
        return {
            "id": self.id,
            "ts": self.ts,
            "updated": self.updated,
            "title": self.title,
            "language": self.language,
            "source": self.source,
            "archived": self.archived,
            "version_count": self.version_count,
            "preview": (self.content or "")[:160],
            "chars": len(self.content or ""),
        }


# ───────────────────────────────────────────────────────────────────────
# Store (JSONL, append-on-add / rewrite-on-edit — mirrors core.notes)
# ───────────────────────────────────────────────────────────────────────
class DocumentsStore:
    """JSONL-backed living-documents store. Thread-safe."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or DEFAULT_PATH
        self._lock = threading.Lock()
        self._entries: List[Document] = []
        self._loaded = False

    # ── persistence ──────────────────────────────────────────────────
    def _max_entries(self) -> int:
        try:
            return max(1, int(_cfg("DOCUMENTS_MAX_ENTRIES", DEFAULT_MAX_ENTRIES)))
        except (TypeError, ValueError):
            return DEFAULT_MAX_ENTRIES

    def _max_versions(self) -> int:
        try:
            return max(1, int(_cfg("DOCUMENTS_MAX_VERSIONS", DEFAULT_MAX_VERSIONS)))
        except (TypeError, ValueError):
            return DEFAULT_MAX_VERSIONS

    @staticmethod
    def _clean_versions(raw: Any) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if not isinstance(raw, (list, tuple)):
            return out
        for v in raw:
            if not isinstance(v, dict):
                continue
            out.append({
                "version_number": int(v.get("version_number") or len(out) + 1),
                "content": str(v.get("content") or ""),
                "summary": str(v.get("summary") or ""),
                "source": str(v.get("source") or "ui"),
                "ts": float(v.get("ts") or 0),
            })
        return out

    def _from_dict(self, d: Dict[str, Any]) -> Document:
        content = str(d.get("content") or "")
        versions = self._clean_versions(d.get("versions"))
        if not versions:
            versions = [{
                "version_number": 1,
                "content": content,
                "summary": "Initial version",
                "source": str(d.get("source") or "ui"),
                "ts": float(d.get("ts") or 0),
            }]
        return Document(
            id=str(d.get("id") or ""),
            ts=float(d.get("ts") or 0),
            updated=float(d.get("updated") or d.get("ts") or 0),
            title=str(d.get("title") or "Untitled"),
            content=content,
            language=str(d.get("language") or "markdown"),
            source=str(d.get("source") or "ui"),
            archived=bool(d.get("archived", False)),
            versions=versions,
        )

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
                    self._entries.append(self._from_dict(d))
        except OSError:
            return
        cap = self._max_entries()
        if len(self._entries) > cap:
            self._entries = self._entries[-cap:]

    def _rewrite(self) -> None:
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

    def _append(self, entry: Document) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _find(self, doc_id: str) -> Optional[Document]:
        for d in self._entries:
            if d.id == doc_id:
                return d
        return None

    def _trim_versions(self, doc: Document) -> None:
        """Keep the origin (v1) + the most recent ``max_versions - 1``."""
        mv = self._max_versions()
        if len(doc.versions) > mv:
            doc.versions = [doc.versions[0]] + doc.versions[-(mv - 1):]

    # ── public API ───────────────────────────────────────────────────
    def add(
        self,
        *,
        title: str = "",
        content: str = "",
        language: str = "",
        source: str = "ui",
    ) -> Document:
        """Create a new document with an initial (v1) version."""
        content = content or ""
        src = (source or "ui").strip() or "ui"
        now = time.time()
        title = (title or "").strip() or derive_title(content)
        lang = (language or "").strip() or detect_language(content)
        with self._lock:
            self._load()
            doc = Document(
                id=uuid.uuid4().hex[:12],
                ts=now,
                updated=now,
                title=title,
                content=content,
                language=lang,
                source=src,
                archived=False,
                versions=[{
                    "version_number": 1,
                    "content": content,
                    "summary": "Initial version",
                    "source": src,
                    "ts": now,
                }],
            )
            self._entries.append(doc)
            self._append(doc)
            cap = self._max_entries()
            if len(self._entries) > cap:
                self._entries = self._entries[-cap:]
                self._rewrite()
            return doc

    def get(self, doc_id: str) -> Optional[Document]:
        with self._lock:
            self._load()
            return self._find(doc_id)

    def update(
        self,
        doc_id: str,
        *,
        content: Optional[str] = None,
        summary: str = "",
        title: Optional[str] = None,
        language: Optional[str] = None,
        source: str = "ui",
        coalesce: bool = True,
    ) -> Optional[Document]:
        """Save a document. A changed ``content`` spawns a new version unless
        the previous version is from the same source and younger than
        ``VERSION_COALESCE_SECONDS`` (then it's coalesced in place). Passing
        only ``title``/``language`` is a metadata-only edit (no new version)."""
        with self._lock:
            self._load()
            doc = self._find(doc_id)
            if doc is None:
                return None
            now = time.time()
            src = (source or "ui").strip() or "ui"
            if title is not None:
                doc.title = (title or "").strip() or doc.title
            if language is not None:
                doc.language = (language or "").strip() or doc.language
            if content is not None and content != doc.content:
                doc.content = content
                last = doc.versions[-1] if doc.versions else None
                if (coalesce and last is not None
                        and last.get("source") == src
                        and (now - float(last.get("ts") or 0)) <= VERSION_COALESCE_SECONDS):
                    last["content"] = content
                    last["ts"] = now
                    if summary:
                        last["summary"] = summary
                else:
                    next_num = (int(last.get("version_number", 0)) + 1) if last else 1
                    doc.versions.append({
                        "version_number": next_num,
                        "content": content,
                        "summary": summary or "",
                        "source": src,
                        "ts": now,
                    })
                    self._trim_versions(doc)
            doc.updated = now
            self._rewrite()
            return doc

    def delete(self, doc_id: str) -> bool:
        with self._lock:
            self._load()
            before = len(self._entries)
            self._entries = [d for d in self._entries if d.id != doc_id]
            if len(self._entries) == before:
                return False
            self._rewrite()
            return True

    def toggle_archive(self, doc_id: str) -> Optional[Document]:
        with self._lock:
            self._load()
            doc = self._find(doc_id)
            if doc is None:
                return None
            doc.archived = not doc.archived
            doc.updated = time.time()
            self._rewrite()
            return doc

    def restore_version(self, doc_id: str, version_number: int) -> Optional[Document]:
        """Copy an earlier version's content forward as a NEW current version.
        Returns None if the document or version number is missing."""
        with self._lock:
            self._load()
            doc = self._find(doc_id)
            if doc is None:
                return None
            try:
                want = int(version_number)
            except (TypeError, ValueError):
                return None
            target = next(
                (v for v in doc.versions if int(v.get("version_number")) == want), None)
            if target is None:
                return None
            now = time.time()
            last = doc.versions[-1] if doc.versions else None
            doc.content = target.get("content") or ""
            next_num = (int(last.get("version_number", 0)) + 1) if last else 1
            doc.versions.append({
                "version_number": next_num,
                "content": doc.content,
                "summary": f"Restored from v{want}",
                "source": "ui",
                "ts": now,
            })
            self._trim_versions(doc)
            doc.updated = now
            self._rewrite()
            return doc

    def list(
        self,
        *,
        include_archived: bool = False,
        query: Optional[str] = None,
        language: Optional[str] = None,
        sort: str = "recent",
    ) -> List[Document]:
        """Library view. Filters by archived flag, language facet, and an
        AND-of-terms substring query over title+content. ``sort`` is one of
        recent (default) / oldest / edits / alpha."""
        with self._lock:
            self._load()
            rows = list(self._entries)
        if not include_archived:
            rows = [d for d in rows if not d.archived]
        if language:
            lang = language.strip().lower()
            rows = [d for d in rows if d.language.lower() == lang]
        if query:
            terms = [t for t in query.strip().lower().split() if t]
            if terms:
                def _hit(d: Document) -> bool:
                    hay = (d.title + "\n" + d.content).lower()
                    return all(t in hay for t in terms)
                rows = [d for d in rows if _hit(d)]
        sort = (sort or "recent").lower()
        if sort == "oldest":
            rows.sort(key=lambda d: d.ts)
        elif sort == "edits":
            rows.sort(key=lambda d: (-d.version_count, -d.updated))
        elif sort in ("alpha", "title"):
            rows.sort(key=lambda d: d.title.lower())
        else:  # "recent"
            rows.sort(key=lambda d: -d.updated)
        return rows

    def languages(self) -> List[str]:
        """Distinct non-empty languages currently in use, alphabetical."""
        with self._lock:
            self._load()
            return sorted({d.language for d in self._entries if d.language})

    def all(self) -> List[Document]:
        with self._lock:
            self._load()
            return list(self._entries)

    def tidy(self, *, apply: bool = False, junk_min_chars: int = 12) -> Dict[str, Any]:
        """Conservative cleanup over non-archived docs. Flags (and, with
        ``apply=True``, deletes):

          • junk — a throwaway/blank title AND < ``junk_min_chars`` of real
            content,
          • duplicates — identical content fingerprint; keeps the one with the
            most real content (ties → most recently updated).

        Returns ``{junk, duplicates, removed, kept}`` (id lists; ``removed`` is
        empty unless ``apply``)."""
        with self._lock:
            self._load()
            live = [d for d in self._entries if not d.archived]
            junk_ids: List[str] = []
            for d in live:
                if (_norm_title(d.title) in _JUNK_TITLES
                        and _real_len(d.content) < junk_min_chars):
                    junk_ids.append(d.id)
            junk_set = set(junk_ids)
            groups: Dict[str, List[Document]] = {}
            for d in live:
                if d.id in junk_set:
                    continue
                fp = _fingerprint(d.content)
                if not fp:
                    continue
                groups.setdefault(fp, []).append(d)
            dup_ids: List[str] = []
            for docs in groups.values():
                if len(docs) < 2:
                    continue
                ordered = sorted(
                    docs, key=lambda d: (_real_len(d.content), d.updated), reverse=True)
                dup_ids.extend(d.id for d in ordered[1:])
            remove = junk_ids + dup_ids
            removed: List[str] = []
            if apply and remove:
                rm = set(remove)
                before = len(self._entries)
                self._entries = [d for d in self._entries if d.id not in rm]
                if len(self._entries) != before:
                    removed = remove
                    self._rewrite()
            return {
                "junk": junk_ids,
                "duplicates": dup_ids,
                "removed": removed,
                "kept": len(self._entries),
            }

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
_store: Optional[DocumentsStore] = None
_singleton_lock = threading.Lock()


def get_documents_store() -> DocumentsStore:
    global _store
    if _store is None:
        with _singleton_lock:
            if _store is None:
                _store = DocumentsStore()
    return _store


def reset_documents_store_for_tests(path: Optional[Path] = None) -> DocumentsStore:
    """Test helper — swap in a fresh store, optionally with a custom path."""
    global _store
    with _singleton_lock:
        _store = DocumentsStore(path=path)
    return _store


__all__ = [
    "Document",
    "DocumentsStore",
    "derive_title",
    "detect_language",
    "get_documents_store",
    "reset_documents_store_for_tests",
]
