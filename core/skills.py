"""SkillStore — learned, reusable procedures (self-evolving agent).

Ported from odysseus (`services/memory/skill_format.py`,
`skill_extractor.py`, `skills.py`) and adapted to TheAgent0. This is the
deeper sibling of `core.lessons`:

  • A **lesson** is a one-line correction/preference ("answer in Vietnamese").
  • A **skill** is a structured, *repeatable procedure* the agent can follow
    to solve a similar problem next time ("how to open a PR from a branch":
    when-to-use + numbered steps + pitfalls + verification).

Storage (human-readable, git-friendly, no new dependency):

    data/skills/<slug>/SKILL.md      — YAML frontmatter + markdown body
    data/skills/_usage.json          — sidecar usage counters (uses, last_used)

Skills are surfaced two ways:
  1. **Retrieval into the prompt** — `render_for_prompt(query)` keyword-scores
     skills against the user's latest message and injects the top matches as a
     `<skills>` block in T1 (see core/context.py). Cheap, dependency-free.
  2. **Auto-extraction** — after a complex agent run (≥2 turns or ≥2 tool
     calls), `maybe_extract_skill()` asks the LLM to distill the approach into
     a reusable skill, gated by `config.SKILLS_AUTO_EXTRACT` (default OFF).

The SKILL.md format is the Hermes/Claude "agent skills" shape, so skills you
write by hand (or drop in from elsewhere) round-trip cleanly.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_DIR = Path("data") / "skills"
MAX_SKILLS = 500
MIN_CONFIDENCE = 0.6          # drop auto-extractions the model is unsure about
EXTRACT_CONTEXT_WINDOW = 12   # recent messages fed to the extractor


# ───────────────────────────────────────────────────────────────────────
# Minimal YAML frontmatter (subset — avoids a PyYAML dependency)
# ───────────────────────────────────────────────────────────────────────

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_FM_KEY_RE = re.compile(r"^([a-z_][a-z0-9_]*):\s*(.*)$", re.IGNORECASE)
_FM_BLOCK_LIST_RE = re.compile(r"^\s*-\s*(.*)$")
_STOPWORDS = {
    "the", "a", "an", "to", "of", "and", "or", "for", "in", "on", "with",
    "how", "do", "i", "you", "is", "it", "this", "that", "my", "me", "can",
    "what", "when", "where", "why", "please", "help", "need", "want",
}


def slugify(text: str, fallback: str = "skill") -> str:
    s = _SLUG_RE.sub("-", str(text or "").strip().lower()).strip("-")
    return (s or fallback)[:60]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_scalar(raw: str) -> Any:
    raw = raw.strip()
    if raw == "":
        return ""
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        return [p.strip().strip("'\"") for p in inner.split(",") if p.strip()] if inner else []
    low = raw.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "none", "~"):
        return None
    if raw[0] == raw[-1] and raw[0] in ("'", '"'):
        return raw[1:-1]
    try:
        return float(raw) if "." in raw else int(raw)
    except ValueError:
        return raw


def parse_frontmatter(text: str) -> tuple[Dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    fm_text = text[3:end].lstrip("\n")
    body = text[end + 4:].lstrip("\n")
    fm: Dict[str, Any] = {}
    pending: Optional[str] = None
    for line in fm_text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _FM_KEY_RE.match(line)
        if m:
            key, val = m.group(1), m.group(2)
            if val.strip() == "":
                pending = key
                fm[key] = []
            else:
                fm[key] = _parse_scalar(val)
                pending = None
            continue
        m2 = _FM_BLOCK_LIST_RE.match(line)
        if m2 and pending:
            fm.setdefault(pending, [])
            if isinstance(fm[pending], list):
                fm[pending].append(_parse_scalar(m2.group(1)))
    return fm, body


def _emit_scalar(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_emit_scalar(x) for x in v) + "]"
    s = str(v)
    if any(c in s for c in ":#\n[]{},&*!|>'\"%@"):
        return json.dumps(s, ensure_ascii=False)
    return s


def _as_list(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v if x not in (None, "")]
    return [str(v)]


def _as_float(v: Any, default: float = 0.8) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ───────────────────────────────────────────────────────────────────────
# Body sections
# ───────────────────────────────────────────────────────────────────────

_HEADING_TO_KEY = {
    "when to use": "when_to_use", "procedure": "procedure", "steps": "procedure",
    "pitfalls": "pitfalls", "verification": "verification",
}
_KEY_TO_HEADING = {
    "when_to_use": "When to Use", "procedure": "Procedure",
    "pitfalls": "Pitfalls", "verification": "Verification",
}


def _parse_list_lines(text: str) -> List[str]:
    items: List[str] = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        m = re.match(r"^(?:[-*]|\d+[.)])\s+(.*)$", s)
        if m:
            items.append(m.group(1).strip())
        elif items:
            items[-1] = items[-1] + " " + s
        else:
            items.append(s)
    return items


def parse_body(body: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"when_to_use": "", "procedure": [], "pitfalls": [], "verification": [], "body_extra": ""}
    if not body or not body.strip():
        return out
    sections: List[tuple[Optional[str], List[str]]] = [(None, [])]
    for line in body.splitlines():
        m = re.match(r"^##\s+(.*?)\s*$", line)
        if m:
            sections.append((_HEADING_TO_KEY.get(m.group(1).strip().lower()), []))
            continue
        sections[-1][1].append(line)
    for key, lines in sections:
        text = "\n".join(lines).strip("\n")
        if key is None:
            if text.strip():
                out["body_extra"] = (out["body_extra"] + "\n\n" + text.strip()).strip()
        elif key == "when_to_use":
            out["when_to_use"] = text.strip()
        else:
            out[key] = _parse_list_lines(text)
    return out


def emit_body(s: Dict[str, Any]) -> str:
    parts: List[str] = []
    when = (s.get("when_to_use") or "").strip()
    if when:
        parts.append(f"## {_KEY_TO_HEADING['when_to_use']}\n\n{when}")
    for key in ("procedure", "pitfalls", "verification"):
        items = s.get(key) or []
        if not items:
            continue
        if key == "procedure":
            body = "\n".join(f"{i + 1}. {x}" for i, x in enumerate(items))
        else:
            body = "\n".join(f"- {x}" for x in items)
        parts.append(f"## {_KEY_TO_HEADING[key]}\n\n{body}")
    extra = (s.get("body_extra") or "").strip()
    if extra:
        parts.append(extra)
    return "\n\n".join(parts) + ("\n" if parts else "")


# ───────────────────────────────────────────────────────────────────────
# Skill record
# ───────────────────────────────────────────────────────────────────────


@dataclass
class Skill:
    name: str                                   # slug / dir name / id
    description: str = ""
    category: str = "general"
    tags: List[str] = field(default_factory=list)
    status: str = "published"                    # draft | published
    confidence: float = 0.8
    source: str = "learned"                      # learned | taught | imported
    created: str = ""
    when_to_use: str = ""
    procedure: List[str] = field(default_factory=list)
    pitfalls: List[str] = field(default_factory=list)
    verification: List[str] = field(default_factory=list)
    body_extra: str = ""
    uses: int = 0
    last_used: Optional[float] = None
    path: Optional[str] = None

    def to_frontmatter(self) -> Dict[str, Any]:
        fm: Dict[str, Any] = {"name": self.name, "description": self.description, "category": self.category}
        if self.tags:
            fm["tags"] = list(self.tags)
        fm["status"] = self.status
        fm["confidence"] = round(float(self.confidence), 3)
        fm["source"] = self.source
        fm["created"] = self.created or _now_iso()
        return fm

    def to_markdown(self) -> str:
        fm = "\n".join(f"{k}: {_emit_scalar(v)}" for k, v in self.to_frontmatter().items()
                       if v not in (None, "", []))
        body = emit_body({
            "when_to_use": self.when_to_use, "procedure": self.procedure,
            "pitfalls": self.pitfalls, "verification": self.verification,
            "body_extra": self.body_extra,
        })
        return f"---\n{fm}\n---\n\n{body}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.name, "name": self.name, "description": self.description,
            "category": self.category, "tags": list(self.tags), "status": self.status,
            "confidence": round(float(self.confidence), 3), "source": self.source,
            "created": self.created, "when_to_use": self.when_to_use,
            "procedure": list(self.procedure), "pitfalls": list(self.pitfalls),
            "verification": list(self.verification), "body_extra": self.body_extra,
            "uses": int(self.uses or 0), "last_used": self.last_used, "path": self.path,
        }

    @classmethod
    def from_markdown(cls, text: str, *, path: Optional[str] = None) -> "Skill":
        fm, body = parse_frontmatter(text)
        sec = parse_body(body)
        raw_name = fm.get("name") or fm.get("description") or ""
        return cls(
            name=slugify(raw_name, fallback="skill"),
            description=str(fm.get("description", "") or ""),
            category=str(fm.get("category", "general") or "general"),
            tags=_as_list(fm.get("tags")),
            status=str(fm.get("status", "published") or "published"),
            confidence=_as_float(fm.get("confidence", 0.8), 0.8),
            source=str(fm.get("source", "learned") or "learned"),
            created=str(fm.get("created") or _now_iso()),
            when_to_use=sec["when_to_use"],
            procedure=list(sec["procedure"]),
            pitfalls=list(sec["pitfalls"]),
            verification=list(sec["verification"]),
            body_extra=sec["body_extra"],
            path=path,
        )

    # ── retrieval scoring ────────────────────────────────────────────
    def _haystack(self) -> str:
        return " ".join([
            self.name.replace("-", " "), self.description, self.category,
            " ".join(self.tags), self.when_to_use, " ".join(self.procedure),
        ]).lower()

    def score(self, query_terms: List[str]) -> float:
        if not query_terms:
            return 0.0
        hay = self._haystack()
        hits = sum(1 for t in query_terms if t in hay)
        if not hits:
            return 0.0
        # Weight by term coverage, nudge by the skill's own confidence.
        return (hits / len(query_terms)) * (0.5 + 0.5 * float(self.confidence or 0.5))


def _query_terms(query: str) -> List[str]:
    toks = re.findall(r"[a-zA-Z0-9_]+", (query or "").lower())
    return [t for t in toks if len(t) > 2 and t not in _STOPWORDS]


# ───────────────────────────────────────────────────────────────────────
# Store
# ───────────────────────────────────────────────────────────────────────


class SkillStore:
    """Filesystem-backed skill store (one SKILL.md per skill). Thread-safe."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self._root = Path(root) if root else DEFAULT_DIR
        self._usage_path = self._root / "_usage.json"
        self._lock = threading.RLock()

    # ── persistence ──────────────────────────────────────────────────
    def _load_usage(self) -> Dict[str, Dict[str, Any]]:
        try:
            return json.loads(self._usage_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_usage(self, usage: Dict[str, Dict[str, Any]]) -> None:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            tmp = self._usage_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(usage, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._usage_path)
        except OSError:
            pass

    def _all_paths(self) -> List[Path]:
        if not self._root.exists():
            return []
        return sorted(self._root.glob("*/SKILL.md"))

    def load_all(self) -> List[Skill]:
        with self._lock:
            usage = self._load_usage()
            out: List[Skill] = []
            for p in self._all_paths():
                try:
                    sk = Skill.from_markdown(p.read_text(encoding="utf-8"), path=str(p))
                except Exception as e:  # noqa: BLE001
                    logger.debug("[skills] skip unreadable %s: %s", p, e)
                    continue
                u = usage.get(sk.name) or {}
                sk.uses = int(u.get("uses", 0) or 0)
                sk.last_used = u.get("last_used")
                out.append(sk)
            return out

    def get(self, name: str) -> Optional[Skill]:
        slug = slugify(name)
        for sk in self.load_all():
            if sk.name == slug:
                return sk
        return None

    # ── mutation ─────────────────────────────────────────────────────
    def save(self, skill: Skill) -> Skill:
        with self._lock:
            if not skill.name:
                skill.name = slugify(skill.description, fallback="skill")
            skill.created = skill.created or _now_iso()
            skill_dir = self._root / skill.name
            skill_dir.mkdir(parents=True, exist_ok=True)
            path = skill_dir / "SKILL.md"
            tmp = path.with_suffix(".md.tmp")
            tmp.write_text(skill.to_markdown(), encoding="utf-8")
            tmp.replace(path)
            skill.path = str(path)
            self._enforce_cap()
            logger.info("[skills] saved '%s' (%s)", skill.name, skill.source)
            return skill

    def add(
        self,
        *,
        title: str,
        when_to_use: str = "",
        procedure: Optional[List[str]] = None,
        pitfalls: Optional[List[str]] = None,
        verification: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        category: str = "general",
        source: str = "learned",
        confidence: float = 0.8,
        description: str = "",
    ) -> Optional[Skill]:
        title = (title or "").strip()
        if not title:
            return None
        sk = Skill(
            name=slugify(title, fallback="skill"),
            description=(description or title).strip(),
            category=category or "general",
            tags=list(tags or []),
            confidence=float(confidence),
            source=source,
            when_to_use=when_to_use or "",
            procedure=list(procedure or []),
            pitfalls=list(pitfalls or []),
            verification=list(verification or []),
        )
        return self.save(sk)

    def delete(self, name: str) -> bool:
        with self._lock:
            slug = slugify(name)
            d = self._root / slug
            md = d / "SKILL.md"
            if not md.exists():
                return False
            try:
                md.unlink()
                if not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                return False
            usage = self._load_usage()
            if slug in usage:
                usage.pop(slug, None)
                self._save_usage(usage)
            return True

    def mark_used(self, name: str) -> None:
        with self._lock:
            slug = slugify(name)
            usage = self._load_usage()
            entry = usage.get(slug) or {"uses": 0}
            entry["uses"] = int(entry.get("uses", 0)) + 1
            entry["last_used"] = time.time()
            usage[slug] = entry
            self._save_usage(usage)

    def _enforce_cap(self) -> None:
        paths = self._all_paths()
        if len(paths) <= MAX_SKILLS:
            return
        # Drop oldest by mtime beyond the cap.
        paths_by_mtime = sorted(paths, key=lambda p: p.stat().st_mtime)
        for p in paths_by_mtime[: len(paths) - MAX_SKILLS]:
            try:
                p.unlink()
                if not any(p.parent.iterdir()):
                    p.parent.rmdir()
            except OSError:
                pass

    def has_title(self, title: str) -> bool:
        return self.get(slugify(title)) is not None

    # ── retrieval ────────────────────────────────────────────────────
    def search(self, query: str, *, limit: int = 5, min_score: float = 0.15) -> List[Skill]:
        terms = _query_terms(query)
        if not terms:
            return []
        scored = [(sk.score(terms), sk) for sk in self.load_all() if sk.status != "draft"]
        scored = [(s, sk) for s, sk in scored if s >= min_score]
        scored.sort(key=lambda x: (x[0], x[1].uses), reverse=True)
        return [sk for _, sk in scored[:limit]]

    def render_for_prompt(
        self,
        *,
        query: str = "",
        limit: int = 3,
        max_chars: int = 1600,
    ) -> str:
        """Return a `<skills>` block of the top skills relevant to `query`.

        Empty string when nothing scores — caller skips the block. Each
        skill renders compactly: name + when-to-use + numbered steps.
        """
        matches = self.search(query, limit=limit)
        if not matches:
            return ""
        lines = ["<skills>", "  (Learned procedures — follow when the task matches 'when to use'.)"]
        total = sum(len(x) for x in lines)
        for sk in matches:
            block = [f"  • {sk.name}: {sk.description or sk.when_to_use}"]
            if sk.when_to_use:
                block.append(f"      when: {sk.when_to_use[:160]}")
            for i, step in enumerate(sk.procedure[:7], 1):
                block.append(f"      {i}. {step[:160]}")
            if sk.pitfalls:
                block.append(f"      pitfall: {sk.pitfalls[0][:160]}")
            chunk = "\n".join(block)
            if total + len(chunk) > max_chars:
                lines.append("  …(more skills available)")
                break
            lines.append(chunk)
            total += len(chunk)
            # Best-effort usage bump so frequently-matched skills rank higher.
            try:
                self.mark_used(sk.name)
            except Exception:
                pass
        lines.append("</skills>")
        return "\n".join(lines)


# ───────────────────────────────────────────────────────────────────────
# Auto-extraction
# ───────────────────────────────────────────────────────────────────────

SKILL_EXTRACT_PROMPT = (
    "You are analyzing an AI agent's work session. The agent took {rounds} "
    "round(s) and {tool_count} tool call(s) to complete the task.\n\n"
    "Extract a reusable 'skill' ONLY IF the session contains a concrete, "
    "repeatable procedure the agent could follow to solve a similar problem "
    "ON THE COMPUTER next time (a sequence of commands, code, file edits, API "
    "calls, or tool usage).\n\n"
    "Return the bare word null (no JSON) when the session is NOT a reusable "
    "computer procedure: pure Q&A/explanation, a one-off personal/context-"
    "specific task, work that happened outside the computer, or a failed/"
    "abandoned attempt.\n\n"
    "When a genuine reusable procedure exists, return ONLY a JSON object:\n"
    '  "title": short name (<10 words)\n'
    '  "when_to_use": one sentence — the trigger condition\n'
    '  "steps": array of 3-7 short imperative steps\n'
    '  "pitfalls": array of 0-3 common failure modes (optional)\n'
    '  "tags": array of 3-5 keywords\n'
    '  "confidence": 0.0-1.0 how reliable AND reusable this is\n\n'
    "Be conservative: if in doubt, return null. No markdown fences."
)


def _strip_think(text: str) -> str:
    text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)
    return text.strip()


def _conversation_digest(messages: List[Dict[str, Any]]) -> str:
    recent = messages[-EXTRACT_CONTEXT_WINDOW:]
    lines: List[str] = []
    for m in recent:
        role = m.get("role", "?")
        if role == "system":
            continue
        c = m.get("content", "")
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
        c = str(c or "")
        if len(c) > 500:
            c = c[:500] + "…"
        if c.strip():
            lines.append(f"[{role}] {c}")
    return "\n".join(lines)


def maybe_extract_skill(
    messages: List[Dict[str, Any]],
    *,
    rounds: int,
    tool_count: int,
    summarizer=None,
    store: Optional[SkillStore] = None,
) -> Optional[Skill]:
    """Distill a reusable skill from a complex agent run.

    Gated by `config.SKILLS_AUTO_EXTRACT` and a complexity floor (≥2 rounds
    or ≥2 tool calls). Returns the saved Skill, or None when nothing
    reusable was found. Never raises — self-learning is best-effort.
    """
    try:
        from config import SKILLS_AUTO_EXTRACT  # type: ignore
        if not SKILLS_AUTO_EXTRACT:
            return None
    except Exception:
        return None
    if rounds < 2 and tool_count < 2:
        return None

    summarizer = summarizer or _default_summarizer()
    if summarizer is None:
        return None
    store = store or get_skills_store()

    digest = _conversation_digest(messages)
    if not digest.strip():
        return None
    prompt = SKILL_EXTRACT_PROMPT.format(rounds=rounds, tool_count=tool_count)
    try:
        resp = summarizer.chat(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"Conversation:\n{digest}"},
            ],
            options={"temperature": 0.1, "num_predict": 700},
        )
        raw = _strip_think(getattr(resp, "content", "") or "")
    except Exception as e:  # noqa: BLE001
        logger.debug("[skills] extractor call failed: %s", e)
        return None

    if not raw or raw.strip().lower() == "null":
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    if text and text[0] != "{":
        s, e = text.find("{"), text.rfind("}")
        if 0 <= s < e:
            text = text[s:e + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    title = str(data.get("title", "")).strip()
    if not title:
        return None
    try:
        conf = float(data.get("confidence", 0.7))
    except (TypeError, ValueError):
        conf = 0.7
    if conf < MIN_CONFIDENCE:
        return None
    if store.has_title(title):
        logger.debug("[skills] '%s' already exists — skip", title)
        return None

    steps = [str(x) for x in (data.get("steps") or []) if str(x).strip()]
    pitfalls = [str(x) for x in (data.get("pitfalls") or []) if str(x).strip()]
    tags = [str(x) for x in (data.get("tags") or []) if str(x).strip()]
    return store.add(
        title=title,
        when_to_use=str(data.get("when_to_use", "")).strip(),
        procedure=steps,
        pitfalls=pitfalls,
        tags=tags,
        source="learned",
        confidence=conf,
    )


def _default_summarizer():
    try:
        from core.providers import smart_provider
        return smart_provider
    except Exception:
        return None


# ───────────────────────────────────────────────────────────────────────
# Module singleton
# ───────────────────────────────────────────────────────────────────────

_store: Optional[SkillStore] = None
_store_lock = threading.Lock()


def get_skills_store() -> SkillStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = SkillStore()
    return _store


def reset_skills_store_for_tests(root: Optional[Path] = None) -> SkillStore:
    global _store
    with _store_lock:
        _store = SkillStore(root=root)
    return _store


__all__ = [
    "Skill", "SkillStore", "slugify",
    "parse_frontmatter", "parse_body", "emit_body",
    "maybe_extract_skill", "SKILL_EXTRACT_PROMPT",
    "get_skills_store", "reset_skills_store_for_tests",
]
