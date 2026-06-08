"""Tool implementations for the agentic proxy.

All tools return a typed `ToolResult` from `core.tool_schema` so the
agent loop can:
  • Distinguish recoverable from fatal errors via `error_code`.
  • Show structured per-call recovery hints in the `<tool_result>` tag.
  • Implement Stop-the-Line: refuse the third retry of the same
    (tool_name, args_hash) when it keeps failing.

File tools:  read_file, write_file, edit_file, list_dir, grep_file,
             glob_files, run_command
Web tools:   search_web, fetch_url

For callers still passing dicts of plain callables (e.g. legacy
`run_agent(messages, FILE_TOOLS)` from older proxy paths) the
`ToolResult` returned here serialises to its `output` field when
treated as a string — the agent loop has explicit handling.
"""

from __future__ import annotations

import re
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from core.tool_schema import ErrorCode, ToolResult

# Beyond this size, local 7B-class models reliably corrupt the JSON
# payload (missing closing quotes, unescaped newlines). We reject large
# single writes and tell the model to chunk via write_file + edit_file.
_WRITE_FILE_MAX_CHARS = 3000

# Per-file read cap. Large files should be sliced via grep/edit.
_READ_FILE_MAX_BYTES = 200_000


# ───────────────────────────────────────────────────────────────────────
# FILE TOOLS
# ───────────────────────────────────────────────────────────────────────


def read_file(path: str) -> ToolResult:
    try:
        p = Path(path)
        if not p.exists():
            return ToolResult.error(
                ErrorCode.FILE_NOT_FOUND,
                f"File not found: {path}",
                hint="Run list_dir on the parent directory to confirm the name.",
            )
        size = p.stat().st_size
        if size > _READ_FILE_MAX_BYTES:
            return ToolResult.error(
                ErrorCode.TOO_LARGE,
                f"File too large ({size} bytes > {_READ_FILE_MAX_BYTES}).",
                hint="Use grep_file or read_file_chunk to slice the file.",
                retryable=False,
                size=size,
            )
        text = p.read_text(encoding="utf-8", errors="replace")
        return ToolResult.success(text, path=str(p), size=size)
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"read_file failed: {e}")


def write_file(path: str, content: str) -> ToolResult:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return ToolResult.success(
            f"OK: Written {len(content)} chars to {path}",
            path=str(p),
            bytes_written=len(content),
            files_touched=[str(p)],
        )
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"write_file failed: {e}")


def list_dir(path: str = ".") -> ToolResult:
    try:
        p = Path(path)
        if not p.exists():
            return ToolResult.error(ErrorCode.FILE_NOT_FOUND, f"Path not found: {path}")
        items = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))
        lines = []
        for item in items:
            if item.is_dir():
                lines.append(f"[DIR]  {item.name}/")
            else:
                lines.append(f"[FILE] {item.name} ({item.stat().st_size:,} bytes)")
        body = "\n".join(lines) if lines else "(empty)"
        return ToolResult.success(body, count=len(items))
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"list_dir failed: {e}")


def run_command(command: str, cwd: Optional[str] = None) -> ToolResult:
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True,
            text=True, timeout=60, cwd=cwd,
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
                ErrorCode.UNKNOWN,
                body,
                hint="Inspect STDERR for the underlying failure.",
                exit_code=result.returncode,
            )
        return ToolResult.success(body, exit_code=0)
    except subprocess.TimeoutExpired:
        return ToolResult.error(
            ErrorCode.EXTERNAL_TIMEOUT,
            "Command timed out (60s).",
            hint="Try a faster command or split into steps.",
        )
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"run_command failed: {e}")


def edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> ToolResult:
    try:
        p = Path(path)
        if not p.exists():
            return ToolResult.error(ErrorCode.FILE_NOT_FOUND, f"File not found: {path}")
        text = p.read_text(encoding="utf-8", errors="replace")
        if old_string == new_string:
            return ToolResult.error(
                ErrorCode.INVALID_ARGS,
                "old_string and new_string are identical — nothing to do.",
                retryable=False,
            )
        if old_string not in text:
            return ToolResult.error(
                ErrorCode.STALE_OLD_STRING,
                f"old_string not found in {path}.",
                hint="Read the file first and copy the EXACT text (whitespace included).",
            )
        count = text.count(old_string)
        if count > 1 and not replace_all:
            return ToolResult.error(
                ErrorCode.AMBIGUOUS_MATCH,
                f"old_string appears {count} times in {path}.",
                hint="Add surrounding context to make it unique, or set replace_all=true.",
                match_count=count,
            )
        new_text = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
        p.write_text(new_text, encoding="utf-8")
        delta = len(new_text) - len(text)
        n_repl = count if replace_all else 1
        return ToolResult.success(
            f"OK: edited {path} ({n_repl} replacement(s), delta {delta:+d} chars)",
            path=str(p),
            replacements=n_repl,
            delta=delta,
            files_touched=[str(p)],
        )
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"edit_file failed: {e}")


def glob_files(pattern: str, path: str = ".") -> ToolResult:
    try:
        base = Path(path)
        if not base.exists():
            return ToolResult.error(ErrorCode.FILE_NOT_FOUND, f"Path not found: {path}")
        matches = [p for p in base.glob(pattern) if p.is_file()]
        if not matches:
            return ToolResult.success(f"No files matching '{pattern}' under {path}", count=0)
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        out = [str(p) for p in matches[:200]]
        header = f"Found {len(matches)} file(s), showing {len(out)}:\n"
        suffix = "" if len(matches) <= 200 else f"\n... ({len(matches) - 200} more)"
        return ToolResult.success(header + "\n".join(out) + suffix, count=len(matches))
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"glob_files failed: {e}")


def grep_file(path: str, pattern: str) -> ToolResult:
    try:
        p = Path(path)
        if not p.exists():
            return ToolResult.error(ErrorCode.FILE_NOT_FOUND, f"File not found: {path}")
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return ToolResult.error(ErrorCode.INVALID_ARGS, f"Invalid regex '{pattern}': {e}", retryable=False)
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        results = []
        for i, line in enumerate(lines, 1):
            if rx.search(line):
                results.append(f"L{i:4d}: {line}")
        if not results:
            return ToolResult.success(f"No matches for '{pattern}' in {path}", count=0)
        header = f"Found {len(results)} match(es) in {path}:\n"
        if len(results) > 100:
            body = header + "\n".join(results[:100]) + f"\n... ({len(results) - 100} more matches)"
        else:
            body = header + "\n".join(results)
        return ToolResult.success(body, count=len(results))
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"grep_file failed: {e}")


# ───────────────────────────────────────────────────────────────────────
# WEB TOOLS
# ───────────────────────────────────────────────────────────────────────

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _strip_html(html: str, max_chars: int = 6000) -> str:
    for tag in ("style", "script", "nav", "footer", "header", "aside"):
        html = re.sub(rf"<{tag}[^>]*>.*?</{tag}>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(
        r"<br\s*/?>|</?p[^>]*>|<li[^>]*>|<h[1-6][^>]*>|<div[^>]*>",
        "\n", html, flags=re.IGNORECASE,
    )
    html = re.sub(r"<[^>]+>", "", html)
    for entity, char in [("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                         ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")]:
        html = html.replace(entity, char)
    html = re.sub(r"&#?\w+;", "", html)
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n{3,}", "\n\n", html)
    text = html.strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n... [truncated — use fetch_url on a more specific URL]"
    return text


def fetch_url(url: str) -> ToolResult:
    # SSRF guard — reject non-http(s) schemes + cloud-metadata range before
    # the outbound request is even built. See core/security.py.
    try:
        from core.security import guard_url_or_result
        bad = guard_url_or_result(url)
        if bad is not None:
            return bad
    except Exception:
        pass
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            ct = resp.headers.get("Content-Type", "")
            raw = resp.read()
            charset = "utf-8"
            m = re.search(r"charset=([^\s;\"']+)", ct)
            if m:
                charset = m.group(1).strip()
            html = raw.decode(charset, errors="replace")
        body = _strip_html(html)
        return ToolResult.success(body, url=url, bytes_read=len(raw))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return ToolResult.error(ErrorCode.RATE_LIMITED, f"HTTP 429 from {url}")
        return ToolResult.error(ErrorCode.UNKNOWN, f"HTTP {e.code} fetching {url}: {e}")
    except urllib.error.URLError as e:
        return ToolResult.error(ErrorCode.EXTERNAL_TIMEOUT, f"Network error fetching {url}: {e}")
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"fetch_url failed: {e}")


def query_rag(query: str, top_k: int = 3, min_score: float = 0.4) -> ToolResult:
    """Agentic RAG retrieval — first-class tool the agent CHOOSES to call.

    Replaces the legacy "auto-inject into system prompt for cad-rag"
    pattern. The agent calls this when it judges retrieval would help;
    otherwise it answers from base knowledge. Returns the top chunks
    with score, source file, and snippet. NO_DATA if everything's below
    `min_score` — the model is told to admit it instead of hallucinate.
    """
    try:
        from core.query import query_rag as _query
    except Exception as e:  # noqa: BLE001 — chromadb optional install path
        return ToolResult.error(
            ErrorCode.UNKNOWN,
            f"RAG backend unavailable: {e}",
            hint="Make sure cad_db/ is populated. Run `python -m scripts.update_rag`.",
            retryable=False,
        )

    try:
        resp = _query(query, n_results=max(1, min(int(top_k or 3), 10)))
    except FileNotFoundError as e:
        return ToolResult.error(
            ErrorCode.FILE_NOT_FOUND,
            f"RAG store missing: {e}",
            hint="Index the docs first (scripts/update_rag.py).",
            retryable=False,
        )
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"query_rag failed: {e}")

    docs = resp.get("documents", [[]])[0] or []
    metas = resp.get("metadatas", [[]])[0] or []
    dists = resp.get("distances", [[]])[0] or []

    if not docs:
        return ToolResult.success(
            f"NO_DATA — RAG store had no matches for: {query!r}",
            count=0, query=query,
        )

    lines = [f"RAG results for: {query!r} (top {len(docs)}, min_score={min_score:.2f})", ""]
    kept = 0
    best_score = 0.0
    for doc, meta, dist in zip(docs, metas, dists):
        score = 1.0 - float(dist)
        best_score = max(best_score, score)
        if score < float(min_score):
            continue
        kept += 1
        meta = meta or {}
        src = meta.get("source") or meta.get("file_name") or "(unknown)"
        section = meta.get("semantic_type") or ""
        lines.append(f"[{kept}] score={score:.3f} | {src}" + (f" | {section}" if section else ""))
        body = doc.strip()
        if len(body) > 800:
            body = body[:800] + "…"
        lines.append(body)
        lines.append("")

    if kept == 0:
        return ToolResult.success(
            f"NO_DATA — all matches below score {min_score:.2f}. Best was {best_score:.2f}. "
            f"Answer from base knowledge or say you don't know — DO NOT cite this corpus.",
            count=0, query=query, best_score=best_score,
        )

    return ToolResult.success(
        "\n".join(lines).rstrip(),
        count=kept, query=query, best_score=best_score,
    )


def search_web(query: str) -> ToolResult:
    """DuckDuckGo HTML search — no API key needed."""
    try:
        encoded = urllib.parse.quote_plus(query)
        url = f"https://html.duckduckgo.com/html/?q={encoded}&kl=wt-wt"
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        results = []
        title_pattern = re.compile(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
        snippet_pattern = re.compile(r'class="result__snippet"[^>]*>(.*?)</(?:span|a|div)', re.DOTALL)
        titles = title_pattern.findall(html)
        snippets = snippet_pattern.findall(html)

        for i, (url_r, title_r) in enumerate(titles[:6]):
            title_r = re.sub(r"<[^>]+>", "", title_r).strip()
            snippet_r = ""
            if i < len(snippets):
                snippet_r = re.sub(r"<[^>]+>", "", snippets[i]).strip()
            if url_r.startswith("//duckduckgo.com/l/"):
                m = re.search(r"uddg=([^&]+)", url_r)
                if m:
                    url_r = urllib.parse.unquote(m.group(1))
            results.append(f"{i+1}. {title_r}\n   URL: {url_r}\n   {snippet_r}")

        if results:
            return ToolResult.success("\n\n".join(results), count=len(results), query=query)

        # Fallback: grab any https links with readable text
        links = re.findall(r'href="(https?://[^"]{10,})"[^>]*>([^<]{5,60})', html)
        links = [(u, t) for u, t in links if "duckduckgo.com" not in u]
        if links:
            body = "\n".join(f"{i}. {t.strip()} — {u}" for i, (u, t) in enumerate(links[:6], 1))
            return ToolResult.success(body, count=len(links), query=query, fallback=True)

        return ToolResult.success("No results found. Try fetch_url with a specific documentation URL instead.", count=0, query=query)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return ToolResult.error(ErrorCode.RATE_LIMITED, f"HTTP 429 from DuckDuckGo")
        return ToolResult.error(ErrorCode.UNKNOWN, f"HTTP {e.code} during search: {e}")
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"search_web failed: {e}")


# ───────────────────────────────────────────────────────────────────────
# Permission-gated wrappers (called by tool_schema's Tool wrappers)
# ───────────────────────────────────────────────────────────────────────


def _gated_write_file(a: Dict[str, Any]) -> ToolResult:
    from core.permissions import request_permission
    path = a["path"]
    content = a.get("content", "") or ""
    if len(content) > _WRITE_FILE_MAX_CHARS:
        return ToolResult.error(
            ErrorCode.TOO_LARGE,
            (
                f"write_file content too large ({len(content)} chars > "
                f"{_WRITE_FILE_MAX_CHARS}). Local models cannot reliably emit JSON this long."
            ),
            hint=(
                "Chunk: 1) write_file with the first ~2500 chars; "
                "2) edit_file to append the next chunk; repeat."
            ),
            retryable=False,
            attempted_chars=len(content),
        )
    preview = content[:300]
    decision = request_permission(
        "write_file",
        path,
        {"path": path, "bytes": len(content), "preview": preview},
    )
    if not decision["allowed"]:
        return ToolResult.error(
            ErrorCode.PERMISSION_DENIED,
            f"User declined write_file for {path} ({decision['reason']}).",
            retryable=False,
        )
    return write_file(path, content)


def _gated_run_command(a: Dict[str, Any]) -> ToolResult:
    from core.permissions import request_permission
    cmd = a["command"]
    cwd = a.get("cwd")
    decision = request_permission("run_command", cmd, {"command": cmd, "cwd": cwd})
    if not decision["allowed"]:
        return ToolResult.error(
            ErrorCode.PERMISSION_DENIED,
            f"User declined run_command `{cmd}` ({decision['reason']}).",
            retryable=False,
        )
    return run_command(cmd, cwd)


def _gated_edit_file(a: Dict[str, Any]) -> ToolResult:
    from core.permissions import request_permission
    path = a["path"]
    old_s = a.get("old_string", "")
    new_s = a.get("new_string", "")
    replace_all = bool(a.get("replace_all", False))
    decision = request_permission("edit_file", path, {
        "path": path,
        "old_preview": old_s[:200],
        "new_preview": new_s[:200],
        "replace_all": replace_all,
    })
    if not decision["allowed"]:
        return ToolResult.error(
            ErrorCode.PERMISSION_DENIED,
            f"User declined edit_file for {path} ({decision['reason']}).",
            retryable=False,
        )
    return edit_file(path, old_s, new_s, replace_all)


# ───────────────────────────────────────────────────────────────────────
# Legacy dict-of-callables (still used by web-creep / browser-agent paths)
# ───────────────────────────────────────────────────────────────────────


FILE_TOOLS = {
    "read_file":   lambda a: read_file(a["path"]),
    "write_file":  _gated_write_file,
    "edit_file":   _gated_edit_file,
    "list_dir":    lambda a: list_dir(a.get("path", ".")),
    "run_command": _gated_run_command,
    "grep_file":   lambda a: grep_file(a["path"], a["pattern"]),
    "glob_files":  lambda a: glob_files(a["pattern"], a.get("path", ".")),
}

WEB_TOOLS = {
    "search_web": lambda a: search_web(a["query"]),
    "fetch_url":  lambda a: fetch_url(a["url"]),
    "read_file":  lambda a: read_file(a["path"]),
}

ALL_TOOLS = {**FILE_TOOLS, **WEB_TOOLS}


def _get_browser_tools():
    """Deferred import — heavy deps (sqlite3, win32) stay off the hot path."""
    from browser_tools import BROWSER_TOOLS  # type: ignore[import-not-found]
    return BROWSER_TOOLS
