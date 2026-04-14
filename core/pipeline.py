"""
Input preprocessing pipeline — openclaw-inspired lifecycle stages.

Stages implemented in phase A (pure, zero LLM cost):
  1. normalize   — strip BOM/zero-width, unify quotes/newlines
  2. extract     — pull structured facts (paths, URLs, filenames, verbs, lang)
  3. assemble    — layer a richer system prompt from base role + extracted facts
                   + an explicit output contract (path fidelity, JSON escaping).

`prepare(messages, base_system)` is the single entry point — returns a new
message list with an enriched system message. Non-destructive: user messages
are preserved verbatim. Safe to call on any message list; if the last user
message has no extractable facts, the output contract alone still helps.

Phase B (classify + plan via one LLM call) is deferred.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List


# ── Regex kit ────────────────────────────────────────────────────
# Windows path: drive letter + separators. Anchored on drive prefix so we
# don't eat normal English words. Trailing quotes/punct are trimmed later.
_WIN_PATH = re.compile(
    r'[A-Za-z]:[\\/](?:[^<>:"|?*\r\n]+?)'
    r'(?=[\s"\'`)>\]]|$)'
)
# Unix path: needs at least one slash in the middle (avoids matching "/").
_UNIX_PATH = re.compile(
    r'(?<![\w/])(?:/[A-Za-z0-9._\-]+){2,}/?'
)
_URL = re.compile(r'https?://[^\s<>"\'`]+', re.I)
_FILENAME = re.compile(
    r'\b([A-Za-z0-9_\-.]+\.'
    r'(?:md|pdf|docx?|txt|csv|tsv|json|html?|xml|ya?ml|py|js|ts|tsx|jsx|'
    r'cs|cpp|cc|c|hpp|hh|h|rs|go|java|kt|swift|rb|php|sh|ps1|sql|log|ini|toml))\b',
    re.I,
)
_VERB = re.compile(
    r'\b(read|write|save|create|summariz\w*|search|fix|edit|delete|remove|'
    r'update|list|show|find|grep|scrape|fetch|download|translate|convert|'
    r'build|run|execute|test|analyze|refactor|'
    r'ghi|lưu|tạo|xuất|đọc|tìm|viết|sửa|xóa)\b',
    re.I,
)
_VI_DIACRITIC = re.compile(
    r'[ăâđêôơưĂÂĐÊÔƠƯàáạảãầấậẩẫèéẹẻẽềếệểễìíịỉĩ'
    r'òóọỏõồốộổỗờớợởỡùúụủũừứựửữỳýỵỷỹ]'
)


# ── Data shape ───────────────────────────────────────────────────

@dataclass
class Extracted:
    paths: List[str] = field(default_factory=list)
    urls: List[str] = field(default_factory=list)
    filenames: List[str] = field(default_factory=list)
    verbs: List[str] = field(default_factory=list)
    language: str = "en"  # 'en' | 'vi'


# ── Stages ───────────────────────────────────────────────────────

def normalize(text: str) -> str:
    if not text:
        return ""
    t = (text
         .replace("\ufeff", "").replace("\u200b", "")
         .replace("\u201c", '"').replace("\u201d", '"')
         .replace("\u2018", "'").replace("\u2019", "'")
         .replace("\r\n", "\n").replace("\r", "\n"))
    return t.strip()


def _trim(p: str) -> str:
    """Strip trailing punctuation that regex greedily swept up."""
    return p.strip().strip('"\'`.,;:)]>')


def extract(text: str) -> Extracted:
    ex = Extracted()
    for m in _WIN_PATH.finditer(text):
        p = _trim(m.group(0))
        if p and p not in ex.paths:
            ex.paths.append(p)
    for m in _UNIX_PATH.finditer(text):
        p = _trim(m.group(0))
        if p and p not in ex.paths:
            ex.paths.append(p)
    for m in _URL.finditer(text):
        u = _trim(m.group(0))
        if u and u not in ex.urls:
            ex.urls.append(u)
    for m in _FILENAME.finditer(text):
        fn = m.group(1)
        if fn not in ex.filenames and not any(fn in p for p in ex.paths):
            ex.filenames.append(fn)
    for m in _VERB.finditer(text):
        v = m.group(1).lower()
        if v not in ex.verbs:
            ex.verbs.append(v)
    ex.language = "vi" if _VI_DIACRITIC.search(text) else "en"
    return ex


# Output contract: opinionated guardrails for the model. Keeps it honest on
# the two issues we've actually seen: path drift, and broken tool_use JSON.
_OUTPUT_CONTRACT = (
    "OUTPUT CONTRACT (follow strictly):\n"
    "  • If the user specified a file path or filename, use it EXACTLY.\n"
    "    Do NOT rename, relocate, or invent a different directory.\n"
    "  • For tool calls, emit: <tool_use>{\"name\":\"...\",\"input\":{...}}</tool_use>\n"
    "    Inside JSON strings: newlines MUST be \\n, quotes \\\", backslashes \\\\.\n"
    "  • For long content, split across multiple tool calls (write_file once,\n"
    "    then edit_file to append) rather than one huge fragile JSON string.\n"
    "  • If the task needs a file output, you MUST end with a successful\n"
    "    write_* call — do not summarize in chat only."
)


def assemble_prompt(base_system: str, extracted: Extracted) -> str:
    blocks = []
    if base_system and base_system.strip():
        blocks.append(base_system.strip())

    facts = []
    if extracted.paths:
        facts.append("PATHS (use EXACTLY — preserve drive, directory, filename):")
        facts.extend(f"  • {p}" for p in extracted.paths)
    if extracted.urls:
        facts.append("URLS:")
        facts.extend(f"  • {u}" for u in extracted.urls)
    if extracted.filenames:
        facts.append(f"FILENAMES MENTIONED: {', '.join(extracted.filenames)}")
    if extracted.verbs:
        facts.append(f"USER INTENT VERBS: {', '.join(extracted.verbs)}")
    if extracted.language == "vi":
        facts.append("USER LANGUAGE: Vietnamese — prefer Vietnamese in the final reply.")

    if facts:
        blocks.append("=== EXTRACTED CONTEXT ===\n" + "\n".join(facts)
                      + "\n=== END CONTEXT ===")

    blocks.append(_OUTPUT_CONTRACT)
    return "\n\n".join(blocks)


# ── Entry point ──────────────────────────────────────────────────

def _last_user_text(messages: list) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content", "")
            if isinstance(c, list):
                return " ".join(p.get("text", "") for p in c if p.get("type") == "text")
            return str(c)
    return ""


def prepare(messages: list, base_system: str) -> list:
    """Return a new message list with an enriched system message.

    Behavior:
      • If an existing `system` message is present, it is REPLACED with the
        assembled prompt (base_system is used as the role instructions).
      • Otherwise a new system message is prepended.
      • User/assistant messages are passed through untouched.
    """
    if not messages:
        return messages
    raw = _last_user_text(messages)
    extracted = extract(normalize(raw))
    system_content = assemble_prompt(base_system, extracted)

    out = []
    injected = False
    for m in messages:
        if m.get("role") == "system" and not injected:
            out.append({"role": "system", "content": system_content})
            injected = True
        else:
            out.append(m)
    if not injected:
        out.insert(0, {"role": "system", "content": system_content})
    return out
