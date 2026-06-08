"""Micro-compaction of stale tool-result bodies in a long conversation.

Extracted from :mod:`core.agent` (behavior-preserving move — no logic
changes). Replaces old verbose <tool_result> bodies with a placeholder +
short digest once the conversation grows long, keeping the most recent
results intact. Re-exported from ``core.agent``.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List


# Micro-compaction — replace old verbose tool-result bodies with a
# placeholder + digest once the conversation gets long. Keeps the most
# recent results intact.
MICRO_COMPACT_TURNS     = 6
MICRO_COMPACT_MIN_CHARS = 1500
MICRO_COMPACT_KEEP_TAIL = 2
MICRO_COMPACT_PLACEHOLDER = (
    "[Old tool result content cleared for context window — "
    "re-run the tool if you need the full output again.]"
)
MICRO_COMPACT_DIGEST_CHARS = 180


def _extract_digest(body: str, limit: int = MICRO_COMPACT_DIGEST_CHARS) -> str:
    if not body:
        return ""
    text = re.sub(r"\s+", " ", body).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _micro_compact(conversation: List[Dict[str, Any]]) -> int:
    """Replace old verbose tool_result bodies with a placeholder + digest.

    Defense-in-depth: never touch the last assistant message in the
    conversation. The current implementation only mutates user-role
    messages with `<tool_result>` blocks, but if compaction ever extends
    to assistant turns we want to keep the most recent final answer
    around — it's the anchor for "our last result" reference resolution.
    """
    last_assistant_idx = -1
    for i in range(len(conversation) - 1, -1, -1):
        if conversation[i].get("role") == "assistant":
            last_assistant_idx = i
            break
    tool_feedback_indices: List[int] = []
    for i, m in enumerate(conversation):
        if i == last_assistant_idx:
            continue
        if m.get("role") != "user":
            continue
        body = m.get("content", "")
        if isinstance(body, str) and "<tool_result" in body and len(body) > MICRO_COMPACT_MIN_CHARS:
            tool_feedback_indices.append(i)
    to_compact = (
        tool_feedback_indices[:-MICRO_COMPACT_KEEP_TAIL]
        if len(tool_feedback_indices) > MICRO_COMPACT_KEEP_TAIL else []
    )
    compacted = 0
    for idx in to_compact:
        if MICRO_COMPACT_PLACEHOLDER in (conversation[idx].get("content", "") or ""):
            continue
        orig = conversation[idx]["content"]

        def _replace(m: "re.Match") -> str:
            open_tag = m.group(1)
            body = m.group(2)
            close_tag = m.group(3)
            digest = _extract_digest(body)
            digest_line = f"Digest: {digest}" if digest else ""
            return (
                f"{open_tag}\n"
                f"{MICRO_COMPACT_PLACEHOLDER}"
                f"{chr(10) + digest_line if digest_line else ''}\n"
                f"{close_tag}"
            )

        compact_body = re.sub(
            r'(<tool_result[^>]*>)([\s\S]*?)(</tool_result>)',
            _replace,
            orig,
        )
        conversation[idx] = {"role": "user", "content": compact_body}
        compacted += 1
    return compacted
