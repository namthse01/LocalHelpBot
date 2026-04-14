"""
Document-format tools.

Text:
  read_file_chunk  — read arbitrarily large text files by byte offset / line range.
                     Works for .py, .cs, .js, .ts, .md, logs — any UTF-8 text.

PDF (needs `pypdf`):
  read_pdf         — extract text from a PDF, optional page range.
  write_pdf        — create a simple text PDF (via `reportlab`).

DOCX (needs `python-docx`):
  read_docx        — extract text from a .docx.
  write_docx       — create a .docx from plain text (paragraph per line).

If the optional library is missing, the tool returns an ERROR with a clear
hint to call `install_package` — the agent's recovery-hint layer will pick it
up automatically.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from core.permissions import request_permission
from core.tool_schema import Tool, ToolRegistry


# ──────────────────────────────────────────
#  Big text file reader
# ──────────────────────────────────────────

def _read_file_chunk(args: Dict[str, Any]) -> str:
    path = args.get("path")
    if not path:
        return "ERROR: read_file_chunk requires 'path'."
    mode = (args.get("mode") or "lines").lower()
    p = Path(path)
    if not p.exists():
        return f"ERROR: File not found: {path}"
    try:
        if mode == "bytes":
            offset = int(args.get("offset", 0))
            limit = int(args.get("limit", 200_000))
            with p.open("rb") as f:
                f.seek(offset)
                data = f.read(limit)
            text = data.decode("utf-8", errors="replace")
            total = p.stat().st_size
            return (f"[{path} bytes {offset}..{offset + len(data)} / {total}]\n{text}")
        # default: line mode
        start = int(args.get("start", 1))
        count = int(args.get("count", 500))
        lines: List[str] = []
        total = 0
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                total = i
                if i < start:
                    continue
                if i >= start + count:
                    # keep counting total
                    continue
                lines.append(f"{i:6d}: {line.rstrip()}")
        body = "\n".join(lines) if lines else "(no lines in range)"
        end = min(start + count - 1, total)
        return f"[{path} lines {start}..{end} of {total}]\n{body}"
    except Exception as e:
        return f"ERROR: {e}"


# ──────────────────────────────────────────
#  PDF
# ──────────────────────────────────────────

def _import_or_hint(module: str, package: str, purpose: str):
    try:
        return __import__(module), None
    except ImportError:
        hint = (f"ERROR: ModuleNotFoundError: No module named '{module}'. "
                f"Call install_package with name=\"{package}\" and "
                f"reason=\"{purpose}\" to add it.")
        return None, hint


def _read_pdf(args: Dict[str, Any]) -> str:
    path = args.get("path")
    if not path:
        return "ERROR: read_pdf requires 'path'."
    p = Path(path)
    if not p.exists():
        return f"ERROR: File not found: {path}"
    pypdf, err = _import_or_hint("pypdf", "pypdf", "read PDF files")
    if err:
        return err
    try:
        reader = pypdf.PdfReader(str(p))
        n = len(reader.pages)
        pages_arg = args.get("pages")
        if pages_arg:
            # "1-3" or "2,5,7" or "1-3,7"
            wanted = set()
            for part in str(pages_arg).split(","):
                part = part.strip()
                if "-" in part:
                    a, b = part.split("-", 1)
                    wanted.update(range(int(a), int(b) + 1))
                elif part:
                    wanted.add(int(part))
            idxs = sorted(i for i in wanted if 1 <= i <= n)
        else:
            idxs = list(range(1, min(n, 20) + 1))  # default: first 20 pages
        out = [f"[PDF {path}: {n} pages, showing {len(idxs)}]"]
        for i in idxs:
            try:
                text = reader.pages[i - 1].extract_text() or "(empty)"
            except Exception as e:
                text = f"(extract failed: {e})"
            out.append(f"\n--- page {i} ---\n{text}")
        return "\n".join(out)
    except Exception as e:
        return f"ERROR: {e}"


def _write_pdf(args: Dict[str, Any]) -> str:
    path = args.get("path")
    content = args.get("content", "")
    if not path:
        return "ERROR: write_pdf requires 'path'."
    decision = request_permission(
        "write_pdf", path,
        {"path": path, "chars": len(content), "preview": content[:300]},
    )
    if not decision["allowed"]:
        return f"PERMISSION_DENIED: user declined write_pdf ({decision['reason']})."

    # Prefer reportlab (true PDF). Fall back to hint.
    rl, err = _import_or_hint("reportlab", "reportlab", "generate PDF files")
    if err:
        return err
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        from reportlab.lib.units import mm
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        c = canvas.Canvas(str(p), pagesize=A4)
        width, height = A4
        margin = 20 * mm
        y = height - margin
        line_h = 14
        c.setFont("Helvetica", 10)
        for raw_line in content.splitlines() or [""]:
            # simple word-wrap to page width
            max_chars = 95
            while len(raw_line) > max_chars:
                c.drawString(margin, y, raw_line[:max_chars])
                raw_line = raw_line[max_chars:]
                y -= line_h
                if y < margin:
                    c.showPage()
                    c.setFont("Helvetica", 10)
                    y = height - margin
            c.drawString(margin, y, raw_line)
            y -= line_h
            if y < margin:
                c.showPage()
                c.setFont("Helvetica", 10)
                y = height - margin
        c.save()
        return f"OK: wrote PDF {path} ({len(content)} chars)"
    except Exception as e:
        return f"ERROR: {e}"


# ──────────────────────────────────────────
#  DOCX
# ──────────────────────────────────────────

def _read_docx(args: Dict[str, Any]) -> str:
    path = args.get("path")
    if not path:
        return "ERROR: read_docx requires 'path'."
    p = Path(path)
    if not p.exists():
        return f"ERROR: File not found: {path}"
    docx, err = _import_or_hint("docx", "python-docx", "read Word .docx files")
    if err:
        return err
    try:
        d = docx.Document(str(p))
        paras = [para.text for para in d.paragraphs]
        text = "\n".join(paras)
        if len(text) > 60_000:
            text = text[:60_000] + "\n... [truncated]"
        return f"[DOCX {path}: {len(paras)} paragraphs]\n{text}"
    except Exception as e:
        return f"ERROR: {e}"


def _write_docx(args: Dict[str, Any]) -> str:
    path = args.get("path")
    content = args.get("content", "")
    if not path:
        return "ERROR: write_docx requires 'path'."
    decision = request_permission(
        "write_docx", path,
        {"path": path, "chars": len(content), "preview": content[:300]},
    )
    if not decision["allowed"]:
        return f"PERMISSION_DENIED: user declined write_docx ({decision['reason']})."
    docx, err = _import_or_hint("docx", "python-docx", "write Word .docx files")
    if err:
        return err
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        d = docx.Document()
        for line in content.splitlines() or [""]:
            d.add_paragraph(line)
        d.save(str(p))
        return f"OK: wrote DOCX {path} ({len(content)} chars, {content.count(chr(10))+1} paragraphs)"
    except Exception as e:
        return f"ERROR: {e}"


# ──────────────────────────────────────────
#  Register
# ──────────────────────────────────────────

def register(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="read_file_chunk",
        description=(
            "Read a portion of a large text file. "
            "mode='lines' (default): uses start/count. "
            "mode='bytes': uses offset/limit. "
            "Works for any UTF-8 text (.py, .cs, .js, .ts, .md, logs, ...)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "mode": {"type": "string", "description": "'lines' or 'bytes' (default 'lines')"},
                "start": {"type": "integer", "description": "1-indexed line start (lines mode)"},
                "count": {"type": "integer", "description": "Number of lines (lines mode, default 500)"},
                "offset": {"type": "integer", "description": "Byte offset (bytes mode)"},
                "limit": {"type": "integer", "description": "Max bytes (bytes mode, default 200000)"},
            },
            "required": ["path"],
        },
        handler=_read_file_chunk,
        category="fs",
    ))
    registry.register(Tool(
        name="read_pdf",
        description=(
            "Extract text from a PDF. Optional 'pages' like '1-3' or '2,5,7'. "
            "Default: first 20 pages. Requires `pypdf` (install via install_package if missing)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "pages": {"type": "string", "description": "e.g. '1-5' or '2,7,9-11'"},
            },
            "required": ["path"],
        },
        handler=_read_pdf,
        category="doc",
    ))
    registry.register(Tool(
        name="write_pdf",
        description=(
            "Create a text PDF (uses reportlab). Asks user permission. "
            "Requires `reportlab` — agent should install_package if missing."
        ),
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
        handler=_write_pdf,
        requires_permission=True,
        category="doc",
    ))
    registry.register(Tool(
        name="read_docx",
        description="Read text from a Word .docx file. Requires `python-docx`.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        handler=_read_docx,
        category="doc",
    ))
    registry.register(Tool(
        name="write_docx",
        description=(
            "Create a Word .docx from plain text (one paragraph per line). "
            "Asks user permission. Requires `python-docx`."
        ),
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
        handler=_write_docx,
        requires_permission=True,
        category="doc",
    ))
