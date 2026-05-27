"""Tests for the convergence guardrails: family budget + CONVERGE injection.

Both guardrails are about forcing the agent to STOP and answer instead of
endlessly exploring with slightly-different args. Stop-the-Line already
handled identical retries; these tests cover the new "same family,
different args" and "too many turns regardless of progress" paths.
"""
from __future__ import annotations

from typing import Any, Dict

from core import agent as agent_mod
from core.tool_schema import ErrorCode, Tool, ToolRegistry, ToolResult


class _FakeResp:
    def __init__(self, content):
        self.content = content
        self.provider = "fake"
        self.model = "fake-7b"

    def timing_str(self):
        return "(test)"


class _ScriptedProvider:
    """Returns canned assistant messages turn by turn."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.received_messages: list = []

    def chat(self, messages, options=None):
        self.calls += 1
        self.received_messages.append(list(messages))
        if not self.script:
            return _FakeResp("ok done.")
        return _FakeResp(self.script.pop(0))


def _ok_handler(args):
    return ToolResult.success("snippet result for " + str(args.get("query", "")))


def _make_search_registry():
    return ToolRegistry([
        Tool(
            name="search_web",
            description="DuckDuckGo web search",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            handler=_ok_handler,
        ),
    ])


# ───────────────────────────────────────────────────────────────────────
# Tool-family budget
# ───────────────────────────────────────────────────────────────────────


def test_family_budget_blocks_fifth_web_call_with_different_args(monkeypatch):
    """5 different search_web calls in a row should hit the family budget
    on the 5th and return LOOP_BUDGET instead of executing."""
    monkeypatch.setattr("core.conversation_store._store", None, raising=False)

    # Script: 5 different search_web calls, then a final answer.
    script = [
        '<tool_use>{"name":"search_web","input":{"query":"pokemon psychic 1"}}</tool_use>',
        '<tool_use>{"name":"search_web","input":{"query":"pokemon psychic 2"}}</tool_use>',
        '<tool_use>{"name":"search_web","input":{"query":"pokemon psychic 3"}}</tool_use>',
        '<tool_use>{"name":"search_web","input":{"query":"pokemon psychic 4"}}</tool_use>',
        '<tool_use>{"name":"search_web","input":{"query":"pokemon psychic 5"}}</tool_use>',  # blocked
        "OK final answer.",
    ]
    provider = _ScriptedProvider(script)
    monkeypatch.setattr(agent_mod, "smart_provider", provider)

    # Stream callback captures all tool_result events.
    events: list = []

    def cb(ev):
        events.append(ev)

    agent_mod.run_agent(
        [{"role": "user", "content": "find me a pokemon image"}],
        _make_search_registry(),
        stream_cb=cb,
        max_turns=10,
    )

    # Locate tool_result events.
    tool_results = [e for e in events if e.get("type") == "tool_result"]
    # First 4 calls should be ok; the 5th should be LOOP_BUDGET.
    assert len(tool_results) >= 5, f"expected >=5 tool_result events, got {len(tool_results)}"
    assert all(e["ok"] for e in tool_results[:4]), "first 4 calls should succeed"
    assert tool_results[4]["ok"] is False
    assert tool_results[4]["code"] == ErrorCode.LOOP_BUDGET.value


def test_family_budget_does_not_block_under_threshold(monkeypatch):
    """3 search calls in a row stay under the budget and all succeed."""
    monkeypatch.setattr("core.conversation_store._store", None, raising=False)

    script = [
        '<tool_use>{"name":"search_web","input":{"query":"a"}}</tool_use>',
        '<tool_use>{"name":"search_web","input":{"query":"b"}}</tool_use>',
        '<tool_use>{"name":"search_web","input":{"query":"c"}}</tool_use>',
        "Done.",
    ]
    provider = _ScriptedProvider(script)
    monkeypatch.setattr(agent_mod, "smart_provider", provider)

    events: list = []
    agent_mod.run_agent(
        [{"role": "user", "content": "do stuff"}],
        _make_search_registry(),
        stream_cb=lambda e: events.append(e),
        max_turns=8,
    )

    tool_results = [e for e in events if e.get("type") == "tool_result"]
    assert len(tool_results) == 3
    assert all(e["ok"] for e in tool_results)


def test_unknown_tool_family_does_not_count_toward_budget(monkeypatch):
    """Tools not in TOOL_FAMILIES should never trigger the family gate.

    Sanity check: if we add a custom tool that isn't classified, the family
    budget shouldn't accidentally lock it out.
    """
    monkeypatch.setattr("core.conversation_store._store", None, raising=False)

    def _handler(args):
        return ToolResult.success("ok")

    custom = Tool(
        name="my_custom_unclassified_tool",
        description="x",
        input_schema={"type": "object", "properties": {}},
        handler=_handler,
    )
    reg = ToolRegistry([custom])

    # 6 calls of a tool that isn't in TOOL_FAMILIES — should all succeed.
    script = [
        '<tool_use>{"name":"my_custom_unclassified_tool","input":{}}</tool_use>',
    ] * 6 + ["done."]
    provider = _ScriptedProvider(script)
    monkeypatch.setattr(agent_mod, "smart_provider", provider)

    events: list = []
    agent_mod.run_agent(
        [{"role": "user", "content": "x"}],
        reg,
        stream_cb=lambda e: events.append(e),
        max_turns=10,
    )

    tool_results = [e for e in events if e.get("type") == "tool_result"]
    assert all(e["ok"] for e in tool_results), (
        "unclassified tool was incorrectly blocked by the family budget"
    )


# ───────────────────────────────────────────────────────────────────────
# CONVERGE injection
# ───────────────────────────────────────────────────────────────────────


def test_converge_nudge_fires_once_after_threshold_turns(monkeypatch):
    """After CONVERGE_INJECT_AFTER_TURN turns of tool-use, a user-role
    "CONVERGE:" nudge gets appended to the conversation. It fires once."""
    monkeypatch.setattr("core.conversation_store._store", None, raising=False)

    # Provider that keeps calling tools forever. The nudge should
    # eventually appear in the conversation passed to .chat().
    inf_script = [
        '<tool_use>{"name":"search_web","input":{"query":"q"}}</tool_use>',
    ] * 10
    provider = _ScriptedProvider(inf_script)
    monkeypatch.setattr(agent_mod, "smart_provider", provider)

    agent_mod.run_agent(
        [{"role": "user", "content": "find pokemon"}],
        _make_search_registry(),
        max_turns=8,
    )

    # Count CONVERGE: occurrences across every conversation we received.
    # The nudge is appended ONCE, then carried in all later .chat() calls.
    converge_msgs: list = []
    for msgs in provider.received_messages:
        for m in msgs:
            if m.get("role") == "user" and "CONVERGE:" in str(m.get("content", "")):
                converge_msgs.append(m)

    assert converge_msgs, "CONVERGE: nudge was never injected"
    # All injected references should point to the SAME message instance —
    # because the loop only inserts it once and just keeps re-sending it.
    unique_contents = {m["content"] for m in converge_msgs}
    assert len(unique_contents) == 1, (
        f"CONVERGE: nudge appears to have fired multiple times with different "
        f"contents: {unique_contents}"
    )


def test_converge_does_not_fire_when_task_finishes_early(monkeypatch):
    """If the agent emits a final answer within the first few turns, no
    CONVERGE: injection should happen."""
    monkeypatch.setattr("core.conversation_store._store", None, raising=False)

    script = [
        '<tool_use>{"name":"search_web","input":{"query":"q"}}</tool_use>',
        "Here is your answer — done.",
    ]
    provider = _ScriptedProvider(script)
    monkeypatch.setattr(agent_mod, "smart_provider", provider)

    agent_mod.run_agent(
        [{"role": "user", "content": "quick task"}],
        _make_search_registry(),
        max_turns=8,
    )

    for msgs in provider.received_messages:
        for m in msgs:
            if m.get("role") == "user":
                assert "CONVERGE:" not in str(m.get("content", "")), (
                    "CONVERGE: nudge fired prematurely on a short task"
                )


# ───────────────────────────────────────────────────────────────────────
# Combined: MAX_TURNS lowered to 8
# ───────────────────────────────────────────────────────────────────────


def test_max_turns_default_is_eight():
    """Sanity: MAX_TURNS default should now be 8, not 20."""
    assert agent_mod.MAX_TURNS == 8


def test_tool_family_map_covers_web_image_fs_categories():
    """Quick sanity that the family classification covers the categories
    the convergence fix actually depends on."""
    f = agent_mod.TOOL_FAMILIES
    assert f.get("search_web") == "web"
    assert f.get("fetch_url") == "web"
    assert f.get("download_file") == "web"
    assert f.get("generate_image") == "image"
    assert f.get("describe_image") == "image"
    assert f.get("read_file") == "fs_read"
    assert f.get("write_file") == "fs_write"
    assert f.get("edit_file") == "fs_write"
    assert agent_mod.TOOL_FAMILY_BUDGET == 4
