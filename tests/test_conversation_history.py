"""Regression tests for multi-turn conversation memory.

The Web UI sends ONLY the latest user message on each `/api/chat` call
(the Ollama wire protocol is stateless). Before the fix the server kept
no per-session message history, so the model saw a single turn at a time
and lost every earlier result. A user reported:

    "1+1"            -> 2
    "3x10"           -> 30
    "2 cái trên chia nhau"  (divide the two above) -> 4   # wrong, lost 30

The fix gives each `Session` an in-memory `history` buffer and has the
orchestrator replay it as the model's real conversation history for
top-level runs. These tests lock that in:

  * `Session.record_message` — append / cap / ignore-empty semantics.
  * End-to-end: three thin-client turns on one session id make the model
    actually receive the prior user+assistant turns on turn 3.
"""
from __future__ import annotations

import pytest

from core import agent as agent_mod
from core import orchestrator as orch_mod
from core.conversation_store import (
    MAX_HISTORY_MESSAGES,
    Session,
    get_store,
)
from core.orchestrator import AgentOrchestrator


# ───────────────────────────────────────────────────────────────────────
# Session.record_message
# ───────────────────────────────────────────────────────────────────────


def test_record_message_appends_in_order():
    sess = Session(sid="t")
    sess.record_message("user", "hello")
    sess.record_message("assistant", "hi there")
    assert sess.history == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]


def test_record_message_ignores_empty():
    sess = Session(sid="t")
    sess.record_message("user", "")
    sess.record_message("", "orphan")
    sess.record_message("assistant", "   ")  # whitespace-only
    assert sess.history == []


def test_record_message_caps_at_max():
    sess = Session(sid="t")
    for i in range(MAX_HISTORY_MESSAGES + 6):
        sess.record_message("user", f"msg {i}")
    assert len(sess.history) == MAX_HISTORY_MESSAGES
    # Oldest dropped, newest kept.
    assert sess.history[0]["content"] == "msg 6"
    assert sess.history[-1]["content"] == f"msg {MAX_HISTORY_MESSAGES + 5}"


# ───────────────────────────────────────────────────────────────────────
# End-to-end: orchestrator replays stored history for thin clients
# ───────────────────────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, content):
        self.content = content
        self.provider = "fake"
        self.model = "fake-14b"

    def timing_str(self):
        return "(test)"


class _CapturingProvider:
    """Scripts main-agent answers and records every conversation it sees.

    A "main" call is identified by a leading system message; non-main
    calls (defensive) get a neutral reply and are not scripted.
    """

    def __init__(self, script):
        self.script = list(script)
        self.seen = []  # list of conversations (each a list of msg dicts)

    def chat(self, msgs, options=None):
        msgs = list(msgs or [])
        is_main = bool(msgs) and msgs[0].get("role") == "system"
        if is_main:
            self.seen.append(msgs)
            reply = self.script.pop(0) if self.script else "done."
            return _FakeResp(reply)
        return _FakeResp("ok")

    def describe(self):
        return {
            "last_used_provider": "fake",
            "primary_provider": "fake",
            "last_used_model": "fake-14b",
            "primary_model": "fake-14b",
            "fallback_provider": "fake",
            "fallback_model": "fake-14b",
        }


def _non_system(conversation):
    return [m for m in conversation if m.get("role") != "system"]


def test_orchestrator_replays_prior_turns_to_model(monkeypatch):
    # Fresh, isolated store.
    monkeypatch.setattr("core.conversation_store._store", None, raising=False)

    # Neutralise best-effort side paths that could touch the network or
    # consume scripted replies out of order.
    monkeypatch.setattr(orch_mod, "_maybe_capture_correction", lambda *a, **k: None)
    monkeypatch.setattr(orch_mod, "_maybe_capture_preference", lambda *a, **k: None)
    monkeypatch.setattr(orch_mod, "_maybe_pull_missing_model", lambda *a, **k: None)
    try:
        import core.context_budget as _cb
        monkeypatch.setattr(_cb, "effective_max_tokens", lambda *a, **k: 8000)
    except Exception:
        pass

    fake = _CapturingProvider([
        "1 + 1 = 2",
        "3 x 10 = 30",
        "30 / 2 = 15",
    ])
    # Both the agent loop and the orchestrator hold their own bound ref.
    monkeypatch.setattr(agent_mod, "smart_provider", fake)
    monkeypatch.setattr(orch_mod, "smart_provider", fake)

    orch = AgentOrchestrator()
    sid = "hist-e2e"

    # Each turn is a THIN client request: only the latest user message.
    orch.run_specialist(
        "main", "1+1",
        conversation=[{"role": "user", "content": "1+1"}],
        session_id=sid,
    )
    orch.run_specialist(
        "main", "3x10",
        conversation=[{"role": "user", "content": "3x10"}],
        session_id=sid,
    )
    orch.run_specialist(
        "main", "2 cái trên chia nhau",
        conversation=[{"role": "user", "content": "2 cái trên chia nhau"}],
        session_id=sid,
    )

    assert len(fake.seen) == 3, "expected exactly one main LLM call per turn"

    # Turn 1 saw no prior turns.
    assert _non_system(fake.seen[0]) == [{"role": "user", "content": "1+1"}]

    # Turn 3 MUST contain both earlier results so the model can divide them.
    turn3 = _non_system(fake.seen[2])
    users = [m["content"] for m in turn3 if m.get("role") == "user"]
    assistants = [m["content"] for m in turn3 if m.get("role") == "assistant"]

    assert any("1+1" in u for u in users), f"turn-1 question missing: {turn3!r}"
    assert any("= 2" in a for a in assistants), f"turn-1 answer (2) missing: {turn3!r}"
    assert any("3x10" in u for u in users), f"turn-2 question missing: {turn3!r}"
    assert any("30" in a for a in assistants), f"turn-2 answer (30) missing: {turn3!r}"
    assert any("chia" in u for u in users), f"turn-3 question missing: {turn3!r}"

    # The session now stores all six raw turns for the next request.
    sess = get_store().get(sid)
    assert sess is not None
    assert len(sess.history) == 6
    assert sess.history[0] == {"role": "user", "content": "1+1"}
    assert sess.history[-1] == {"role": "assistant", "content": "30 / 2 = 15"}


def test_orchestrator_does_not_double_inject_when_client_sends_history(monkeypatch):
    """A non-thin client (sends its own assistant turns) must NOT get the
    stored history prepended on top — that would duplicate turns."""
    monkeypatch.setattr("core.conversation_store._store", None, raising=False)
    monkeypatch.setattr(orch_mod, "_maybe_capture_correction", lambda *a, **k: None)
    monkeypatch.setattr(orch_mod, "_maybe_capture_preference", lambda *a, **k: None)
    monkeypatch.setattr(orch_mod, "_maybe_pull_missing_model", lambda *a, **k: None)
    try:
        import core.context_budget as _cb
        monkeypatch.setattr(_cb, "effective_max_tokens", lambda *a, **k: 8000)
    except Exception:
        pass

    fake = _CapturingProvider(["first answer", "second answer"])
    monkeypatch.setattr(agent_mod, "smart_provider", fake)
    monkeypatch.setattr(orch_mod, "smart_provider", fake)

    orch = AgentOrchestrator()
    sid = "hist-nothin"

    # Turn 1 (thin) seeds stored history.
    orch.run_specialist(
        "main", "alpha",
        conversation=[{"role": "user", "content": "alpha"}],
        session_id=sid,
    )

    # Turn 2 the client sends FULL history itself (has an assistant turn).
    client_history = [
        {"role": "user", "content": "alpha"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "beta"},
    ]
    orch.run_specialist(
        "main", "beta",
        conversation=client_history,
        session_id=sid,
    )

    turn2 = _non_system(fake.seen[1])
    # "alpha" should appear exactly once (from the client), not duplicated
    # by a stored-history prepend.
    alpha_count = sum(1 for m in turn2 if m.get("content") == "alpha")
    assert alpha_count == 1, f"stored history double-injected: {turn2!r}"
