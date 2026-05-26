"""Integration test for the screenshot bug:

    Turn 1 — user asks for a file read
    Turn 2 — user asks a math question, agent answers "1+1 = 2"
    Turn 3 — user types "+ 4"

Before the fix the agent re-read the file. After the fix the
arithmetic fast-path fires and returns "2 + 4 = 6" with NO LLM call.
"""
from __future__ import annotations

from core import agent as agent_mod
from core.conversation_store import get_store
from core.tool_schema import Tool, ToolRegistry, ToolResult


class _FakeResp:
    def __init__(self, content):
        self.content = content
        self.provider = "fake"
        self.model = "fake-7b"

    def timing_str(self):
        return "(test)"


class _CountingProvider:
    """Records how many times .chat() was called and what scripted reply
    to return next."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def chat(self, msgs, options=None):
        self.calls += 1
        if not self.script:
            return _FakeResp("done.")
        return _FakeResp(self.script.pop(0))


def _reader(args):
    return ToolResult.success(
        f"PDF content from {args.get('path', '?')}: lorem ipsum.",
        files_touched=[args.get("path", "?")],
    )


def _make_registry():
    read_pdf = Tool(
        name="read_pdf",
        description="read a pdf",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        handler=_reader,
    )
    return ToolRegistry([read_pdf])


def test_three_turn_fastpath_skips_llm_on_operator_followup(monkeypatch):
    monkeypatch.setattr("core.conversation_store._store", None, raising=False)
    sid = "fp-three-turn"
    get_store().get_or_create(sid)

    # ── Turn 1 — agent reads a PDF and reports it. ─────────────────
    t1_provider = _CountingProvider([
        '<tool_use>{"name":"read_pdf","input":{"path":"guide.pdf"}}</tool_use>',
        "Summary of guide.pdf: lorem ipsum.",
    ])
    monkeypatch.setattr(agent_mod, "smart_provider", t1_provider)
    out1 = agent_mod.run_agent(
        [{"role": "user", "content": "read guide.pdf for me"}],
        _make_registry(),
        session_id=sid,
        max_turns=4,
    )
    assert "lorem ipsum" in out1

    # ── Turn 2 — math question. Stub LLM to answer "1+1 = 2". ──────
    t2_provider = _CountingProvider(["1+1 = 2"])
    monkeypatch.setattr(agent_mod, "smart_provider", t2_provider)
    out2 = agent_mod.run_agent(
        [{"role": "user", "content": "what is 1+1"}],
        _make_registry(),
        session_id=sid,
        max_turns=4,
    )
    assert "2" in out2
    # Session should now carry the numeric anchor.
    sess = get_store().get(sid)
    assert sess is not None
    assert sess.last_numeric_value == 2.0

    # ── Turn 3 — "+ 4". Fast-path MUST fire; LLM MUST NOT be called.
    t3_provider = _CountingProvider([
        # If the fast-path didn't fire and the LLM was consulted, this
        # would be returned — the assertion below will fail.
        "I should re-read the PDF for context.",
    ])
    monkeypatch.setattr(agent_mod, "smart_provider", t3_provider)
    out3 = agent_mod.run_agent(
        [{"role": "user", "content": "+ 4"}],
        _make_registry(),
        session_id=sid,
        max_turns=4,
    )
    assert t3_provider.calls == 0, (
        "LLM was called on the operator-leading follow-up — fast-path "
        f"did NOT fire. Output was: {out3!r}"
    )
    assert "= 6" in out3, f"Expected 2 + 4 = 6, got {out3!r}"
    # New numeric anchor should be 6 now.
    sess = get_store().get(sid)
    assert sess is not None
    assert sess.last_numeric_value == 6.0


def test_fastpath_does_not_fire_without_numeric_anchor(monkeypatch):
    """If turn 1 didn't produce a number, '+ 4' must fall through to the
    LLM rather than picking a stale or hallucinated anchor."""
    monkeypatch.setattr("core.conversation_store._store", None, raising=False)
    sid = "fp-no-anchor"
    get_store().get_or_create(sid)

    t1_provider = _CountingProvider(["Hello! How can I help?"])
    monkeypatch.setattr(agent_mod, "smart_provider", t1_provider)
    agent_mod.run_agent(
        [{"role": "user", "content": "hi"}],
        _make_registry(),
        session_id=sid,
        max_turns=2,
    )
    sess = get_store().get(sid)
    assert sess is not None
    assert sess.last_numeric_value is None

    # Now "+ 4" should hit the LLM — no anchor to compute from.
    t2_provider = _CountingProvider(["I need more context to compute that."])
    monkeypatch.setattr(agent_mod, "smart_provider", t2_provider)
    agent_mod.run_agent(
        [{"role": "user", "content": "+ 4"}],
        _make_registry(),
        session_id=sid,
        max_turns=2,
    )
    assert t2_provider.calls >= 1
