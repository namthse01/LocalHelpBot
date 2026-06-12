"""Notes tools — the agent's interface to the Keep-style NotesStore.

  add_note            — create a note or a checklist (to-do list).
  list_notes          — list / filter / search saved notes.
  update_note         — edit a note's fields (title, content, items, label…).
  complete_note_item  — tick (or untick) a checklist item.
  delete_note         — remove a note.

These are explicit agent actions (like Compare) — there is no auto-injection
and no feature flag. The same store backs the **Notes** UI tab and the
``/api/notes`` proxy routes. odysseus "tasks" are scheduled jobs (already
covered by TheAgent0's Daily Tasks), so a checklist note IS the to-do list.
See [[core/notes.py]].
"""
from __future__ import annotations

from typing import Any, Dict, List

from core.notes import CHECKLIST, get_notes_store
from core.tool_schema import ErrorCode, Tool, ToolRegistry, ToolResult


def _as_str_list(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    # tolerate a newline/semicolon string
    return [s.strip() for s in str(v).replace(";", "\n").splitlines() if s.strip()]


def _summary(note) -> str:
    """One-line human summary of a note for tool output."""
    kind = "checklist" if note.note_type == CHECKLIST else "note"
    bits = [f"[{note.id}] {kind}: {note.title or '(untitled)'}"]
    if note.note_type == CHECKLIST and note.items:
        bits.append(f"({note.done_items()}/{len(note.items)} done)")
    if note.label:
        bits.append(f"#{note.label}")
    if note.pinned:
        bits.append("📌")
    if note.archived:
        bits.append("(archived)")
    return " ".join(bits)


def _add_note(args: Dict[str, Any]) -> ToolResult:
    title = (args.get("title") or "").strip()
    content = (args.get("content") or args.get("body") or "").strip()
    items = _as_str_list(args.get("items") or args.get("checklist"))
    if not title and not content and not items:
        return ToolResult.error(
            ErrorCode.INVALID_ARGS,
            "add_note needs at least a 'title', 'content', or 'items'.",
            retryable=False,
        )
    note = get_notes_store().add(
        title=title,
        content=content,
        items=items,
        note_type=(args.get("note_type") or None),
        label=(args.get("label") or "").strip(),
        due_date=(args.get("due_date") or "").strip(),
        pinned=bool(args.get("pinned") or False),
        source="agent",
    )
    return ToolResult.success(f"OK: saved {_summary(note)}", note_id=note.id)


def _list_notes(args: Dict[str, Any]) -> ToolResult:
    store = get_notes_store()
    notes = store.list(
        include_archived=bool(args.get("include_archived") or False),
        label=(args.get("label") or None),
        query=(args.get("query") or None),
    )
    limit = int(args.get("limit") or 50)
    notes = notes[: max(1, limit)]
    if not notes:
        return ToolResult.success("NO_DATA: no notes match.")
    lines = [f"{len(notes)} note(s):"]
    for n in notes:
        lines.append("  • " + _summary(n))
    return ToolResult.success("\n".join(lines), count=len(notes))


def _update_note(args: Dict[str, Any]) -> ToolResult:
    note_id = (args.get("id") or args.get("note_id") or "").strip()
    if not note_id:
        return ToolResult.error(ErrorCode.INVALID_ARGS, "update_note requires 'id'.", retryable=False)
    fields: Dict[str, Any] = {}
    if "title" in args:
        fields["title"] = args.get("title")
    if "content" in args or "body" in args:
        fields["content"] = args.get("content") or args.get("body")
    if "items" in args or "checklist" in args:
        fields["items"] = _as_str_list(args.get("items") or args.get("checklist"))
    if "label" in args:
        fields["label"] = args.get("label")
    if "due_date" in args:
        fields["due_date"] = args.get("due_date")
    if "pinned" in args:
        fields["pinned"] = bool(args.get("pinned"))
    if "archived" in args:
        fields["archived"] = bool(args.get("archived"))
    if not fields:
        return ToolResult.error(
            ErrorCode.INVALID_ARGS,
            "update_note needs at least one field to change "
            "(title/content/items/label/due_date/pinned/archived).",
            retryable=False,
        )
    note = get_notes_store().update(note_id, **fields)
    if note is None:
        return ToolResult.error(ErrorCode.FILE_NOT_FOUND, f"No note with id '{note_id}'.", retryable=False)
    return ToolResult.success(f"OK: updated {_summary(note)}", note_id=note.id)


def _complete_note_item(args: Dict[str, Any]) -> ToolResult:
    note_id = (args.get("id") or args.get("note_id") or "").strip()
    if not note_id:
        return ToolResult.error(ErrorCode.INVALID_ARGS, "complete_note_item requires 'id'.", retryable=False)
    store = get_notes_store()
    index = args.get("index")
    # Allow ticking by item text when no index is given.
    if index is None:
        text = (args.get("text") or args.get("item") or "").strip().lower()
        if not text:
            return ToolResult.error(
                ErrorCode.INVALID_ARGS,
                "complete_note_item needs an 'index' (0-based) or matching 'text'.",
                retryable=False,
            )
        note = store.get(note_id)
        if note is None:
            return ToolResult.error(ErrorCode.FILE_NOT_FOUND, f"No note with id '{note_id}'.", retryable=False)
        index = next((i for i, it in enumerate(note.items)
                      if text in str(it.get("text", "")).lower()), None)
        if index is None:
            return ToolResult.error(
                ErrorCode.NO_RESULTS, f"No checklist item matching '{text}'.", retryable=False)
    try:
        index = int(index)
    except (TypeError, ValueError):
        return ToolResult.error(ErrorCode.INVALID_ARGS, "'index' must be an integer.", retryable=False)
    note = store.toggle_item(note_id, index)
    if note is None:
        return ToolResult.error(
            ErrorCode.INVALID_ARGS,
            f"Note '{note_id}' has no checklist item at index {index}.",
            retryable=False,
        )
    state = "done" if note.items[index].get("done") else "open"
    return ToolResult.success(
        f"OK: item {index} ('{note.items[index]['text']}') is now {state}. "
        f"{note.done_items()}/{len(note.items)} done.",
        note_id=note.id,
    )


def _delete_note(args: Dict[str, Any]) -> ToolResult:
    note_id = (args.get("id") or args.get("note_id") or "").strip()
    if not note_id:
        return ToolResult.error(ErrorCode.INVALID_ARGS, "delete_note requires 'id'.", retryable=False)
    ok = get_notes_store().delete(note_id)
    if not ok:
        return ToolResult.error(ErrorCode.FILE_NOT_FOUND, f"No note with id '{note_id}'.", retryable=False)
    return ToolResult.success(f"OK: deleted note '{note_id}'.")


def register(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="add_note",
        description=(
            "Save a NOTE or a CHECKLIST (to-do list) for the user. Use for things "
            "to remember, jot down, or track as tasks. Provide 'title' and either "
            "'content' (free text) OR 'items' (a list of checklist entries). Add "
            "'label' to group it and 'due_date' if it's time-sensitive. NOT for "
            "scheduled/recurring automation — that's Daily Tasks."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short note title."},
                "content": {"type": "string", "description": "Free-text body (for a plain note)."},
                "items": {"type": "array", "items": {"type": "string"},
                          "description": "Checklist entries — makes this a to-do list."},
                "label": {"type": "string", "description": "Optional group/tag."},
                "due_date": {"type": "string", "description": "Optional due date (free-form/ISO)."},
                "pinned": {"type": "boolean"},
            },
        },
        handler=_add_note,
        category="notes",
    ))
    registry.register(Tool(
        name="list_notes",
        description=(
            "List the user's notes and checklists. Optional 'query' searches "
            "title/content/items; 'label' filters by group; 'include_archived' "
            "to also show archived notes. Returns id + summary for each."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "label": {"type": "string"},
                "include_archived": {"type": "boolean"},
                "limit": {"type": "integer"},
            },
        },
        handler=_list_notes,
        category="notes",
    ))
    registry.register(Tool(
        name="update_note",
        description=(
            "Edit an existing note by 'id'. Change any of: title, content, items "
            "(replaces the checklist), label, due_date, pinned, archived. Get the "
            "id from list_notes first."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Note id (from list_notes)."},
                "title": {"type": "string"},
                "content": {"type": "string"},
                "items": {"type": "array", "items": {"type": "string"}},
                "label": {"type": "string"},
                "due_date": {"type": "string"},
                "pinned": {"type": "boolean"},
                "archived": {"type": "boolean"},
            },
            "required": ["id"],
        },
        handler=_update_note,
        category="notes",
    ))
    registry.register(Tool(
        name="complete_note_item",
        description=(
            "Tick (or untick) a checklist item on a note — marks a to-do done. "
            "Give the note 'id' and either the 0-based 'index' or the item 'text' "
            "to match. Toggles the current state."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Note id."},
                "index": {"type": "integer", "description": "0-based item index."},
                "text": {"type": "string", "description": "Item text to match (if no index)."},
            },
            "required": ["id"],
        },
        handler=_complete_note_item,
        category="notes",
    ))
    registry.register(Tool(
        name="delete_note",
        description="Delete a note by 'id'. Use when a note is finished or obsolete.",
        input_schema={
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
        handler=_delete_note,
        category="notes",
    ))
