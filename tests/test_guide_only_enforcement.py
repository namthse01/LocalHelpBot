"""v5 — agent loop hard-blocks tool calls under a guide-only ToolPolicy."""
from __future__ import annotations

import core.agent as agent
from core.security import build_effective_tool_policy
from core.tool_schema import Tool, ToolRegistry, ToolResult


def _registry(calls):
    def handler(args):
        calls.append(args)
        return ToolResult.success("ran")
    return ToolRegistry([Tool(
        name="read_file", description="read", input_schema={"type": "object"},
        handler=handler,
    )])


class _ScriptedProvider:
    """Emits a tool_use on turn 1, then a plain answer on turn 2."""

    def __init__(self):
        self.turn = 0

    def chat(self, messages, options=None):
        self.turn += 1

        class R:
            pass
        r = R()
        r.provider, r.model = "fake", "fake"
        r.timing_str = lambda: "0ms"
        if self.turn == 1:
            r.content = '<tool_use>{"name": "read_file", "input": {"path": "x"}}</tool_use>'
        else:
            r.content = "Here is my guidance without tools."
        return r


def test_guide_only_blocks_tool_execution(monkeypatch):
    calls = []
    monkeypatch.setattr(agent, "smart_provider", _ScriptedProvider())
    policy = build_effective_tool_policy(last_user_message="do not use any tools")
    out = agent.run_agent(
        [{"role": "user", "content": "do not use any tools, just guide me"}],
        _registry(calls),
        max_turns=4,
        tool_policy=policy,
    )
    # Handler must NEVER have run, and the loop must still produce an answer.
    assert calls == []
    assert "guidance" in out.lower()


def test_no_policy_allows_tool_execution(monkeypatch):
    calls = []
    monkeypatch.setattr(agent, "smart_provider", _ScriptedProvider())
    out = agent.run_agent(
        [{"role": "user", "content": "read x for me"}],
        _registry(calls),
        max_turns=4,
        tool_policy=None,
    )
    assert calls == [{"path": "x"}]
