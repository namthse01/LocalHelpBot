"""Slice 0.1 — confirm ToolResult.meta['files_touched'] is propagated
into the session store, and surfaces in the T2 tier of the next
assemble_context() call."""
from __future__ import annotations

from core import agent as agent_mod
from core.context import assemble_context
from core.conversation_store import ConversationStore, get_store
from core.tool_schema import ErrorCode, Tool, ToolRegistry, ToolResult


def test_run_agent_writes_files_touched_to_store(monkeypatch):
    # Reset the global store for test isolation.
    monkeypatch.setattr(
        "core.conversation_store._store", None, raising=False
    )
    sid = "files-touched-test"

    def writer(args):
        return ToolResult.success(
            f"OK: Written to {args['path']}",
            path=args["path"],
            files_touched=[args["path"]],
        )

    fake_tool = Tool(
        name="write_file",
        description="test write tool",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
        handler=writer,
    )
    reg = ToolRegistry([fake_tool])

    class _FakeResp:
        def __init__(self, content): self.content = content; self.provider = "fake"; self.model = "fake-7b"
        def timing_str(self): return "(test)"

    idx = [0]
    script = [
        '<tool_use>{"name":"write_file","input":{"path":"hello.md","content":"hi"}}</tool_use>',
        "Done.",
    ]

    class _FakeProvider:
        def chat(self, msgs, options=None):
            if idx[0] >= len(script): return _FakeResp("ok.")
            out = _FakeResp(script[idx[0]]); idx[0] += 1; return out

    monkeypatch.setattr(agent_mod, "smart_provider", _FakeProvider())

    # Pre-create the session so the store knows about it.
    get_store().get_or_create(sid)

    agent_mod.run_agent(
        [{"role": "user", "content": "write hello.md"}],
        reg,
        session_id=sid,
        max_turns=4,
    )

    sess = get_store().get(sid)
    assert sess is not None
    assert "hello.md" in sess.files_touched


def test_run_agent_without_session_id_noops_files_touched(monkeypatch):
    """Legacy callers (no session_id) must not crash and must not
    persist files_touched into a random store entry."""
    def writer(args):
        return ToolResult.success("OK", files_touched=["x.md"])

    t = Tool(name="write_file", description="x", input_schema={"type": "object"}, handler=writer)
    reg = ToolRegistry([t])

    class _FakeResp:
        def __init__(self, content): self.content = content; self.provider = "fake"; self.model = "fake-7b"
        def timing_str(self): return "(test)"

    idx = [0]
    script = [
        '<tool_use>{"name":"write_file","input":{}}</tool_use>',
        "done.",
    ]

    class _FakeProvider:
        def chat(self, msgs, options=None):
            if idx[0] >= len(script): return _FakeResp("ok.")
            out = _FakeResp(script[idx[0]]); idx[0] += 1; return out

    monkeypatch.setattr(agent_mod, "smart_provider", _FakeProvider())

    # No session_id passed — must not crash.
    final = agent_mod.run_agent(
        [{"role": "user", "content": "write"}],
        reg,
        max_turns=4,
    )
    assert isinstance(final, str)


def test_files_touched_appears_in_t2_after_propagation():
    """End-to-end: after files_touched lands in the store, the next
    assemble_context() call renders them in the T2 <session> block."""
    sid = "t2-files-touched"
    store = get_store()
    sess = store.get_or_create(sid)
    sess.record_files_touched(["notes/draft.md", "out/report.md"])

    ctx = assemble_context(
        session_id=sid,
        base_prompt="You are main.",
        tools=[],
        history=[],
        runtime_block="",
        profile_name="main",
    )
    assert "<session>" in ctx.system
    assert "files_touched:" in ctx.system
    assert "notes/draft.md" in ctx.system
    assert "out/report.md" in ctx.system
