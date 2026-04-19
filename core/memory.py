"""
Session memory helpers — Claude Code-inspired in-session compaction.

Responsibilities:
  • Preserve "conversation summary" across pipeline / orchestrator / RAG
    rewrites that otherwise drop it by replacing the system message.
  • Estimate conversation size (cheap char/4 token proxy) for server-side
    compaction decisions.
  • Run a structured summarizer (port of claude-code's compact prompt,
    scaled down for local models) when the budget is exceeded.

Design notes
------------
Client and server both mark summary content with SUMMARY_MARKER. Every
place that rebuilds the system prompt must call `merge_summary(base,
existing_system_or_messages)` so the summary carries forward.

The summary lives in a `system` message; we also support multiple system
messages (some client paths emit them). `extract_summary(messages)` walks
all system messages and returns the concatenated summary block (or "").
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Sentinels used to recognize summary content across pipeline boundaries.
# Both are stable strings so client JS and server Python agree without shared
# config. Client prefixes its summarizer output with SUMMARY_MARKER; server
# looks for either marker (legacy + new).
SUMMARY_MARKER = "=== CONVERSATION SUMMARY ==="
SUMMARY_END    = "=== END SUMMARY ==="
_LEGACY_PREFIXES = (
    "Prior conversation summary:",
    "Prior conversation summary (updated):",
)

# Token-proxy: English plain prose is ~4.0 chars/token; Vietnamese with
# diacritics drops to ~2.5-2.8; code/JSON with whitespace is ~3.2. This bot
# sees a mix of all three (Vietnamese chat + English code + tool JSON), so
# 2.8 is the safe floor — we over-compact rather than silently overflow
# Ollama's 4-8K default context window.
CHARS_PER_TOKEN = 2.8
DEFAULT_MAX_TOKENS = 6000      # soft budget before server-side compaction fires
DEFAULT_KEEP_RECENT_TURNS = 10 # turns preserved verbatim after compaction


def _content_text(msg: Dict) -> str:
    """Extract text from a message whose content may be a str or list-of-parts."""
    c = msg.get("content", "")
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
    return str(c) if c is not None else ""


def _looks_like_summary(text: str) -> bool:
    if not text:
        return False
    head = text.strip()[:120]
    if SUMMARY_MARKER in text:
        return True
    return any(head.startswith(p) for p in _LEGACY_PREFIXES)


def extract_summary(messages: List[Dict]) -> str:
    """Pull all summary content out of the system messages.

    Handles both the marker format and the legacy "Prior conversation summary:"
    prefix (what the existing client code produces). Returns "" if none found.
    """
    if not messages:
        return ""
    parts: List[str] = []
    for m in messages:
        if m.get("role") != "system":
            continue
        text = _content_text(m)
        if not _looks_like_summary(text):
            continue
        # Keep the full content so structure (sections, legacy prefix) is intact
        parts.append(text.strip())
    return "\n\n".join(parts)


def strip_summary_from_system(messages: List[Dict]) -> List[Dict]:
    """Return a copy of messages with summary system messages removed.

    Non-summary system messages are preserved. Useful when the caller wants
    to reinject summary via merge_summary() rather than as a standalone msg.
    """
    out = []
    for m in messages:
        if m.get("role") == "system" and _looks_like_summary(_content_text(m)):
            continue
        out.append(m)
    return out


def merge_summary(base_system: str, summary: str) -> str:
    """Append a summary block to a base system prompt. No-op if summary is empty."""
    base = (base_system or "").strip()
    summary = (summary or "").strip()
    if not summary:
        return base
    # Strip any legacy/new markers from the summary body; we wrap freshly.
    body = summary
    for p in _LEGACY_PREFIXES:
        if body.startswith(p):
            body = body[len(p):].strip()
    if SUMMARY_MARKER in body:
        # Extract just the inner block if the client already wrapped it
        try:
            start = body.index(SUMMARY_MARKER) + len(SUMMARY_MARKER)
            end = body.index(SUMMARY_END, start) if SUMMARY_END in body else len(body)
            body = body[start:end].strip()
        except ValueError:
            pass
    block = f"{SUMMARY_MARKER}\n{body}\n{SUMMARY_END}"
    return f"{base}\n\n{block}" if base else block


# ──────────────────────────────────────────────────────────────
#  Budget / sizing
# ──────────────────────────────────────────────────────────────

def estimate_tokens(messages: List[Dict]) -> int:
    """Cheap char/4 token proxy over all message content."""
    total_chars = 0
    for m in messages:
        total_chars += len(_content_text(m))
    return int(total_chars / CHARS_PER_TOKEN)


def split_for_compaction(
    messages: List[Dict],
    keep_recent_turns: int = DEFAULT_KEEP_RECENT_TURNS,
) -> Tuple[List[Dict], List[Dict], List[Dict]]:
    """Split messages into (system_msgs, to_summarize, keep_verbatim).

    `to_summarize` is the oldest N-keep_recent_turns non-system turns,
    `keep_verbatim` is the most recent keep_recent_turns turns.
    """
    system = [m for m in messages if m.get("role") == "system"]
    turns  = [m for m in messages if m.get("role") != "system"]
    if len(turns) <= keep_recent_turns:
        return system, [], turns
    cut = len(turns) - keep_recent_turns
    return system, turns[:cut], turns[cut:]


# ──────────────────────────────────────────────────────────────
#  Structured summarizer prompt (port of claude-code compact.ts,
#  scaled down for 7B-class local models — 8 sections, ≤ ~350 words)
# ──────────────────────────────────────────────────────────────

STRUCTURED_SUMMARY_PROMPT = (
    "You are compressing a chat conversation so the assistant can continue "
    "without losing context. Read the conversation below and emit a summary "
    "with these EXACT sections (keep each short, prefer bullets over prose):\n"
    "\n"
    "1. Primary Request and Intent — what the user is trying to accomplish.\n"
    "2. Key Decisions — choices the assistant and user agreed on.\n"
    "3. Files and Paths — specific file paths, filenames, URLs mentioned. "
    "Preserve EXACT spelling, drive letters, extensions.\n"
    "4. Tools Used and Results — which tools were called, on what targets, "
    "did they succeed or fail? (one line each)\n"
    "5. Errors and Fixes — errors hit and how they were resolved. Include "
    "ANY user corrections verbatim.\n"
    "6. User Feedback — notable instructions/preferences from the user "
    "(e.g. 'don't rename files', 'answer in Vietnamese').\n"
    "7. Pending Tasks — what's still on the todo list.\n"
    "8. Current State — exactly what was being worked on right before this "
    "summary. Include the last user message verbatim.\n"
    "\n"
    "Rules:\n"
    "  • Preserve file paths and filenames EXACTLY — do not rename or relocate.\n"
    "  • If the user wrote in Vietnamese, summarize in Vietnamese.\n"
    "  • Do NOT call any tools. Plain text only.\n"
    "  • Total length: aim for 250-400 words. If the conversation is small, "
    "fewer is fine.\n"
    "  • Output ONLY the 8 sections — no preamble, no sign-off.\n"
)


def build_summarizer_messages(to_summarize: List[Dict]) -> List[Dict]:
    """Build the [system, user] pair for the structured summarizer."""
    # Flatten the slice into plain dialogue text. Tool feedback messages can
    # be huge — truncate tool results only (keep user/assistant prose intact).
    lines = []
    for m in to_summarize:
        role = m.get("role", "user")
        text = _content_text(m)
        if not text:
            continue
        # Tool-result feedback is verbose and low-signal for a summary;
        # keep only the first 600 chars of long user-role feedback.
        if role == "user" and ("<tool_result" in text or "tool_result" in text) and len(text) > 800:
            text = text[:600] + "\n…[tool result truncated for summary]"
        lines.append(f"{role}: {text}")
    convo = "\n".join(lines)
    return [
        {"role": "system", "content": STRUCTURED_SUMMARY_PROMPT},
        {"role": "user", "content": f"Summarize this conversation:\n\n{convo}"},
    ]


def wrap_summary_output(raw: str) -> str:
    """Normalize summarizer output: strip <analysis> scratchpads if any,
    strip surrounding <summary> tags, collapse blank lines."""
    import re
    text = raw or ""
    text = re.sub(r"<analysis>[\s\S]*?</analysis>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</?summary>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
