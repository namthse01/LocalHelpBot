"""Powerful web / resource tools — Slice 3 of the v4 upgrade.

All tools return typed `ToolResult`. Network ops use stdlib `urllib`
(no extra deps) with a 15s default timeout. Anonymous GitHub API access
is rate-limited to 60 req/h; setting `GITHUB_TOKEN` in the env bumps
it to 5000/h.

Tools:
  download_file        — HTTP GET to a local path (gated, 50 MB cap default).
  extract_text         — fetch URL + readability-style extraction.
  github_search_repos  — list top public repos matching a query.
  github_read_file     — read a file from owner/repo at HEAD or a ref.
  github_releases      — latest 5 releases for a repo.
  pypi_search          — heuristic package search via the JSON+search index.
  pypi_info            — full metadata for a PyPI package.
  youtube_transcript   — auto transcript for a YouTube URL (optional dep).
  wikipedia_summary    — first-paragraph summary from Wikipedia REST API.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from core.permissions import request_permission
from core.tool_schema import ErrorCode, Tool, ToolRegistry, ToolResult


_DEFAULT_TIMEOUT = 15
_DOWNLOAD_MAX_BYTES = 50 * 1024 * 1024
_GH_BASE = "https://api.github.com"
_PYPI_JSON = "https://pypi.org/pypi/{name}/json"
_PYPI_SEARCH_INDEX = "https://pypi.org/simple/"

_HEADERS_HTML = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_HEADERS_API = {
    "User-Agent": "LocalHelpBot/1.0 (+https://github.com/)",
    "Accept": "application/vnd.github+json",
}


def _gh_headers() -> Dict[str, str]:
    h = dict(_HEADERS_API)
    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def _http_get(url: str, *, headers: Optional[Dict[str, str]] = None, timeout: int = _DEFAULT_TIMEOUT) -> tuple[int, bytes, str]:
    req = urllib.request.Request(url, headers=headers or _HEADERS_HTML)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read(), r.headers.get("Content-Type", "")


def _http_json(url: str, *, headers: Optional[Dict[str, str]] = None, timeout: int = _DEFAULT_TIMEOUT) -> tuple[int, Any]:
    status, body, _ct = _http_get(url, headers=headers, timeout=timeout)
    try:
        return status, json.loads(body)
    except json.JSONDecodeError:
        return status, None


# ───────────────────────────────────────────────────────────────────────
# download_file
# ───────────────────────────────────────────────────────────────────────


def _download_file(args: Dict[str, Any]) -> ToolResult:
    url = (args.get("url") or "").strip()
    path = (args.get("path") or "").strip()
    max_bytes = int(args.get("max_bytes") or _DOWNLOAD_MAX_BYTES)
    if not url or not path:
        return ToolResult.error(
            ErrorCode.INVALID_ARGS,
            "download_file requires both 'url' and 'path'.",
            retryable=False,
        )
    if not re.match(r"^https?://", url):
        return ToolResult.error(ErrorCode.INVALID_ARGS, "url must start with http:// or https://", retryable=False)

    decision = request_permission(
        "download_file", url,
        {"url": url, "path": path, "max_bytes": max_bytes},
    )
    if not decision["allowed"]:
        return ToolResult.error(
            ErrorCode.PERMISSION_DENIED,
            f"User declined download_file ({decision['reason']}).",
            retryable=False,
        )

    try:
        req = urllib.request.Request(url, headers=_HEADERS_HTML)
        with urllib.request.urlopen(req, timeout=60) as r:
            total = 0
            dest = Path(path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as out:
                while True:
                    chunk = r.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        out.close()
                        dest.unlink(missing_ok=True)
                        return ToolResult.error(
                            ErrorCode.TOO_LARGE,
                            f"download_file exceeded max_bytes={max_bytes} (got {total}).",
                            hint="Raise max_bytes or pick a smaller URL.",
                            retryable=False,
                        )
                    out.write(chunk)
        return ToolResult.success(
            f"OK: downloaded {total} bytes -> {dest}",
            path=str(dest.resolve()),
            bytes_written=total,
            files_touched=[str(dest.resolve())],
        )
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return ToolResult.error(ErrorCode.RATE_LIMITED, f"HTTP 429 from {url}")
        return ToolResult.error(ErrorCode.UNKNOWN, f"HTTP {e.code} downloading {url}: {e}")
    except urllib.error.URLError as e:
        return ToolResult.error(ErrorCode.EXTERNAL_TIMEOUT, f"Network error: {e}")
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"download_file failed: {e}")


# ───────────────────────────────────────────────────────────────────────
# extract_text — readability-style extraction
# ───────────────────────────────────────────────────────────────────────


_NOISE_TAGS = ("style", "script", "nav", "footer", "header", "aside", "form", "iframe", "noscript")
_TITLE_RE = re.compile(r"<title[^>]*>([\s\S]*?)</title>", re.IGNORECASE)
_META_DESC_RE = re.compile(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)', re.IGNORECASE)
_ARTICLE_RE = re.compile(r"<(?:article|main)[^>]*>([\s\S]+?)</(?:article|main)>", re.IGNORECASE)


def _readability(html: str, max_chars: int = 8000) -> Dict[str, str]:
    """Hand-rolled extractor — drops nav/footer/script, prefers <article>/<main>.

    Returns dict with title, byline (best-effort), text.
    """
    title_m = _TITLE_RE.search(html)
    title = re.sub(r"\s+", " ", title_m.group(1)).strip() if title_m else ""
    desc_m = _META_DESC_RE.search(html)
    byline = re.sub(r"\s+", " ", desc_m.group(1)).strip() if desc_m else ""

    article_m = _ARTICLE_RE.search(html)
    body_html = article_m.group(1) if article_m else html

    for t in _NOISE_TAGS:
        body_html = re.sub(rf"<{t}[^>]*>.*?</{t}>", " ", body_html, flags=re.DOTALL | re.IGNORECASE)
    # Convert block breaks to newlines for paragraph structure.
    body_html = re.sub(r"<br\s*/?>|</?p[^>]*>|<li[^>]*>|<h[1-6][^>]*>|<div[^>]*>", "\n", body_html, flags=re.IGNORECASE)
    body_html = re.sub(r"<[^>]+>", "", body_html)
    for entity, char in [("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")]:
        body_html = body_html.replace(entity, char)
    body_html = re.sub(r"&#?\w+;", "", body_html)
    body_html = re.sub(r"[ \t]+", " ", body_html)
    body_html = re.sub(r"\n{3,}", "\n\n", body_html).strip()
    if len(body_html) > max_chars:
        body_html = body_html[:max_chars] + "\n\n…[truncated; raise max_chars or use download_file for full content]"
    return {"title": title, "byline": byline, "text": body_html}


def _extract_text(args: Dict[str, Any]) -> ToolResult:
    url = (args.get("url") or "").strip()
    max_chars = int(args.get("max_chars") or 8000)
    if not url:
        return ToolResult.error(ErrorCode.INVALID_ARGS, "extract_text requires 'url'.", retryable=False)
    try:
        status, raw, ct = _http_get(url)
        charset = "utf-8"
        m = re.search(r"charset=([^\s;\"']+)", ct or "")
        if m:
            charset = m.group(1).strip()
        html = raw.decode(charset, errors="replace")
        info = _readability(html, max_chars=max_chars)
        body = (
            (f"# {info['title']}\n\n" if info["title"] else "")
            + (f"_{info['byline']}_\n\n" if info["byline"] else "")
            + info["text"]
        )
        return ToolResult.success(body, url=url, title=info["title"])
    except urllib.error.HTTPError as e:
        return ToolResult.error(ErrorCode.UNKNOWN, f"HTTP {e.code} for {url}: {e}")
    except urllib.error.URLError as e:
        return ToolResult.error(ErrorCode.EXTERNAL_TIMEOUT, f"Network error: {e}")
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"extract_text failed: {e}")


# ───────────────────────────────────────────────────────────────────────
# GitHub
# ───────────────────────────────────────────────────────────────────────


def _github_search_repos(args: Dict[str, Any]) -> ToolResult:
    q = (args.get("query") or "").strip()
    if not q:
        return ToolResult.error(ErrorCode.INVALID_ARGS, "github_search_repos requires 'query'.", retryable=False)
    sort = args.get("sort") or "stars"
    url = f"{_GH_BASE}/search/repositories?q={urllib.parse.quote(q)}&sort={sort}&per_page=10"
    try:
        status, data = _http_json(url, headers=_gh_headers())
        if status == 403:
            return ToolResult.error(ErrorCode.RATE_LIMITED, "GitHub rate-limited (60/h anonymous). Set GITHUB_TOKEN env var to get 5000/h.")
        if status != 200 or not isinstance(data, dict):
            return ToolResult.error(ErrorCode.UNKNOWN, f"GitHub returned status {status}.")
        items = data.get("items") or []
        if not items:
            return ToolResult.success(f"NO_DATA — no GitHub repos matched {q!r}", count=0)
        lines = [f"Top {len(items)} repos for {q!r}:", ""]
        for r in items:
            lines.append(
                f"- **{r['full_name']}** ★{r.get('stargazers_count', 0):,} — {r.get('description') or '(no description)'}\n"
                f"  {r['html_url']}"
            )
        return ToolResult.success("\n".join(lines), count=len(items), query=q)
    except urllib.error.HTTPError as e:
        return ToolResult.error(ErrorCode.UNKNOWN, f"HTTP {e.code} searching GitHub: {e}")
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"github_search_repos failed: {e}")


def _github_read_file(args: Dict[str, Any]) -> ToolResult:
    spec = (args.get("path") or "").strip()
    ref = (args.get("ref") or "HEAD").strip()
    parts = spec.split("/", 2)
    if len(parts) < 3:
        return ToolResult.error(
            ErrorCode.INVALID_ARGS,
            "github_read_file 'path' must be 'owner/repo/path/in/repo'.",
            retryable=False,
        )
    owner, repo, file_path = parts
    raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{file_path}"
    try:
        status, raw, _ct = _http_get(raw_url)
        if status == 404:
            return ToolResult.error(ErrorCode.FILE_NOT_FOUND, f"GitHub: {raw_url} not found.")
        if status != 200:
            return ToolResult.error(ErrorCode.UNKNOWN, f"GitHub returned status {status}.")
        text = raw.decode("utf-8", errors="replace")
        return ToolResult.success(text[:20_000] + ("…[truncated]" if len(text) > 20_000 else ""),
                                  repo=f"{owner}/{repo}", path=file_path, ref=ref, size=len(text))
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"github_read_file failed: {e}")


def _github_releases(args: Dict[str, Any]) -> ToolResult:
    spec = (args.get("repo") or "").strip()
    if "/" not in spec:
        return ToolResult.error(ErrorCode.INVALID_ARGS, "github_releases 'repo' must be 'owner/repo'.", retryable=False)
    url = f"{_GH_BASE}/repos/{spec}/releases?per_page=5"
    try:
        status, data = _http_json(url, headers=_gh_headers())
        if status == 404:
            return ToolResult.error(ErrorCode.FILE_NOT_FOUND, f"Repo {spec} not found.")
        if status == 403:
            return ToolResult.error(ErrorCode.RATE_LIMITED, "GitHub rate-limited. Set GITHUB_TOKEN to lift.")
        if status != 200 or not isinstance(data, list):
            return ToolResult.error(ErrorCode.UNKNOWN, f"GitHub returned status {status}.")
        if not data:
            return ToolResult.success(f"NO_DATA — {spec} has no releases.", count=0)
        lines = [f"Latest releases for {spec}:", ""]
        for rel in data:
            published = (rel.get("published_at") or "")[:10]
            tag = rel.get("tag_name") or "?"
            name = rel.get("name") or tag
            url = rel.get("html_url") or ""
            lines.append(f"- **{tag}** ({published}) — {name}\n  {url}")
        return ToolResult.success("\n".join(lines), count=len(data), repo=spec)
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"github_releases failed: {e}")


# ───────────────────────────────────────────────────────────────────────
# PyPI
# ───────────────────────────────────────────────────────────────────────


def _pypi_search(args: Dict[str, Any]) -> ToolResult:
    q = (args.get("query") or "").strip().lower()
    if not q:
        return ToolResult.error(ErrorCode.INVALID_ARGS, "pypi_search requires 'query'.", retryable=False)
    try:
        # pypi.org/simple is a flat HTML index of all package names.
        # Heuristic: pull the index and substring-match. Cached on first call.
        global _pypi_index
        if "_pypi_index" not in globals() or _pypi_index is None:
            _, raw, _ = _http_get(_PYPI_SEARCH_INDEX, timeout=20)
            _pypi_index = re.findall(r'<a[^>]+>([^<]+)</a>', raw.decode("utf-8", errors="replace"))
        matches = [n for n in _pypi_index if q in n.lower()][:12]
        if not matches:
            return ToolResult.success(f"NO_DATA — no PyPI packages contain {q!r}", count=0)
        return ToolResult.success(
            f"PyPI matches for {q!r}:\n\n" + "\n".join(f"- {n}" for n in matches),
            count=len(matches),
        )
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"pypi_search failed: {e}")


_pypi_index = None  # populated lazily by _pypi_search


def _pypi_info(args: Dict[str, Any]) -> ToolResult:
    name = (args.get("name") or "").strip()
    if not name:
        return ToolResult.error(ErrorCode.INVALID_ARGS, "pypi_info requires 'name'.", retryable=False)
    try:
        url = _PYPI_JSON.format(name=urllib.parse.quote(name))
        status, data = _http_json(url, headers={"Accept": "application/json", "User-Agent": _HEADERS_API["User-Agent"]})
        if status == 404:
            return ToolResult.error(ErrorCode.FILE_NOT_FOUND, f"PyPI: package {name!r} not found.")
        if status != 200 or not isinstance(data, dict):
            return ToolResult.error(ErrorCode.UNKNOWN, f"PyPI returned status {status}.")
        info = data.get("info") or {}
        urls_map = info.get("project_urls") or {}
        body_lines = [
            f"# {info.get('name', name)} ({info.get('version', '?')})",
            "",
            info.get("summary") or "(no summary)",
            "",
            f"- requires_python: {info.get('requires_python') or '?'}",
            f"- license: {info.get('license') or '?'}",
            f"- author: {info.get('author') or '?'}",
        ]
        if urls_map:
            body_lines.append("- project urls:")
            for k, v in urls_map.items():
                body_lines.append(f"    - {k}: {v}")
        body_lines.extend([
            "",
            f"Install: `pip install {info.get('name', name)}`",
        ])
        return ToolResult.success("\n".join(body_lines), name=info.get("name", name), version=info.get("version"))
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"pypi_info failed: {e}")


# ───────────────────────────────────────────────────────────────────────
# YouTube transcript (optional dep)
# ───────────────────────────────────────────────────────────────────────


_YT_ID_RE = re.compile(r"(?:v=|/)([A-Za-z0-9_-]{11})(?:[?&].*)?$")


def _youtube_transcript(args: Dict[str, Any]) -> ToolResult:
    url = (args.get("url") or "").strip()
    if not url:
        return ToolResult.error(ErrorCode.INVALID_ARGS, "youtube_transcript requires 'url'.", retryable=False)
    m = _YT_ID_RE.search(url)
    if not m:
        return ToolResult.error(ErrorCode.INVALID_ARGS, f"Could not extract video id from {url!r}.", retryable=False)
    vid = m.group(1)
    try:
        from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore
    except ImportError:
        return ToolResult.error(
            ErrorCode.INVALID_ARGS,
            "youtube-transcript-api is not installed.",
            hint="Call install_package with name='youtube-transcript-api' and a reason.",
            retryable=False,
        )
    try:
        items = YouTubeTranscriptApi.get_transcript(vid)
        text = " ".join(it.get("text", "") for it in items)
        if len(text) > 12_000:
            text = text[:12_000] + "\n\n…[truncated]"
        return ToolResult.success(f"# YouTube transcript {vid}\n\n{text}", video_id=vid, segments=len(items))
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"youtube_transcript failed: {e}")


# ───────────────────────────────────────────────────────────────────────
# Wikipedia
# ───────────────────────────────────────────────────────────────────────


def _wikipedia_summary(args: Dict[str, Any]) -> ToolResult:
    title = (args.get("title") or "").strip()
    lang = (args.get("lang") or "en").strip()
    if not title:
        return ToolResult.error(ErrorCode.INVALID_ARGS, "wikipedia_summary requires 'title'.", retryable=False)
    url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(title)}"
    try:
        status, data = _http_json(url, headers={"Accept": "application/json", "User-Agent": _HEADERS_API["User-Agent"]})
        if status == 404:
            return ToolResult.error(ErrorCode.FILE_NOT_FOUND, f"Wikipedia: no page titled {title!r} ({lang}).")
        if status != 200 or not isinstance(data, dict):
            return ToolResult.error(ErrorCode.UNKNOWN, f"Wikipedia returned status {status}.")
        extract = data.get("extract") or ""
        page_url = (data.get("content_urls") or {}).get("desktop", {}).get("page") or ""
        body = f"# {data.get('title', title)}\n\n{extract}\n\n_source: {page_url}_"
        return ToolResult.success(body, title=data.get("title"), lang=lang, url=page_url)
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(ErrorCode.UNKNOWN, f"wikipedia_summary failed: {e}")


# ───────────────────────────────────────────────────────────────────────
# Register
# ───────────────────────────────────────────────────────────────────────


def register(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="download_file",
        description="HTTP GET a URL to a local file. Asks permission. Default max 50 MB.",
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "path": {"type": "string", "description": "Destination file path"},
                "max_bytes": {"type": "integer", "description": "Hard cap on bytes (default 50000000)"},
            },
            "required": ["url", "path"],
        },
        handler=_download_file,
        requires_permission=True,
        category="web",
    ))
    registry.register(Tool(
        name="extract_text",
        description=(
            "Fetch a URL and run readability-style extraction (drop nav/footer/ads, "
            "keep article body). Returns clean markdown-friendly text with the page title."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "max_chars": {"type": "integer", "description": "Truncate body at N chars (default 8000)"},
            },
            "required": ["url"],
        },
        handler=_extract_text,
        category="web",
    ))
    registry.register(Tool(
        name="github_search_repos",
        description="Search public GitHub repos. Returns top 10 with name/stars/desc/URL.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "sort": {"type": "string", "description": "'stars' (default), 'forks', or 'updated'"},
            },
            "required": ["query"],
        },
        handler=_github_search_repos,
        category="web",
    ))
    registry.register(Tool(
        name="github_read_file",
        description=(
            "Read a file from a public GitHub repo. 'path' format: owner/repo/path/in/repo. "
            "'ref' is a branch/tag/SHA (default HEAD)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "ref":  {"type": "string"},
            },
            "required": ["path"],
        },
        handler=_github_read_file,
        category="web",
    ))
    registry.register(Tool(
        name="github_releases",
        description="Latest 5 releases for a repo. 'repo' = owner/repo.",
        input_schema={
            "type": "object",
            "properties": {"repo": {"type": "string"}},
            "required": ["repo"],
        },
        handler=_github_releases,
        category="web",
    ))
    registry.register(Tool(
        name="pypi_search",
        description="Find PyPI packages whose name contains the query string. Quick scan over the simple index.",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        handler=_pypi_search,
        category="web",
    ))
    registry.register(Tool(
        name="pypi_info",
        description="Full PyPI metadata for a package — version, license, summary, project URLs.",
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        handler=_pypi_info,
        category="web",
    ))
    registry.register(Tool(
        name="youtube_transcript",
        description=(
            "Pull the auto-generated transcript for a YouTube video URL. "
            "Requires `youtube-transcript-api` (call install_package if missing)."
        ),
        input_schema={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        handler=_youtube_transcript,
        category="web",
    ))
    registry.register(Tool(
        name="wikipedia_summary",
        description=(
            "Fetch the first-paragraph summary for a Wikipedia page. "
            "'lang' defaults to 'en'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "lang":  {"type": "string"},
            },
            "required": ["title"],
        },
        handler=_wikipedia_summary,
        category="web",
    ))
