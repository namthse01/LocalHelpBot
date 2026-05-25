"""Self-learning tools — Slice 4 of the v4 upgrade.

  save_lesson      — record a one-line lesson into the LessonsStore.
                     Used by the agent OR by the /remember slash command.
  learn_from_file  — chunk + embed a local file into the RAG corpus.
  learn_from_url   — fetch + extract + chunk + embed a URL into the RAG corpus.
  update_self      — `git pull --ff-only` on the configured branch. Refuses
                     dirty trees. Permission-gated. Off by default
                     (config flag UPDATE_SELF_ENABLED).

All return typed `ToolResult`. RAG ingest tools reuse the existing
pipeline at `data/chunker.py` + `data/embedder.py` + `data/storage.py`.
"""
from __future__ import annotations

import os
import re
import subprocess
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from core.lessons import get_lessons_store
from core.permissions import request_permission
from core.tool_schema import ErrorCode, Tool, ToolRegistry, ToolResult


# ───────────────────────────────────────────────────────────────────────
# save_lesson
# ───────────────────────────────────────────────────────────────────────


def _save_lesson(args: Dict[str, Any]) -> ToolResult:
    lesson = (args.get("lesson") or "").strip()
    scope = (args.get("scope") or "global").strip() or "global"
    trigger = (args.get("trigger") or "").strip()
    if not lesson:
        return ToolResult.error(ErrorCode.INVALID_ARGS, "save_lesson requires 'lesson'.", retryable=False)
    if scope not in ("global",) and not scope.startswith("session:"):
        return ToolResult.error(
            ErrorCode.INVALID_ARGS,
            "scope must be 'global' or 'session:<sid>'.",
            retryable=False,
        )
    store = get_lessons_store()
    entry = store.record(lesson=lesson, scope=scope, trigger=trigger)
    if entry is None:
        return ToolResult.success("OK: lesson was empty, nothing saved.")
    return ToolResult.success(
        f"OK: lesson saved (scope={entry.scope})\n  > {entry.lesson}",
        scope=entry.scope, ts=entry.ts,
    )


# ───────────────────────────────────────────────────────────────────────
# RAG ingest helpers
# ───────────────────────────────────────────────────────────────────────


def _ingest_text_into_rag(text: str, source: str) -> Dict[str, Any]:
    """Chunk + embed a text blob into the existing cad_db collection.

    Returns a dict with `chunks_added` and the source label. Reuses
    `data.chunker` if available; falls back to a naive sliding window.
    Embeddings go through `data.embedder` if exposed; else through the
    Ollama /api/embed endpoint directly.
    """
    # Try to import the project's RAG pipeline; fall back gracefully if shapes
    # have shifted since this was written.
    chunks: list[str] = []
    try:
        from data.chunker import chunk_text  # type: ignore
        chunks = list(chunk_text(text))   # function may yield or return list
    except Exception:
        # Naive 1500-char sliding window with 200-char overlap.
        step, overlap = 1500, 200
        i = 0
        while i < len(text):
            chunks.append(text[i:i + step])
            i += max(1, step - overlap)
    chunks = [c.strip() for c in chunks if c and c.strip()]
    if not chunks:
        return {"chunks_added": 0, "source": source}

    # Embed via the Ollama HTTP endpoint (matches what core/query.py does).
    import json
    from config import EMBED_MODEL, OLLAMA_BASE
    embeddings: list[list[float]] = []
    for c in chunks:
        body = json.dumps({"model": EMBED_MODEL, "input": [c]}).encode()
        req = urllib.request.Request(
            OLLAMA_BASE + "/api/embed", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
        embeddings.append(data["embeddings"][0])

    # Insert into ChromaDB. Lazy import so we don't pay the chromadb load
    # at proxy startup.
    import chromadb
    import hashlib
    DB_PATH = Path("cad_db")
    DB_PATH.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(DB_PATH))
    col = client.get_or_create_collection(name="cad_docs")
    ids = [f"{source}::{hashlib.sha1((c[:64] + str(i)).encode()).hexdigest()[:16]}" for i, c in enumerate(chunks)]
    metas = [{"file_name": source, "source": source, "chunk": i} for i, _ in enumerate(chunks)]
    col.upsert(ids=ids, documents=chunks, embeddings=embeddings, metadatas=metas)
    return {"chunks_added": len(chunks), "source": source}


def _learn_from_file(args: Dict[str, Any]) -> ToolResult:
    path = (args.get("path") or "").strip()
    if not path:
        return ToolResult.error(ErrorCode.INVALID_ARGS, "learn_from_file requires 'path'.", retryable=False)
    p = Path(path)
    if not p.exists():
        return ToolResult.error(ErrorCode.FILE_NOT_FOUND, f"File not found: {path}")
    # Extract text by extension. PDF / DOCX go through their readers;
    # everything else is read as UTF-8 text.
    text = ""
    suffix = p.suffix.lower()
    try:
        if suffix == ".pdf":
            try:
                import pypdf  # type: ignore
                reader = pypdf.PdfReader(str(p))
                text = "\n\n".join((pg.extract_text() or "") for pg in reader.pages)
            except ImportError:
                return ToolResult.error(
                    ErrorCode.INVALID_ARGS, "pypdf not installed.",
                    hint="install_package name='pypdf' to ingest PDFs.",
                    retryable=False,
                )
        elif suffix == ".docx":
            try:
                import docx  # type: ignore
                d = docx.Document(str(p))
                text = "\n".join(par.text for par in d.paragraphs)
            except ImportError:
                return ToolResult.error(
                    ErrorCode.INVALID_ARGS, "python-docx not installed.",
                    hint="install_package name='python-docx' to ingest DOCX.",
                    retryable=False,
                )
        else:
            text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"learn_from_file: extract failed: {e}")

    text = (text or "").strip()
    if not text:
        return ToolResult.error(ErrorCode.NO_RESULTS, f"learn_from_file: {p} has no extractable text.")
    try:
        result = _ingest_text_into_rag(text, source=str(p.resolve()))
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"learn_from_file: ingest failed: {e}",
                                hint="Make sure cad_db/ exists and EMBED_MODEL is installed in Ollama.")
    return ToolResult.success(
        f"OK: learned from {p} — {result['chunks_added']} chunks added to RAG corpus.",
        source=result["source"], chunks_added=result["chunks_added"],
    )


def _learn_from_url(args: Dict[str, Any]) -> ToolResult:
    url = (args.get("url") or "").strip()
    if not url:
        return ToolResult.error(ErrorCode.INVALID_ARGS, "learn_from_url requires 'url'.", retryable=False)
    if not re.match(r"^https?://", url):
        return ToolResult.error(ErrorCode.INVALID_ARGS, "url must start with http:// or https://", retryable=False)

    # Reuse the readability extractor from web_extras for clean text.
    try:
        from core.plugins.web_extras import _readability, _http_get  # type: ignore
        status, raw, ct = _http_get(url)
        if status != 200:
            return ToolResult.error(ErrorCode.UNKNOWN, f"learn_from_url: HTTP {status} for {url}")
        charset = "utf-8"
        m = re.search(r"charset=([^\s;\"']+)", ct or "")
        if m:
            charset = m.group(1).strip()
        html = raw.decode(charset, errors="replace")
        info = _readability(html, max_chars=200_000)
        text = (info["title"] + "\n\n" if info["title"] else "") + info["text"]
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"learn_from_url: fetch failed: {e}")

    text = (text or "").strip()
    if not text:
        return ToolResult.error(ErrorCode.NO_RESULTS, f"learn_from_url: no extractable text from {url}.")
    try:
        result = _ingest_text_into_rag(text, source=url)
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"learn_from_url: ingest failed: {e}",
                                hint="Make sure cad_db/ exists and EMBED_MODEL is installed in Ollama.")
    return ToolResult.success(
        f"OK: learned from {url} — {result['chunks_added']} chunks added to RAG corpus.",
        source=result["source"], chunks_added=result["chunks_added"],
    )


# ───────────────────────────────────────────────────────────────────────
# update_self  (Slice 4.6) — git pull on main, dirty-tree refuse, gated.
# ───────────────────────────────────────────────────────────────────────


_DEFAULT_UPDATE_BRANCH = "main"
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _git(*cmd: str, cwd: Optional[Path] = None, timeout: float = 30.0) -> tuple[int, str, str]:
    try:
        out = subprocess.run(
            ["git", *cmd],
            cwd=str(cwd or _REPO_ROOT),
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        return out.returncode, out.stdout.strip(), out.stderr.strip()
    except FileNotFoundError:
        return 127, "", "git not on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", "git timed out"


def _update_self(args: Dict[str, Any]) -> ToolResult:
    # Feature flag — refuse unless explicitly enabled in config.py.
    try:
        from config import UPDATE_SELF_ENABLED   # type: ignore
        enabled = bool(UPDATE_SELF_ENABLED)
    except Exception:
        enabled = False
    if not enabled:
        return ToolResult.error(
            ErrorCode.PERMISSION_DENIED,
            "update_self is disabled. Set UPDATE_SELF_ENABLED = True in config.py to allow it.",
            retryable=False,
        )
    branch = (args.get("branch") or _DEFAULT_UPDATE_BRANCH).strip() or _DEFAULT_UPDATE_BRANCH

    # 1) Are we even in a git repo?
    rc, head, _ = _git("rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0:
        return ToolResult.error(ErrorCode.UNKNOWN, f"update_self: not a git repo or git missing ({rc}).", retryable=False)

    # 2) Refuse on a branch other than the configured update branch.
    if head != branch:
        return ToolResult.error(
            ErrorCode.INVALID_ARGS,
            f"update_self: refuses to run on branch '{head}' (configured: '{branch}'). Check out '{branch}' first.",
            retryable=False,
        )

    # 3) Refuse on a dirty working tree.
    rc, status, _ = _git("status", "--porcelain")
    if rc != 0:
        return ToolResult.error(ErrorCode.UNKNOWN, "update_self: git status failed.", retryable=False)
    if status:
        return ToolResult.error(
            ErrorCode.INVALID_ARGS,
            f"update_self: dirty working tree — refuses to pull.\n{status[:500]}",
            hint="Commit / stash your changes before updating.",
            retryable=False,
        )

    # 4) Permission gate (with summary).
    rc, fetch_log, _ = _git("fetch", "--quiet")
    if rc != 0:
        return ToolResult.error(ErrorCode.EXTERNAL_TIMEOUT, "update_self: git fetch failed.", retryable=False)
    rc, ahead, _ = _git("rev-list", "--count", f"HEAD..origin/{branch}")
    behind = int(ahead) if ahead.isdigit() else 0
    if behind == 0:
        return ToolResult.success(f"OK: already at the tip of origin/{branch}; nothing to pull.", behind=0)

    rc, log_lines, _ = _git("log", "--oneline", f"HEAD..origin/{branch}", "--max-count=10")
    decision = request_permission(
        "update_self", f"git pull --ff-only origin/{branch}",
        {
            "branch": branch,
            "commits_behind": behind,
            "incoming_commits_preview": (log_lines or "(none)")[:1200],
        },
    )
    if not decision["allowed"]:
        return ToolResult.error(
            ErrorCode.PERMISSION_DENIED,
            f"User declined update_self ({decision['reason']}).",
            retryable=False,
        )

    # 5) Pull (fast-forward only).
    rc, pull_out, pull_err = _git("pull", "--ff-only", "origin", branch, timeout=60.0)
    if rc != 0:
        return ToolResult.error(
            ErrorCode.UNKNOWN,
            f"update_self: git pull failed (exit {rc}). STDERR: {pull_err[:500]}",
            retryable=False,
        )
    return ToolResult.success(
        f"OK: pulled {behind} commits onto {branch}.\n{pull_out[:800]}\n"
        f"NOTE: a proxy restart is needed for code changes to take effect.",
        branch=branch, commits_pulled=behind,
    )


# ───────────────────────────────────────────────────────────────────────
# Register
# ───────────────────────────────────────────────────────────────────────


def register(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="save_lesson",
        description=(
            "Persist a short lesson the user wants you to remember in future "
            "conversations. Use when the user says 'remember', 'always', "
            "'I prefer', 'don't do X', etc. Lessons appear in T1 of every "
            "future system prompt."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "lesson":  {"type": "string", "description": "≤400 chars; the rule to remember."},
                "scope":   {"type": "string", "description": "'global' (default) or 'session:<sid>'."},
                "trigger": {"type": "string", "description": "Optional: the assistant message that prompted the correction."},
            },
            "required": ["lesson"],
        },
        handler=_save_lesson,
        category="meta",
    ))
    registry.register(Tool(
        name="learn_from_file",
        description=(
            "Chunk + embed a local file (PDF/DOCX/TXT/MD/code) into the local "
            "RAG corpus so it becomes findable via `query_rag` in future "
            "conversations. Reuses the project's chunker + EMBED_MODEL."
        ),
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        handler=_learn_from_file,
        category="meta",
    ))
    registry.register(Tool(
        name="learn_from_url",
        description=(
            "Fetch a URL, run readability-style extraction, chunk + embed the "
            "result into the local RAG corpus. The page becomes searchable via "
            "`query_rag` in future conversations."
        ),
        input_schema={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        handler=_learn_from_url,
        category="meta",
    ))
    registry.register(Tool(
        name="update_self",
        description=(
            "Pull the latest LocalHelpBot code from `origin/<branch>`. "
            "Fast-forward only. Refuses dirty trees or unexpected branches. "
            "Asks user permission with a preview of incoming commits. "
            "DISABLED unless config.py sets UPDATE_SELF_ENABLED = True."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "branch": {"type": "string", "description": "Default 'main'."},
            },
        },
        handler=_update_self,
        requires_permission=True,
        category="meta",
    ))
