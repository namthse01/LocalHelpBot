"""Per-turn side-effect capture helpers for the orchestrator.

Extracted from :mod:`core.orchestrator` (behavior-preserving move — no logic
changes). These three helpers all run once per user turn, off the side of
``AgentOrchestrator.run_specialist``, and never block the request:

  • ``_maybe_capture_correction`` — detect a "no/don't/actually" style
    correction and persist it as a lesson (Slice 4.3, gated by
    ``config.LESSONS_AUTO_CAPTURE``).
  • ``_maybe_capture_preference`` — detect "I prefer X" / "always X" and store
    it as a sticky decision on the Session (Slice 4.3).
  • ``_maybe_pull_missing_model`` — when a profile names an Ollama model that
    isn't installed AND ``config.OLLAMA_AUTO_PULL`` is on, ask for permission
    and ``ollama pull`` it (Slice 4.5).

``core.orchestrator`` re-imports all three (plus the two regexes are private
to this module) so the bare-name calls inside ``run_specialist`` keep
resolving and ``tests/test_lessons.py`` (which calls
``orchestrator._maybe_capture_correction``) is unaffected.

The logger is pinned to the name ``"core.orchestrator"`` (rather than
``__name__``) so these helpers emit log records under the exact same logger
name they did before the move — a strict behavior-preserving choice.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, List, Optional

from core.conversation_store import get_store
from core.lessons import get_lessons_store

# Pinned to the original module name so log records are byte-identical to
# pre-extraction behavior (see module docstring).
logger = logging.getLogger("core.orchestrator")

# v4 Slice 4.3 — auto-capture correction patterns. When the user's
# message starts with one of these AND there's a prior assistant turn,
# persist a lesson candidate. Off by default (config.LESSONS_AUTO_CAPTURE).
_CORRECTION_RE = re.compile(
    r"^\s*(no[,.\s!]|don[''']t|actually[,]?|that[''']s wrong|stop|"
    r"không phải|sai rồi|đừng|hãy nhớ|please remember|please don[''']t)",
    re.IGNORECASE,
)

# v4 Slice 4.3 — preference detection. When the user says "I prefer X" /
# "always do X" / "luôn dùng X" / "from now on", persist as a sticky
# decision tied to this session.
_PREFERENCE_RE = re.compile(
    r"\b(i prefer|i'd prefer|i would prefer|always (use|do|answer|reply)|"
    r"from now on|always remember|luôn (dùng|trả lời|viết)|ghi nhớ là)\b",
    re.IGNORECASE,
)


def _maybe_capture_correction(
    conversation: Optional[List[Dict[str, Any]]],
    user_message: str,
    session_id: str,
) -> None:
    """Slice 4.3 — auto-detect a correction in the user's new turn and
    persist it as a lesson. Cheap heuristic, never blocks the request."""
    try:
        from config import LESSONS_AUTO_CAPTURE   # type: ignore
        if not LESSONS_AUTO_CAPTURE:
            return
    except Exception:
        return
    if not user_message or not _CORRECTION_RE.match(user_message):
        return
    # Find the most recent assistant turn for context.
    trigger = ""
    for msg in reversed(conversation or []):
        if msg.get("role") == "assistant":
            c = msg.get("content", "")
            if isinstance(c, list):
                c = " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
            trigger = str(c or "")[:200]
            break
    try:
        get_lessons_store().record(
            lesson=user_message[:400],
            scope="global",
            trigger=trigger,
            source_session=session_id,
        )
        logger.info(f"[lessons] auto-captured correction (sid={session_id})")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[lessons] auto-capture failed: {e}")


def _maybe_capture_preference(user_message: str, session_id: str) -> None:
    """Slice 4.3 — detect 'I prefer X' / 'always X' style sticky prefs
    and store on the Session. No-op if already set or pattern misses."""
    if not user_message or not _PREFERENCE_RE.search(user_message):
        return
    try:
        store = get_store()
        sess = store.get(session_id)
        if sess is None:
            return
        # Lightweight extraction: take the matched verb + ≤120 chars context.
        snippet = user_message.strip()[:200]
        key = "preference_" + str(len(sess.sticky))
        sess.set_sticky(key, snippet)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[lessons] preference capture failed: {e}")


def _maybe_pull_missing_model(model: str, stream_cb: Optional[Callable]) -> None:
    """Slice 4.5 — when an agent profile names an Ollama model that
    isn't installed AND OLLAMA_AUTO_PULL is on, ask the user for
    permission and run `ollama pull` in a subprocess.

    Best-effort: silent no-op on any failure path; the existing provider
    fallback chain will catch the actual chat error if pull doesn't run.
    """
    try:
        from config import OLLAMA_AUTO_PULL, OLLAMA_BASE   # type: ignore
        if not OLLAMA_AUTO_PULL or not model:
            return
    except Exception:
        return
    try:
        # Check if model is already installed.
        import json
        import urllib.request
        with urllib.request.urlopen(OLLAMA_BASE + "/api/tags", timeout=3) as r:
            tags = json.loads(r.read())
        installed = {(m.get("name") or m.get("model") or "").lower() for m in tags.get("models", [])}
        wanted = model.lower()
        if wanted in installed or any(i.startswith(wanted.split(":")[0]) for i in installed):
            return   # already present
    except Exception:
        return

    try:
        from core.permissions import request_permission
        decision = request_permission(
            "install_package", model,
            {
                "package": model,
                "reason": f"Agent profile needs '{model}' but it's not installed locally.",
                "interpreter": "ollama",
                "command": f"ollama pull {model}",
            },
        )
        if not decision["allowed"]:
            return
        if stream_cb:
            stream_cb({"type": "status", "text": f"pulling Ollama model {model}…"})
        import subprocess
        proc = subprocess.run(["ollama", "pull", model], capture_output=True, text=True, timeout=600)
        if stream_cb:
            stream_cb({
                "type": "status",
                "text": f"ollama pull {model} → exit {proc.returncode}",
            })
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[ollama] auto-pull failed for {model}: {e}")
