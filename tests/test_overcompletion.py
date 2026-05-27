"""Tests for the over-completion guard: _expects_write should fire ONLY
when the user genuinely asked for a file, not when they used the verb
"write" in the "compose" sense.

The bug we're guarding against: a user types
    "write introduce a person name: Nam in english and japanese language"
and the bot creates introduction_english.txt + introduction_japanese.txt
that the user never asked for.

These tests recreate the regex from run_agent and check both the failing
prompt and a battery of look-alikes.
"""
from __future__ import annotations

import re
import pytest


# Mirror the exact regex used by core/agent.py:run_agent at the time of
# writing. Keep this in sync — if the production regex changes, update
# this fixture (and the assertions) deliberately.
def _expects_write(first_user: str) -> bool:
    return bool(
        re.search(
            r"\b(write|save|create|export|output|summariz\w+|report|generate|ghi|lưu|tạo|xuất)\b"
            r".{0,60}(\.(?:pdf|docx|doc|md|txt|csv|json|html|xml|ya?ml)\b"
            r"|\binto\s+(?:a\s+)?(?:file\b|\.?(?:pdf|docx|md|txt|csv|json|html|xml|ya?ml)\b))",
            first_user, re.IGNORECASE | re.DOTALL,
        )
        or bool(re.search(
            r"\b(save|export|output|lưu|ghi|xuất)\s+(to|as|into|vào|vô)\b",
            first_user, re.IGNORECASE,
        ))
        or bool(re.search(
            r"\binto\s+\w+\.(?:pdf|docx|doc|md|txt|csv|json|html|xml|ya?ml)\b",
            first_user, re.IGNORECASE,
        ))
    )


# ───────────────────────────────────────────────────────────────────────
# Should NOT trigger write-expectation — chat answers only.
# ───────────────────────────────────────────────────────────────────────

CHAT_ONLY_PROMPTS = [
    # The exact failing prompt the user reported.
    "write introduce a person name: Nam in english and japanese language",
    # Common "write me X" phrasings.
    "write me a poem about autumn",
    "write a haiku",
    "write a short story about a dragon",
    "please write a long story for me about a dragon",
    "write me an introduction",
    # Compose synonyms.
    "compose a story",
    "draft a report",
    "describe a sunset in 3 sentences",
    "explain how a transformer works",
    "introduce yourself",
    "tell me about black holes",
    "summarize the theory of relativity",  # has 'summariz' but no extension/into-file
    # Vietnamese without explicit file.
    "viết một bài thơ",
    "viết giới thiệu một người tên Nam",
    "kể chuyện về một con rồng",
    "miêu tả hoàng hôn",
    # Mentions a non-file word that contains 'to' — must not falsely match.
    "write to the user",
    "write to me",
]


@pytest.mark.parametrize("prompt", CHAT_ONLY_PROMPTS)
def test_expects_write_is_false_for_chat_only_prompts(prompt):
    assert not _expects_write(prompt), (
        f"_expects_write fired on a chat-only prompt — bot will be nudged "
        f"to create surprise files. Prompt: {prompt!r}"
    )


# ───────────────────────────────────────────────────────────────────────
# Should trigger write-expectation — file explicitly requested.
# ───────────────────────────────────────────────────────────────────────

FILE_REQUEST_PROMPTS = [
    # Explicit extension at the target — verb anchored.
    "save that to notes.md",
    "write a report into report.pdf",
    "summarize into summary.docx",
    "write the answer to result.txt",
    "create a config.json with these values",
    "export the data to data.csv",
    # 'into <ext>' / 'into a file'.
    "summarize this PDF into a .md file",
    "draft an outline into outline.md",
    # Vietnamese verb + extension.
    "ghi vào notes.txt",
    "lưu vào tho.md",
    "save vô notes.md",
    # Pure 'save/export <to|as>' adjacency — still a file signal.
    "save to disk",
    "export as csv",
]


# Cases the regex DOES NOT catch — that's fine. They are ambiguous and the
# CRITICAL RULE in the system prompt makes the model ASK the user. We
# don't want false positives that nudge the bot into creating surprise
# files.
AMBIGUOUS_PROMPTS_THAT_FALL_THROUGH = [
    "save the result to my_notes",       # no extension, no immediate save+to
    "viết một bài thơ vào tho.txt",      # "viết" isn't in the verb list
    "read guide.pdf for me",             # READ verb, not write — must not fire
]


@pytest.mark.parametrize("prompt", AMBIGUOUS_PROMPTS_THAT_FALL_THROUGH)
def test_ambiguous_or_read_prompts_do_not_fire(prompt):
    """Cases where _expects_write deliberately returns False: ambiguous
    (no extension), or referring to an existing file the user wants to
    READ. The CRITICAL RULE in the system prompt handles these — the
    heuristic stays conservative so we don't nudge file creation."""
    assert not _expects_write(prompt), (
        f"_expects_write fired on an ambiguous/read-only prompt: {prompt!r}"
    )


@pytest.mark.parametrize("prompt", FILE_REQUEST_PROMPTS)
def test_expects_write_is_true_for_explicit_file_requests(prompt):
    assert _expects_write(prompt), (
        f"_expects_write missed a legitimate file request — bot won't be "
        f"nudged to write the file the user asked for. Prompt: {prompt!r}"
    )


# ───────────────────────────────────────────────────────────────────────
# System-prompt smoke tests — confirm the new guardrail language is
# actually in the main profile's system_prompt string.
# ───────────────────────────────────────────────────────────────────────

def test_system_prompt_contains_no_unrequested_files_rule():
    from config import AGENT_PROFILES
    sp = AGENT_PROFILES["main"]["system_prompt"]
    assert "DO NOT CREATE FILES THE USER DID NOT ASK FOR" in sp
    assert "Default output is TEXT IN CHAT" in sp


def test_system_prompt_contains_chat_vs_file_examples():
    from config import AGENT_PROFILES
    sp = AGENT_PROFILES["main"]["system_prompt"]
    assert "EXAMPLES — chat vs. file output" in sp
    assert "Write me a poem about autumn" in sp
    assert "Write a haiku to autumn.txt" in sp
    assert "Introduce a person named Nam" in sp


def test_system_prompt_no_longer_auto_fills_directory_filename():
    """The old 'PICK a sensible filename' instruction is removed; replaced
    with 'ASK the user'."""
    from config import AGENT_PROFILES
    sp = AGENT_PROFILES["main"]["system_prompt"]
    assert "PICK a sensible filename" not in sp
    assert "ASK the user\n    for a filename" in sp


def test_prime_directive_clarifies_files_are_not_an_outcome():
    from config import AGENT_PROFILES
    sp = AGENT_PROFILES["main"]["system_prompt"]
    # The phrase spans a line break in the prompt — normalize whitespace
    # before checking so we don't break if the wrap moves.
    flat = " ".join(sp.split())
    assert "Creating files is NOT an outcome" in flat
