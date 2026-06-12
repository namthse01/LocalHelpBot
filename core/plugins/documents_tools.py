"""Documents tools — the agent's interface to the living-documents store.

  create_document  — start a new living document (markdown).
  list_documents   — browse / search / sort the document library.
  get_document     — read a document's current content + version info.
  update_document  — save a new revision (auto-coalesced if rapid).
  delete_document  — remove a document.

Explicit agent actions (like Notes / Compare) — no auto-injection and no
feature flag. The same DocumentsStore backs the **Documents** UI tab and the
``/api/documents`` proxy routes. A previous version can be restored from the UI
(or ``POST /api/documents/restore``); the agent edits the live content.
See [[core/documents.py]].
"""
from __future__ import annotations

from typing import Any, Dict

from core.documents import get_documents_store
from core.tool_schema import ErrorCode, Tool, ToolRegistry, ToolResult

# Cap the content echoed back by get_document so a huge doc can't blow the
# agent's context window. The full text is always available in the UI.
_GET_MAX_CHARS = 12000


def _summary(doc) -> str:
    """One-line human summary of a document for tool output."""
    bits = [f"[{doc.id}] {doc.title or '(untitled)'}"]
    bits.append(f"({doc.language}, v{doc.version_count}, {len(doc.content)} chars)")
    if doc.archived:
        bits.append("(archived)")
    return " ".join(bits)


def _create_document(args: Dict[str, Any]) -> ToolResult:
    content = args.get("content") or args.get("body") or ""
    title = (args.get("title") or "").strip()
    if not str(content).strip() and not title:
        return ToolResult.error(
            ErrorCode.INVALID_ARGS,
            "create_document needs 'content' (and optionally a 'title').",
            retryable=False,
        )
    doc = get_documents_store().add(
        title=title,
        content=str(content),
        language=(args.get("language") or "").strip(),
        source="agent",
    )
    return ToolResult.success(f"OK: created {_summary(doc)}", document_id=doc.id)


def _list_documents(args: Dict[str, Any]) -> ToolResult:
    store = get_documents_store()
    docs = store.list(
        include_archived=bool(args.get("include_archived") or False),
        query=(args.get("query") or None),
        language=(args.get("language") or None),
        sort=(args.get("sort") or "recent"),
    )
    limit = int(args.get("limit") or 50)
    docs = docs[: max(1, limit)]
    if not docs:
        return ToolResult.success("NO_DATA: no documents match.")
    lines = [f"{len(docs)} document(s):"]
    for d in docs:
        lines.append("  • " + _summary(d))
    return ToolResult.success("\n".join(lines), count=len(docs))


def _get_document(args: Dict[str, Any]) -> ToolResult:
    doc_id = (args.get("id") or args.get("document_id") or "").strip()
    if not doc_id:
        return ToolResult.error(
            ErrorCode.INVALID_ARGS, "get_document requires 'id'.", retryable=False)
    doc = get_documents_store().get(doc_id)
    if doc is None:
        return ToolResult.error(
            ErrorCode.FILE_NOT_FOUND, f"No document with id '{doc_id}'.", retryable=False)
    content = doc.content or ""
    truncated = len(content) > _GET_MAX_CHARS
    if truncated:
        content = content[:_GET_MAX_CHARS]
    body = f"{_summary(doc)}\n\n{content}"
    if truncated:
        body += (f"\n\n…[truncated at {_GET_MAX_CHARS} chars — "
                 f"full length {len(doc.content)}]")
    return ToolResult.success(
        body, document_id=doc.id, version_count=doc.version_count, truncated=truncated)


def _update_document(args: Dict[str, Any]) -> ToolResult:
    doc_id = (args.get("id") or args.get("document_id") or "").strip()
    if not doc_id:
        return ToolResult.error(
            ErrorCode.INVALID_ARGS, "update_document requires 'id'.", retryable=False)
    if not any(k in args for k in ("content", "body", "title", "language")):
        return ToolResult.error(
            ErrorCode.INVALID_ARGS,
            "update_document needs 'content' (and/or 'title'/'language').",
            retryable=False,
        )
    content = args.get("content")
    if content is None:
        content = args.get("body")
    doc = get_documents_store().update(
        doc_id,
        content=content,
        summary=(args.get("summary") or ""),
        title=args.get("title"),
        language=args.get("language"),
        source="agent",
    )
    if doc is None:
        return ToolResult.error(
            ErrorCode.FILE_NOT_FOUND, f"No document with id '{doc_id}'.", retryable=False)
    return ToolResult.success(
        f"OK: saved {_summary(doc)}", document_id=doc.id, version_count=doc.version_count)


def _delete_document(args: Dict[str, Any]) -> ToolResult:
    doc_id = (args.get("id") or args.get("document_id") or "").strip()
    if not doc_id:
        return ToolResult.error(
            ErrorCode.INVALID_ARGS, "delete_document requires 'id'.", retryable=False)
    ok = get_documents_store().delete(doc_id)
    if not ok:
        return ToolResult.error(
            ErrorCode.FILE_NOT_FOUND, f"No document with id '{doc_id}'.", retryable=False)
    return ToolResult.success(f"OK: deleted document '{doc_id}'.")


def register(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="create_document",
        description=(
            "Start a new LIVING DOCUMENT (markdown) the user can keep editing — "
            "an essay, plan, spec, README, notes page. Provide 'content' (and "
            "optionally a 'title'; it's auto-derived from the first heading/line "
            "otherwise). For a quick reminder or a to-do list use add_note "
            "instead; for writing to a file on disk use write_file."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Optional document title."},
                "content": {"type": "string", "description": "The markdown body."},
                "language": {"type": "string",
                             "description": "Optional format/language label (default markdown)."},
            },
        },
        handler=_create_document,
        category="documents",
    ))
    registry.register(Tool(
        name="list_documents",
        description=(
            "List the user's living documents (the Documents library). Optional "
            "'query' searches title+content (all terms must match); 'language' "
            "filters by format; 'sort' is recent|oldest|edits|alpha; "
            "'include_archived' to also show archived docs. Returns id + summary."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "language": {"type": "string"},
                "sort": {"type": "string",
                         "enum": ["recent", "oldest", "edits", "alpha"]},
                "include_archived": {"type": "boolean"},
                "limit": {"type": "integer"},
            },
        },
        handler=_list_documents,
        category="documents",
    ))
    registry.register(Tool(
        name="get_document",
        description=(
            "Read a document's current content and version info by 'id'. Get the "
            "id from list_documents first. Long documents are truncated in the "
            "returned text (the full content stays intact in the store)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Document id (from list_documents)."},
            },
            "required": ["id"],
        },
        handler=_get_document,
        category="documents",
    ))
    registry.register(Tool(
        name="update_document",
        description=(
            "Save a new revision of an existing document by 'id'. Pass the full "
            "new 'content' (it replaces the current content and is versioned; "
            "rapid re-saves are coalesced). You may also change 'title'/"
            "'language'. Add a short 'summary' to label the revision. Get the id "
            "from list_documents first."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Document id."},
                "content": {"type": "string", "description": "Full new markdown content."},
                "summary": {"type": "string", "description": "Short revision label."},
                "title": {"type": "string"},
                "language": {"type": "string"},
            },
            "required": ["id"],
        },
        handler=_update_document,
        category="documents",
    ))
    registry.register(Tool(
        name="delete_document",
        description="Delete a document by 'id'. Use when it's finished or obsolete.",
        input_schema={
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
        handler=_delete_document,
        category="documents",
    ))
