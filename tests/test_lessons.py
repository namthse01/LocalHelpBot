"""Slice 4 — LessonsStore persistence + render + auto-capture."""
from __future__ import annotations

import pytest

from core.lessons import LessonsStore, reset_lessons_store_for_tests
from core import orchestrator as orch_mod


@pytest.fixture
def fresh_store(tmp_path):
    """Always use a fresh per-test JSONL so tests are hermetic."""
    return reset_lessons_store_for_tests(path=tmp_path / "lessons.jsonl")


def test_record_then_list_returns_global_lessons(fresh_store):
    fresh_store.record(lesson="prefer Vietnamese", scope="global")
    out = fresh_store.list(limit=10)
    assert len(out) == 1
    assert out[0].lesson == "prefer Vietnamese"
    assert out[0].scope == "global"


def test_record_empty_lesson_returns_none(fresh_store):
    assert fresh_store.record(lesson="   ") is None
    assert fresh_store.list() == []


def test_list_session_scoped_only_when_session_id_matches(fresh_store):
    fresh_store.record(lesson="global rule", scope="global")
    fresh_store.record(lesson="session rule", scope="session:abc")
    fresh_store.record(lesson="other session", scope="session:xyz")

    # No session_id filter — should pull global only (session scopes
    # are tied to a specific sid).
    out = fresh_store.list(session_id=None, limit=10)
    assert {e.lesson for e in out} == {"global rule"}

    # With session_id, global + that session's lessons.
    out = fresh_store.list(session_id="abc", limit=10)
    assert {e.lesson for e in out} == {"global rule", "session rule"}


def test_render_for_prompt_returns_empty_when_no_lessons(fresh_store):
    assert fresh_store.render_for_prompt() == ""


def test_render_for_prompt_wraps_in_lessons_learned_block(fresh_store):
    fresh_store.record(lesson="answer in Vietnamese")
    block = fresh_store.render_for_prompt()
    assert block.startswith("<lessons_learned>")
    assert block.endswith("</lessons_learned>")
    assert "answer in Vietnamese" in block


def test_max_entries_cap_enforced(fresh_store):
    for i in range(220):
        fresh_store.record(lesson=f"lesson_{i}", scope="global")
    all_ = fresh_store.all()
    assert len(all_) == 200
    lessons = {e.lesson for e in all_}
    # Oldest 20 should have been trimmed, most recent retained.
    assert "lesson_219" in lessons
    assert "lesson_5" not in lessons   # oldest 20 dropped
    assert "lesson_20" in lessons       # boundary: index 20 is the first kept


def test_jsonl_persists_across_store_instances(tmp_path):
    path = tmp_path / "lessons.jsonl"
    a = LessonsStore(path=path)
    a.record(lesson="cross-instance check", scope="global")
    # New instance against the same file should see the lesson.
    b = LessonsStore(path=path)
    out = b.list(limit=5)
    assert any("cross-instance check" in e.lesson for e in out)


def test_clear_wipes_in_memory_and_on_disk(fresh_store, tmp_path):
    fresh_store.record(lesson="x")
    n = fresh_store.clear()
    assert n == 1
    assert fresh_store.all() == []
    # File should be gone too.
    assert not fresh_store._path.exists()


# ───────────────────────────────────────────────────────────────────────
# Auto-capture (Slice 4.3)
# ───────────────────────────────────────────────────────────────────────


def test_auto_capture_off_by_default(fresh_store, monkeypatch):
    """LESSONS_AUTO_CAPTURE defaults to False; the helper should no-op."""
    import config
    monkeypatch.setattr(config, "LESSONS_AUTO_CAPTURE", False, raising=False)
    orch_mod._maybe_capture_correction(
        [{"role": "assistant", "content": "the answer is 5"}],
        "no, actually it's 7",
        "test-session",
    )
    assert fresh_store.list() == []


def test_auto_capture_persists_correction_when_enabled(fresh_store, monkeypatch):
    import config
    monkeypatch.setattr(config, "LESSONS_AUTO_CAPTURE", True, raising=False)
    orch_mod._maybe_capture_correction(
        [{"role": "assistant", "content": "the answer is 5"}],
        "no, actually it's 7",
        "test-session",
    )
    out = fresh_store.list(limit=5)
    assert len(out) == 1
    assert "actually" in out[0].lesson.lower() or "no" in out[0].lesson.lower()
    assert "answer is 5" in out[0].trigger


def test_auto_capture_ignores_messages_that_arent_corrections(fresh_store, monkeypatch):
    import config
    monkeypatch.setattr(config, "LESSONS_AUTO_CAPTURE", True, raising=False)
    orch_mod._maybe_capture_correction(
        [{"role": "assistant", "content": "the answer is 5"}],
        "great, thanks!",
        "test-session",
    )
    assert fresh_store.list() == []


def test_auto_capture_detects_vietnamese_correction(fresh_store, monkeypatch):
    import config
    monkeypatch.setattr(config, "LESSONS_AUTO_CAPTURE", True, raising=False)
    orch_mod._maybe_capture_correction(
        [{"role": "assistant", "content": "OK"}],
        "không phải, kết quả phải là 7",
        "sid",
    )
    assert len(fresh_store.list(limit=5)) == 1


# ───────────────────────────────────────────────────────────────────────
# Lessons → context T1 integration
# ───────────────────────────────────────────────────────────────────────


def test_lessons_appear_in_assembled_context(fresh_store):
    from core.context import assemble_context
    from core.conversation_store import get_store
    sid = "lessons-context-sid"
    get_store().get_or_create(sid)
    fresh_store.record(lesson="answer concisely; bullet points preferred")

    ctx = assemble_context(
        session_id=sid,
        base_prompt="You are an agent.",
        tools=[],
        history=[],
        runtime_block="",
        profile_name="main",
    )
    assert "<lessons_learned>" in ctx.system
    assert "answer concisely" in ctx.system
