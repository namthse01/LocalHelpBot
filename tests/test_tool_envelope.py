"""Slice 0.4 — typed tool envelope + Stop-the-Line invariants."""
from __future__ import annotations

from core.tool_schema import (
    RECOVERY_HINTS,
    ErrorCode,
    Tool,
    ToolRegistry,
    ToolResult,
    build_default_registry,
)
from core import agent as agent_mod


# ───────────────────────────────────────────────────────────────────────
# ToolResult basics
# ───────────────────────────────────────────────────────────────────────


def test_tool_result_success_is_ok():
    r = ToolResult.success("done", path="x.py")
    assert r.ok
    assert r.error_code is None
    assert r.meta == {"path": "x.py"}


def test_tool_result_error_pulls_default_hint():
    r = ToolResult.error(ErrorCode.STALE_OLD_STRING, "not found")
    assert not r.ok
    assert r.error_code is ErrorCode.STALE_OLD_STRING
    assert r.hint == RECOVERY_HINTS[ErrorCode.STALE_OLD_STRING]


def test_tool_result_render_xml_emits_value_not_repr():
    """f-string interpolation on the enum must use .value (not 'ErrorCode.X')."""
    r = ToolResult.error(ErrorCode.PERMISSION_DENIED, "no", retryable=False)
    xml = r.render_xml()
    assert 'code="PERMISSION_DENIED"' in xml
    assert "ErrorCode.PERMISSION_DENIED" not in xml
    assert 'is_error="true"' in xml
    assert 'retryable="false"' in xml


def test_tool_result_render_xml_escapes_double_quotes_in_hint():
    r = ToolResult.error(ErrorCode.STALE_OLD_STRING, "x", hint='Use "quoted" form')
    xml = r.render_xml()
    # Double-quotes in attrs must be replaced with single-quotes
    assert "'quoted'" in xml
    assert '"quoted"' not in xml.split('hint="', 1)[1].split('"', 1)[0]


# ───────────────────────────────────────────────────────────────────────
# Legacy string adapter
# ───────────────────────────────────────────────────────────────────────


def test_from_legacy_recognises_file_not_found():
    r = ToolResult.from_legacy("ERROR: File not found: x.md")
    assert not r.ok
    assert r.error_code is ErrorCode.FILE_NOT_FOUND


def test_from_legacy_recognises_permission_denied():
    r = ToolResult.from_legacy("PERMISSION_DENIED: user said no")
    assert not r.ok
    assert r.error_code is ErrorCode.PERMISSION_DENIED
    assert not r.retryable


def test_from_legacy_recognises_stale_old_string():
    r = ToolResult.from_legacy("ERROR: old_string not found in foo.py")
    assert r.error_code is ErrorCode.STALE_OLD_STRING


def test_from_legacy_recognises_ambiguous_match():
    r = ToolResult.from_legacy("ERROR: old_string appears 3 times in foo.py")
    assert r.error_code is ErrorCode.AMBIGUOUS_MATCH


def test_from_legacy_recognises_too_large():
    r = ToolResult.from_legacy("ERROR: write_file content too large (5000 chars)")
    assert r.error_code is ErrorCode.TOO_LARGE


def test_from_legacy_passes_through_plain_success():
    r = ToolResult.from_legacy("OK: did the thing")
    assert r.ok
    assert r.output == "OK: did the thing"


def test_from_legacy_handles_none():
    r = ToolResult.from_legacy(None)
    assert r.ok
    assert r.output == ""


def test_from_legacy_passes_through_existing_tool_result():
    original = ToolResult.error(ErrorCode.RATE_LIMITED, "429")
    assert ToolResult.from_legacy(original) is original


# ───────────────────────────────────────────────────────────────────────
# ToolRegistry
# ───────────────────────────────────────────────────────────────────────


def test_default_registry_includes_expected_tools():
    reg = build_default_registry()
    expected = {
        "read_file", "write_file", "edit_file", "list_dir",
        "grep_file", "glob_files", "run_command",
        "search_web", "fetch_url", "query_rag",
        # plugin tools
        "delete_file", "make_dir", "move_file",
        "python_exec", "list_processes", "kill_process",
        "install_package",
        "read_file_chunk", "read_pdf", "write_pdf", "read_docx", "write_docx",
    }
    for name in expected:
        assert name in reg, f"missing tool: {name}"


def test_registry_filter_returns_subset():
    reg = build_default_registry()
    sub = reg.filter(["read_file", "write_file"])
    assert "read_file" in sub
    assert "write_file" in sub
    assert "delete_file" not in sub


def test_tool_run_normalises_string_handler_to_tool_result():
    """A handler still returning a raw string gets adapted via from_legacy()."""
    legacy_tool = Tool(
        name="legacy", description="returns a string",
        input_schema={"type": "object"},
        handler=lambda a: "ERROR: File not found: x",
    )
    r = legacy_tool.run({})
    assert isinstance(r, ToolResult)
    assert r.error_code is ErrorCode.FILE_NOT_FOUND


def test_tool_run_catches_handler_exception_as_unknown_error():
    def bad(args):
        raise RuntimeError("boom")
    t = Tool(name="bad", description="raises", input_schema={"type": "object"}, handler=bad)
    r = t.run({})
    assert not r.ok
    assert r.error_code is ErrorCode.UNKNOWN
    assert "boom" in r.output


# ───────────────────────────────────────────────────────────────────────
# Stop-the-Line
# ───────────────────────────────────────────────────────────────────────


def test_args_hash_is_stable_for_same_args():
    h1 = agent_mod._args_hash("edit_file", {"path": "a.py", "old_string": "x"})
    h2 = agent_mod._args_hash("edit_file", {"path": "a.py", "old_string": "x"})
    assert h1 == h2


def test_args_hash_differs_for_different_args():
    h1 = agent_mod._args_hash("edit_file", {"path": "a.py", "old_string": "x"})
    h2 = agent_mod._args_hash("edit_file", {"path": "a.py", "old_string": "y"})
    assert h1 != h2


def test_stop_the_line_blocks_third_invocation_of_failing_call(monkeypatch):
    """End-to-end: two failures of the same call let the handler run; the
    third is short-circuited with a LOOP_BUDGET synthetic result."""
    calls = []

    def broken_handler(args):
        calls.append(args)
        return ToolResult.error(ErrorCode.STALE_OLD_STRING, "nope")

    fake_tool = Tool(
        name="edit_file",
        description="test broken edit_file",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
            },
            "required": ["path", "old_string", "new_string"],
        },
        handler=broken_handler,
    )
    reg = ToolRegistry([fake_tool])

    class _FakeResp:
        def __init__(self, content): self.content = content; self.provider = "fake"; self.model = "fake-7b"
        def timing_str(self): return "(test)"

    script = [
        '<tool_use>{"name":"edit_file","input":{"path":"a.py","old_string":"x","new_string":"y"}}</tool_use>',
        '<tool_use>{"name":"edit_file","input":{"path":"a.py","old_string":"x","new_string":"y"}}</tool_use>',
        '<tool_use>{"name":"edit_file","input":{"path":"a.py","old_string":"x","new_string":"y"}}</tool_use>',
        "Giving up.",
    ]
    idx = [0]

    class _FakeProvider:
        def chat(self, msgs, options=None):
            if idx[0] >= len(script):
                return _FakeResp("done.")
            out = _FakeResp(script[idx[0]])
            idx[0] += 1
            return out

    monkeypatch.setattr(agent_mod, "smart_provider", _FakeProvider())

    events = []
    agent_mod.run_agent(
        [
            {"role": "system", "content": "test"},
            {"role": "user", "content": "edit a.py"},
        ],
        reg,
        stream_cb=lambda e: events.append(e),
        max_turns=8,
    )

    # Handler invoked exactly twice; third invocation blocked by Stop-the-Line.
    assert len(calls) == 2

    # A LOOP_BUDGET tool_result event must have been emitted.
    codes = [e.get("code") for e in events if isinstance(e, dict) and e.get("type") == "tool_result"]
    assert "STALE_OLD_STRING" in codes
    assert "LOOP_BUDGET" in codes
