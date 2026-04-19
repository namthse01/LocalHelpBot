"""
Tool implementations for agentic proxy.
- File tools: read_file, write_file, list_dir, run_command, grep_file
- Web tools:  search_web, fetch_url
"""

import os
import re
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path


# ──────────────────────────────────────────
# FILE TOOLS
# ──────────────────────────────────────────

def read_file(path: str) -> str:
    try:
        p = Path(path)
        if not p.exists():
            return f"ERROR: File not found: {path}"
        size = p.stat().st_size
        if size > 200_000:
            return f"ERROR: File too large ({size} bytes). Use grep_file to search for specific sections."
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"ERROR: {e}"


def write_file(path: str, content: str) -> str:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"OK: Written {len(content)} chars to {path}"
    except Exception as e:
        return f"ERROR: {e}"


def list_dir(path: str = ".") -> str:
    try:
        p = Path(path)
        if not p.exists():
            return f"ERROR: Path not found: {path}"
        items = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))
        lines = []
        for item in items:
            if item.is_dir():
                lines.append(f"[DIR]  {item.name}/")
            else:
                lines.append(f"[FILE] {item.name} ({item.stat().st_size:,} bytes)")
        return "\n".join(lines) if lines else "(empty)"
    except Exception as e:
        return f"ERROR: {e}"


def run_command(command: str, cwd: str = None) -> str:
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True,
            text=True, timeout=60, cwd=cwd,   # increased from 30s
            encoding="utf-8", errors="replace"
        )
        parts = []
        if result.stdout.strip():
            parts.append(f"STDOUT:\n{result.stdout.strip()}")
        if result.stderr.strip():
            parts.append(f"STDERR:\n{result.stderr.strip()}")
        parts.append(f"EXIT: {result.returncode}")
        return "\n".join(parts)
    except subprocess.TimeoutExpired:
        return "ERROR: Command timed out (60s). Try a faster/simpler command or split into steps."
    except Exception as e:
        return f"ERROR: {e}"


def edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """Exact string replace in a file. old_string must be unique unless replace_all=True."""
    try:
        p = Path(path)
        if not p.exists():
            return f"ERROR: File not found: {path}"
        text = p.read_text(encoding="utf-8", errors="replace")
        if old_string == new_string:
            return "ERROR: old_string and new_string are identical — nothing to do."
        if old_string not in text:
            return f"ERROR: old_string not found in {path}. Read the file first and copy the EXACT text (including whitespace)."
        count = text.count(old_string)
        if count > 1 and not replace_all:
            return f"ERROR: old_string appears {count} times in {path}. Provide more surrounding context to make it unique, or set replace_all=true."
        new_text = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
        p.write_text(new_text, encoding="utf-8")
        delta = len(new_text) - len(text)
        return f"OK: edited {path} ({count if replace_all else 1} replacement(s), delta {delta:+d} chars)"
    except Exception as e:
        return f"ERROR: {e}"


def glob_files(pattern: str, path: str = ".") -> str:
    """Find files matching a glob pattern (e.g. '**/*.py'). Returns up to 200 paths sorted by mtime desc."""
    try:
        base = Path(path)
        if not base.exists():
            return f"ERROR: Path not found: {path}"
        matches = [p for p in base.glob(pattern) if p.is_file()]
        if not matches:
            return f"No files matching '{pattern}' under {path}"
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        out = [str(p) for p in matches[:200]]
        header = f"Found {len(matches)} file(s), showing {len(out)}:\n"
        suffix = "" if len(matches) <= 200 else f"\n... ({len(matches) - 200} more)"
        return header + "\n".join(out) + suffix
    except Exception as e:
        return f"ERROR: {e}"


def grep_file(path: str, pattern: str) -> str:
    """Search for regex pattern in a file. Returns matching lines with line numbers."""
    try:
        p = Path(path)
        if not p.exists():
            return f"ERROR: File not found: {path}"
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return f"ERROR: Invalid regex pattern '{pattern}': {e}"
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        results = []
        for i, line in enumerate(lines, 1):
            if rx.search(line):
                results.append(f"L{i:4d}: {line}")
        if not results:
            return f"No matches for '{pattern}' in {path}"
        header = f"Found {len(results)} match(es) in {path}:\n"
        # Limit output to 100 lines to avoid flooding context
        if len(results) > 100:
            return header + "\n".join(results[:100]) + f"\n... ({len(results) - 100} more matches)"
        return header + "\n".join(results)
    except Exception as e:
        return f"ERROR: {e}"


# ──────────────────────────────────────────
# WEB TOOLS
# ──────────────────────────────────────────

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
    # Remove style/script/nav/footer blocks
    for tag in ("style", "script", "nav", "footer", "header", "aside"):
        html = re.sub(
            rf"<{tag}[^>]*>.*?</{tag}>", " ", html,
            flags=re.DOTALL | re.IGNORECASE
        )
    # Preserve block structure as newlines
    html = re.sub(
        r"<br\s*/?>|</?p[^>]*>|<li[^>]*>|<h[1-6][^>]*>|<div[^>]*>",
        "\n", html, flags=re.IGNORECASE
    )
    # Strip remaining tags
    html = re.sub(r"<[^>]+>", "", html)
    # Decode common entities
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


def fetch_url(url: str) -> str:
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
        return _strip_html(html)
    except Exception as e:
        return f"ERROR fetching {url}: {e}"


def search_web(query: str) -> str:
    """DuckDuckGo HTML search — no API key needed."""
    try:
        encoded = urllib.parse.quote_plus(query)
        url = f"https://html.duckduckgo.com/html/?q={encoded}&kl=wt-wt"
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        results = []

        # Primary extraction: DuckDuckGo result blocks
        # Each result has title link + snippet span
        title_pattern = re.compile(
            r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            re.DOTALL
        )
        snippet_pattern = re.compile(
            r'class="result__snippet"[^>]*>(.*?)</(?:span|a|div)',
            re.DOTALL
        )

        titles   = title_pattern.findall(html)
        snippets = snippet_pattern.findall(html)

        for i, (url_r, title_r) in enumerate(titles[:6]):
            title_r   = re.sub(r"<[^>]+>", "", title_r).strip()
            snippet_r = ""
            if i < len(snippets):
                snippet_r = re.sub(r"<[^>]+>", "", snippets[i]).strip()
            # DuckDuckGo sometimes wraps URLs in redirect — try to unwrap
            if url_r.startswith("//duckduckgo.com/l/"):
                m = re.search(r"uddg=([^&]+)", url_r)
                if m:
                    url_r = urllib.parse.unquote(m.group(1))
            results.append(f"{i+1}. {title_r}\n   URL: {url_r}\n   {snippet_r}")

        if results:
            return "\n\n".join(results)

        # Fallback: grab any https links with readable text
        links = re.findall(r'href="(https?://[^"]{10,})"[^>]*>([^<]{5,60})', html)
        links = [(u, t) for u, t in links if "duckduckgo.com" not in u]
        if links:
            return "\n".join(
                f"{i}. {t.strip()} — {u}"
                for i, (u, t) in enumerate(links[:6], 1)
            )

        return "No results found. Try fetch_url with a specific documentation URL instead."
    except Exception as e:
        return f"ERROR searching: {e}"


# ──────────────────────────────────────────
# TOOL REGISTRY
# ──────────────────────────────────────────

# Beyond this size, local models reliably corrupt the JSON payload (missing
# closing quotes, unescaped newlines). We reject large single writes and
# instruct the model to chunk via write_file + edit_file appends. The agent
# loop's self-heal layer picks up the hint and retries in the chunked shape.
_WRITE_FILE_MAX_CHARS = 3000

def _gated_write_file(a):
    from core.permissions import request_permission
    path = a["path"]
    content = a.get("content", "") or ""
    if len(content) > _WRITE_FILE_MAX_CHARS:
        return (
            f"ERROR: write_file content too large ({len(content)} chars > "
            f"{_WRITE_FILE_MAX_CHARS}). Local models cannot reliably emit JSON "
            f"this long. CHUNK YOUR WRITE:\n"
            f"  1. Call write_file now with ONLY the first ~2500 chars (header "
            f"+ first 1–2 sections).\n"
            f"  2. On the next turn, call edit_file with old_string=\"\" (empty) "
            f"and new_string=<next chunk> — it will append.\n"
            f"     OR use edit_file to replace a placeholder anchor you leave "
            f"at the end of the previous chunk (e.g. '<!-- CONTINUE -->').\n"
            f"  3. Repeat until done.\n"
            f"Do NOT retry with the full content — it will fail the same way."
        )
    preview = content[:300]
    decision = request_permission("write_file", path, {"path": path, "bytes": len(content), "preview": preview})
    if not decision["allowed"]:
        return f"PERMISSION_DENIED: user declined write_file for {path} ({decision['reason']})."
    return write_file(path, content)


def _gated_run_command(a):
    from core.permissions import request_permission
    cmd = a["command"]
    cwd = a.get("cwd")
    decision = request_permission("run_command", cmd, {"command": cmd, "cwd": cwd})
    if not decision["allowed"]:
        return f"PERMISSION_DENIED: user declined run_command `{cmd}` ({decision['reason']})."
    return run_command(cmd, cwd)


def _gated_edit_file(a):
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
        return f"PERMISSION_DENIED: user declined edit_file for {path} ({decision['reason']})."
    return edit_file(path, old_s, new_s, replace_all)


FILE_TOOLS = {
    "read_file":    lambda a: read_file(a["path"]),
    "write_file":   _gated_write_file,
    "edit_file":    _gated_edit_file,
    "list_dir":     lambda a: list_dir(a.get("path", ".")),
    "run_command":  _gated_run_command,
    "grep_file":    lambda a: grep_file(a["path"], a["pattern"]),
    "glob_files":   lambda a: glob_files(a["pattern"], a.get("path", ".")),
}

WEB_TOOLS = {
    "search_web":   lambda a: search_web(a["query"]),
    "fetch_url":    lambda a: fetch_url(a["url"]),
    # web-creep can also read local files for context
    "read_file":    lambda a: read_file(a["path"]),
}

ALL_TOOLS = {**FILE_TOOLS, **WEB_TOOLS}

# Lazy import to avoid heavy deps at proxy startup
def _get_browser_tools():
    from browser_tools import BROWSER_TOOLS
    return BROWSER_TOOLS
