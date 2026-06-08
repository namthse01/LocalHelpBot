"""Per-request message preprocessing for the proxy.

Extracted from :mod:`core.proxy` (behavior-preserving move — no logic
changes). Server-side compaction safety net (``_ensure_budget``), system-
prompt injection for forward-only specialists (``_inject_system``), session-id
derivation (``_session_id_from_request``), and last-user-message extraction
(``_last_user_msg``). Re-exported from ``core.proxy``.

``log`` is bound to ``get_logger("proxy")`` — the same named singleton the
proxy module uses — so log records keep emitting under the ``proxy`` logger.
"""

from __future__ import annotations

from core import memory as mem  # session-memory helpers (summary preservation, budget)
from core.conversation_store import derive_session_id, get_store
from core.logs import get_logger

log = get_logger("proxy")


def _ensure_budget(messages: list, *, session_id: str = "") -> list:
    """Server-side compaction safety net.

    Thin wrapper around `MemoryEngine.compact()`. Persists the resulting
    summary to the conversation store when a session_id is known so the
    next turn can read it from T2 instead of relying on the client to
    echo the marker block back.
    """
    if not messages:
        return messages
    try:
        from core.providers import smart_provider as _sp
        engine = mem.get_default_engine()
        prior = engine.extract_summary(messages)
        result = engine.compact(messages, summarizer=_sp, prior_summary=prior)
        if result.fired:
            log.info("compact safety-net fired", extra={"session_id": session_id})
            if session_id:
                get_store().note(session_id, summary=result.summary)
        return result.messages
    except Exception as e:  # noqa: BLE001 — never fail a request on this
        log.warning(f"compact safety-net failed (non-fatal): {e}", extra={"session_id": session_id})
        return messages


def _inject_system(messages: list, system: str, *, session_id: str = "") -> list:
    """Replace the system prompt for forward-only specialists (ui-agent,
    web-creep, browser-agent). Always merges the virtual-model `system`
    with any carried summary; any non-summary system message from the
    client is appended below ours rather than skipped.

    v4 Slice 0.3: dropped the legacy early-return that silently kept the
    client's system message instead of merging ours — that path could
    swallow `UI_SYSTEM` / `WEB_SYSTEM` entirely.
    """
    engine = mem.get_default_engine()
    carried = ""
    if session_id:
        sess = get_store().get(session_id)
        if sess and sess.summary:
            carried = sess.summary
    if not carried:
        carried = engine.extract_summary(messages)
    merged = engine.merge_summary(system, carried)
    out = [{"role": "system", "content": merged}]
    for m in messages:
        if m.get("role") == "system":
            # Fold any non-summary system content the caller sent into ours.
            if not mem._looks_like_summary(mem._content_text(m)):
                out[0]["content"] = out[0]["content"] + "\n\n" + mem._content_text(m)
            continue
        out.append(m)
    return out


def _session_id_from_request(headers: dict, payload: dict) -> str:
    """Derive a stable session_id for this chat request.

    Order of preference:
      1. `X-Session-Id` HTTP header (UI / Discord adapter / MCP send it).
      2. `session_id` field in the JSON body (some clients prefer body).
      3. Hash of (first user message + today's date) — fallback for
         vanilla Ollama clients that ship no identity at all.
    """
    explicit = ""
    if isinstance(headers, dict):
        for k, v in headers.items():
            if k.lower() == "x-session-id":
                explicit = (v or "").strip()
                break
    if not explicit and isinstance(payload, dict):
        explicit = (payload.get("session_id") or "").strip()
    return derive_session_id(payload.get("messages", []) if isinstance(payload, dict) else [], explicit=explicit)


def _last_user_msg(messages: list) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            c = msg.get("content", "")
            if isinstance(c, list):
                return " ".join(p.get("text", "") for p in c if p.get("type") == "text")
            return str(c)
    return ""
