"""Session memory — MemoryEngine + compatibility shims.

Two things live in this file:

1. `MemoryEngine` — the proper, instance-based API. Owns the summarizer
   prompt, the token-budget heuristic, and the compaction state machine.
   The new context engine (core.context) talks to MemoryEngine directly.

2. A set of module-level functions (`extract_summary`, `merge_summary`,
   `estimate_tokens`, `split_for_compaction`, `build_summarizer_messages`,
   `wrap_summary_output`, `_content_text`, `_looks_like_summary`).
   These existed before the rewrite; many call sites still import them.
   They are kept as thin shims that delegate to the default engine so the
   refactor is non-breaking for callers we haven't touched yet (Discord
   gateway, MCP server, scheduler).

Sentinel strings (`SUMMARY_MARKER`, `SUMMARY_END`) are part of the wire
protocol between the JS client and the proxy — never rename them.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Wire-protocol sentinels (do not rename) ──────────────────────────
SUMMARY_MARKER = "=== CONVERSATION SUMMARY ==="
SUMMARY_END    = "=== END SUMMARY ==="
_LEGACY_PREFIXES = (
    "Prior conversation summary:",
    "Prior conversation summary (updated):",
)

# ── Sizing heuristics ────────────────────────────────────────────────
# English plain prose ≈ 4 chars/token; Vietnamese with diacritics drops
# to ~2.5; mixed code/JSON/tool feedback sits in between. 2.8 is the
# safe floor — we'd rather over-compact than overflow a 4K-8K window.
CHARS_PER_TOKEN = 2.8
DEFAULT_MAX_TOKENS = 6000        # soft budget before compaction fires
DEFAULT_KEEP_RECENT_TURNS = 10   # turns preserved verbatim after compaction


# ───────────────────────────────────────────────────────────────────────
# Pure helpers (no state, no IO) — engine methods call these too
# ───────────────────────────────────────────────────────────────────────

def _content_text(msg: Dict) -> str:
    """Extract plain text from a message whose content may be str or parts list."""
    c = msg.get("content", "")
    if isinstance(c, list):
        return " ".join(
            p.get("text", "")
            for p in c
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return str(c) if c is not None else ""


def _looks_like_summary(text: str) -> bool:
    if not text:
        return False
    head = text.strip()[:120]
    if SUMMARY_MARKER in text:
        return True
    return any(head.startswith(p) for p in _LEGACY_PREFIXES)


# ───────────────────────────────────────────────────────────────────────
# Summarizer prompt (port of claude-code compact.ts, scaled for 7B locals)
# ───────────────────────────────────────────────────────────────────────

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


# ───────────────────────────────────────────────────────────────────────
# MemoryEngine — the real API
# ───────────────────────────────────────────────────────────────────────


@dataclass
class CompactionResult:
    """Outcome of a compaction attempt. Always safe to use — if the
    summarizer call fails we fall back to the original messages."""

    messages: List[Dict]
    summary: str
    fired: bool
    reason: str = ""


@dataclass
class MemoryEngine:
    """Instance-based session memory utilities.

    Default config matches the historical module-level constants so the
    behaviour after the rewrite is unchanged for callers that don't pass
    overrides. Subclasses / tests can pass tighter budgets.
    """

    max_tokens: int = DEFAULT_MAX_TOKENS
    keep_recent_turns: int = DEFAULT_KEEP_RECENT_TURNS
    chars_per_token: float = CHARS_PER_TOKEN

    # Optional summarizer callable: `summarizer(messages) -> response_with_.content`.
    # Default None means callers must pass one to `compact()` — keeps the
    # engine importable without dragging in core.providers (which would
    # create an import cycle with proxy.py).
    summarizer: Optional[object] = field(default=None, repr=False)

    # ── sizing ───────────────────────────────────────────────────────
    def estimate_tokens(self, messages: List[Dict]) -> int:
        total_chars = sum(len(_content_text(m)) for m in messages)
        return int(total_chars / self.chars_per_token)

    def over_budget(self, messages: List[Dict]) -> bool:
        return self.estimate_tokens(messages) > self.max_tokens

    # ── summary extraction / merging ─────────────────────────────────
    def extract_summary(self, messages: List[Dict]) -> str:
        """Concatenate every summary-looking system message into one string."""
        if not messages:
            return ""
        parts: List[str] = []
        for m in messages:
            if m.get("role") != "system":
                continue
            text = _content_text(m)
            if not _looks_like_summary(text):
                continue
            parts.append(text.strip())
        return "\n\n".join(parts)

    def strip_summary_from_system(self, messages: List[Dict]) -> List[Dict]:
        """Drop summary-only system messages, preserve other systems."""
        out = []
        for m in messages:
            if m.get("role") == "system" and _looks_like_summary(_content_text(m)):
                continue
            out.append(m)
        return out

    def merge_summary(self, base_system: str, summary: str) -> str:
        """Append a marker-wrapped summary block to a base system prompt."""
        base = (base_system or "").strip()
        body = (summary or "").strip()
        if not body:
            return base

        # Strip legacy prefixes / existing markers — we wrap freshly.
        for p in _LEGACY_PREFIXES:
            if body.startswith(p):
                body = body[len(p):].strip()
        if SUMMARY_MARKER in body:
            try:
                start = body.index(SUMMARY_MARKER) + len(SUMMARY_MARKER)
                end = body.index(SUMMARY_END, start) if SUMMARY_END in body else len(body)
                body = body[start:end].strip()
            except ValueError:
                pass

        block = f"{SUMMARY_MARKER}\n{body}\n{SUMMARY_END}"
        return f"{base}\n\n{block}" if base else block

    # ── compaction state machine ─────────────────────────────────────
    def split_for_compaction(
        self,
        messages: List[Dict],
        keep_recent_turns: Optional[int] = None,
    ) -> Tuple[List[Dict], List[Dict], List[Dict]]:
        keep_n = keep_recent_turns if keep_recent_turns is not None else self.keep_recent_turns
        system = [m for m in messages if m.get("role") == "system"]
        turns  = [m for m in messages if m.get("role") != "system"]
        if len(turns) <= keep_n:
            return system, [], turns
        cut = len(turns) - keep_n
        return system, turns[:cut], turns[cut:]

    def build_summarizer_messages(self, to_summarize: List[Dict]) -> List[Dict]:
        """Build the [system, user] prompt pair fed to the summarizer model."""
        lines: List[str] = []
        for m in to_summarize:
            role = m.get("role", "user")
            text = _content_text(m)
            if not text:
                continue
            # Tool feedback is verbose and low-signal for a summary.
            if (
                role == "user"
                and ("<tool_result" in text or "tool_result" in text)
                and len(text) > 800
            ):
                text = text[:600] + "\n…[tool result truncated for summary]"
            lines.append(f"{role}: {text}")
        convo = "\n".join(lines)
        return [
            {"role": "system", "content": STRUCTURED_SUMMARY_PROMPT},
            {"role": "user", "content": f"Summarize this conversation:\n\n{convo}"},
        ]

    def wrap_summary_output(self, raw: str) -> str:
        """Normalize summarizer output: drop scratchpads/tags, collapse blanks."""
        text = raw or ""
        text = re.sub(r"<analysis>[\s\S]*?</analysis>", "", text, flags=re.IGNORECASE)
        text = re.sub(r"</?summary>", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def compact(
        self,
        messages: List[Dict],
        *,
        summarizer=None,
        prior_summary: str = "",
    ) -> CompactionResult:
        """Run one compaction cycle if we're over budget.

        Returns a CompactionResult; `fired=False` means the input was
        already under budget or the summarizer call failed (in which
        case we return the original messages unchanged).
        """
        if not messages:
            return CompactionResult(messages=messages, summary=prior_summary, fired=False, reason="empty")

        tokens = self.estimate_tokens(messages)
        if tokens <= self.max_tokens:
            return CompactionResult(messages=messages, summary=prior_summary, fired=False, reason="under_budget")

        system, to_summarize, keep = self.split_for_compaction(messages)
        if not to_summarize:
            return CompactionResult(messages=messages, summary=prior_summary, fired=False, reason="nothing_to_summarize")

        summarizer = summarizer or self.summarizer
        if summarizer is None:
            logger.warning("MemoryEngine.compact() called without a summarizer — skipping")
            return CompactionResult(messages=messages, summary=prior_summary, fired=False, reason="no_summarizer")

        prior = prior_summary or self.extract_summary(system)
        prompt_msgs = self.build_summarizer_messages(to_summarize)
        if prior:
            prompt_msgs[-1]["content"] = (
                f"Prior summary (extend this, do not discard facts):\n{prior}\n\n"
                + prompt_msgs[-1]["content"]
            )

        try:
            resp = summarizer.chat(prompt_msgs, options={"temperature": 0.1, "num_predict": 1024})
        except Exception as e:  # noqa: BLE001 — summarizer is third-party
            logger.warning("MemoryEngine.compact() summarizer call failed: %s", e)
            return CompactionResult(messages=messages, summary=prior_summary, fired=False, reason=f"summarizer_error:{e}")

        summary = self.wrap_summary_output(getattr(resp, "content", "") or "")
        if not summary:
            return CompactionResult(messages=messages, summary=prior_summary, fired=False, reason="empty_summary")

        # Rebuild: non-summary system msgs + new summary system + kept turns.
        non_summary_system = [
            m for m in system
            if not _looks_like_summary(_content_text(m))
        ]
        summary_msg = {
            "role": "system",
            "content": f"{SUMMARY_MARKER}\n{summary}\n{SUMMARY_END}",
        }
        new_messages = non_summary_system + [summary_msg] + keep
        logger.info(
            "MemoryEngine compacted: tokens~%s > %s, summarized %s old turns, kept %s",
            tokens, self.max_tokens, len(to_summarize), len(keep),
        )
        return CompactionResult(messages=new_messages, summary=summary, fired=True, reason="compacted")


# ───────────────────────────────────────────────────────────────────────
# Default engine + module-level shims for backward compatibility
# ───────────────────────────────────────────────────────────────────────

_default_engine = MemoryEngine()


def get_default_engine() -> MemoryEngine:
    return _default_engine


def extract_summary(messages: List[Dict]) -> str:
    return _default_engine.extract_summary(messages)


def strip_summary_from_system(messages: List[Dict]) -> List[Dict]:
    return _default_engine.strip_summary_from_system(messages)


def merge_summary(base_system: str, summary: str) -> str:
    return _default_engine.merge_summary(base_system, summary)


def estimate_tokens(messages: List[Dict]) -> int:
    return _default_engine.estimate_tokens(messages)


def split_for_compaction(
    messages: List[Dict],
    keep_recent_turns: int = DEFAULT_KEEP_RECENT_TURNS,
) -> Tuple[List[Dict], List[Dict], List[Dict]]:
    return _default_engine.split_for_compaction(messages, keep_recent_turns)


def build_summarizer_messages(to_summarize: List[Dict]) -> List[Dict]:
    return _default_engine.build_summarizer_messages(to_summarize)


def wrap_summary_output(raw: str) -> str:
    return _default_engine.wrap_summary_output(raw)
