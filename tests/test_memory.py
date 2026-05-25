"""Slice 0.4 — memory + session continuity invariants."""
from __future__ import annotations

from core.conversation_store import (
    ConversationStore,
    Session,
    derive_discord_session_id,
    derive_session_id,
    get_store,
)
from core.memory import (
    DEFAULT_MAX_TOKENS,
    SUMMARY_END,
    SUMMARY_MARKER,
    MemoryEngine,
    extract_summary,
    merge_summary,
)


# ───────────────────────────────────────────────────────────────────────
# derive_session_id
# ───────────────────────────────────────────────────────────────────────


def test_derive_session_id_is_stable_for_same_first_user_message():
    sid1 = derive_session_id([{"role": "user", "content": "hello"}])
    sid2 = derive_session_id([{"role": "user", "content": "hello"}])
    assert sid1 == sid2


def test_derive_session_id_differs_per_first_user_message():
    a = derive_session_id([{"role": "user", "content": "hello"}])
    b = derive_session_id([{"role": "user", "content": "something else"}])
    assert a != b


def test_derive_session_id_respects_explicit_override():
    sid = derive_session_id(
        [{"role": "user", "content": "anything"}],
        explicit="my-custom-id",
    )
    assert sid == "my-custom-id"


def test_derive_session_id_handles_empty_conversation():
    # Must not raise on an empty list
    sid = derive_session_id([])
    assert isinstance(sid, str) and len(sid) > 0


def test_derive_session_id_handles_list_content_format():
    """Anthropic-style content can be a list of parts; derivation must
    flatten safely."""
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hi there"}]}]
    sid = derive_session_id(msgs)
    assert isinstance(sid, str) and len(sid) > 0


def test_derive_discord_session_id_has_expected_shape():
    sid = derive_discord_session_id(123, 456, 789)
    assert sid.startswith("d-")
    assert len(sid) == 16   # 'd-' + 14 hex chars per the implementation


# ───────────────────────────────────────────────────────────────────────
# ConversationStore / Session
# ───────────────────────────────────────────────────────────────────────


def test_session_goal_locks_on_first_set():
    s = Session(sid="x")
    s.note_user_goal("first")
    s.note_user_goal("second — should be ignored")
    assert s.goal == "first"


def test_session_files_touched_dedupes_and_orders_newest_last():
    s = Session(sid="x")
    s.record_files_touched(["a.py", "b.py"])
    s.record_files_touched(["a.py"])    # re-touching moves it to end
    assert s.files_touched == ["b.py", "a.py"]


def test_session_files_touched_caps_at_50():
    s = Session(sid="x")
    s.record_files_touched([f"f{i}.py" for i in range(60)])
    assert len(s.files_touched) == 50


def test_session_sticky_set_and_overwrite():
    s = Session(sid="x")
    s.set_sticky("lang", "Vietnamese")
    s.set_sticky("lang", "English")
    assert s.sticky == {"lang": "English"}


def test_store_get_or_create_is_idempotent():
    store = ConversationStore()
    s1 = store.get_or_create("sid-1")
    s2 = store.get_or_create("sid-1")
    assert s1 is s2


def test_store_note_writes_through_to_session():
    store = ConversationStore()
    store.get_or_create("sid-2")
    store.note("sid-2", goal="ship it", files_touched=["a.md", "b.md"], sticky={"k": "v"})
    s = store.get("sid-2")
    assert s.goal == "ship it"
    assert s.files_touched == ["a.md", "b.md"]
    assert s.sticky == {"k": "v"}


def test_store_jsonl_persistence_roundtrip(tmp_path):
    sessions_dir = tmp_path / "sessions"
    # Write
    store_a = ConversationStore()
    store_a.enable_jsonl_persistence(sessions_dir)
    store_a.get_or_create("sid-rt")
    store_a.note("sid-rt", goal="round-trip me", files_touched=["x.md"])
    # Re-open
    store_b = ConversationStore()
    store_b.enable_jsonl_persistence(sessions_dir)
    s = store_b.get("sid-rt")
    assert s is not None
    assert s.goal == "round-trip me"
    assert "x.md" in s.files_touched


# ───────────────────────────────────────────────────────────────────────
# MemoryEngine
# ───────────────────────────────────────────────────────────────────────


def test_summary_constants_are_what_clients_expect():
    """The JS client and Discord adapter depend on these literals."""
    assert SUMMARY_MARKER == "=== CONVERSATION SUMMARY ==="
    assert SUMMARY_END == "=== END SUMMARY ==="


def test_extract_summary_pulls_marker_blocks_only():
    msgs = [
        {"role": "system", "content": "You are a bot."},
        {"role": "system", "content": f"{SUMMARY_MARKER}\nThe user is named Sam.\n{SUMMARY_END}"},
        {"role": "user", "content": "hi"},
    ]
    summary = extract_summary(msgs)
    assert "The user is named Sam." in summary


def test_extract_summary_recognises_legacy_prefix():
    msgs = [{"role": "system", "content": "Prior conversation summary: foo bar"}]
    assert "foo bar" in extract_summary(msgs)


def test_merge_summary_wraps_in_markers():
    out = merge_summary("base prompt", "the user prefers Vietnamese")
    assert SUMMARY_MARKER in out
    assert SUMMARY_END in out
    assert "the user prefers Vietnamese" in out
    assert out.startswith("base prompt")


def test_merge_summary_strips_pre_wrapped_summary():
    """If the caller already wrapped the summary with markers, we
    unwrap and re-wrap so we don't end up with nested blocks."""
    inner = f"{SUMMARY_MARKER}\nfacts\n{SUMMARY_END}"
    out = merge_summary("base", inner)
    assert out.count(SUMMARY_MARKER) == 1
    assert out.count(SUMMARY_END) == 1


def test_memory_engine_estimate_tokens_is_proportional_to_chars():
    engine = MemoryEngine()
    msgs = [{"role": "user", "content": "x" * 280}]   # ~100 tokens at 2.8 chars/tok
    assert 90 <= engine.estimate_tokens(msgs) <= 110


def test_memory_engine_under_budget_does_not_compact():
    engine = MemoryEngine(max_tokens=100)
    msgs = [{"role": "user", "content": "short"}]
    result = engine.compact(msgs)
    assert not result.fired
    assert result.messages == msgs


def test_memory_engine_compact_no_op_without_summarizer():
    """Without a summarizer, compact returns the original messages
    untouched even when over budget — we don't drop state on the floor."""
    engine = MemoryEngine(max_tokens=10)
    long_msgs = [{"role": "user", "content": "x" * 5000}] * 20
    result = engine.compact(long_msgs, summarizer=None)
    assert result.messages is long_msgs
    assert not result.fired


def test_memory_engine_compact_uses_provided_summarizer():
    """When a summarizer is supplied and we're over budget, compact fires
    and produces a summary system message."""
    class _FakeResp:
        content = "1. User wants tests.\n2. Files: tests/."

    class _FakeProvider:
        def chat(self, msgs, options=None):
            return _FakeResp()

    engine = MemoryEngine(max_tokens=50, keep_recent_turns=2)
    long_msgs = [{"role": "user", "content": f"turn {i}: " + "x" * 200} for i in range(10)]
    result = engine.compact(long_msgs, summarizer=_FakeProvider())
    assert result.fired
    assert result.summary
    # First message should be the new system summary
    assert result.messages[0]["role"] == "system"
    assert SUMMARY_MARKER in result.messages[0]["content"]
    # Last messages should be the kept tail
    assert len(result.messages) <= 1 + 2 + 1   # system + tail + maybe non-summary system
