"""Unit tests for the <recent_exchange> reference-resolution path.

Covers:
  * `_extract_last_number` — pulls a numeric answer out of free-form text.
  * `Session.note_answer` — dual-slot storage; numeric only overwrites
    when present, short always overwrites.
  * `<recent_exchange>` rendering in T2 — appears in `assemble_context`
    output when slots are populated, omitted when empty.
  * `_try_arithmetic_fastpath` — operator-leading inputs evaluate against
    `last_numeric_value` deterministically; everything else returns None.
"""
from __future__ import annotations

import pytest

from core.agent import _extract_last_number, _try_arithmetic_fastpath
from core.context import assemble_context
from core.conversation_store import Session, get_store


# ───────────────────────────────────────────────────────────────────────
# _extract_last_number
# ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1+1 = 2", 2.0),
        ("the answer is 42", 42.0),
        ("Total: 1,234.5 items", 1234.5),
        ("price was 1.234,56 €", 1234.56),  # European format
        ("2.5 * 2 = 5", 5.0),                # picks LAST number
        ("", None),
        ("done — no numbers here", None),
        ("Wrote to C:/out.pdf", None),       # path-like, no standalone number
        ("-3.14 negative", -3.14),
    ],
)
def test_extract_last_number(text, expected):
    got = _extract_last_number(text)
    if expected is None:
        assert got is None, f"expected None, got {got!r}"
    else:
        assert got == pytest.approx(expected), f"text={text!r}"


# ───────────────────────────────────────────────────────────────────────
# Session.note_answer — dual-slot semantics
# ───────────────────────────────────────────────────────────────────────


def test_session_note_answer_sets_both_slots():
    sess = Session(sid="t")
    sess.note_answer("1+1 = 2", 2.0)
    assert sess.last_short_answer == "1+1 = 2"
    assert sess.last_numeric_value == 2.0


def test_session_note_answer_short_overwrites_always():
    sess = Session(sid="t")
    sess.note_answer("answer was 5", 5.0)
    sess.note_answer("not a number this time", None)
    # Short overwritten; numeric preserved (None should NOT erase 5.0).
    assert sess.last_short_answer == "not a number this time"
    assert sess.last_numeric_value == 5.0


def test_session_note_answer_caps_short_to_200_chars():
    sess = Session(sid="t")
    long_text = "x" * 500 + " = 7"
    sess.note_answer(long_text, 7.0)
    assert sess.last_short_answer is not None
    assert len(sess.last_short_answer) == 200
    # We keep the tail so the actual answer is preserved.
    assert sess.last_short_answer.endswith("= 7")
    assert sess.last_numeric_value == 7.0


def test_session_note_answer_empty_short_keeps_existing():
    sess = Session(sid="t")
    sess.note_answer("hello", None)
    sess.note_answer("", None)
    assert sess.last_short_answer == "hello"


# ───────────────────────────────────────────────────────────────────────
# T2 rendering
# ───────────────────────────────────────────────────────────────────────


def test_recent_exchange_appears_in_t2_when_populated(monkeypatch):
    monkeypatch.setattr("core.conversation_store._store", None, raising=False)
    sid = "rx-populated"
    sess = get_store().get_or_create(sid)
    sess.note_answer("1+1 = 2", 2.0)

    ctx = assemble_context(
        session_id=sid,
        base_prompt="You are main.",
        tools=[],
        history=[],
        runtime_block="",
        profile_name="main",
    )
    assert "<session>" in ctx.system
    assert "recent_exchange:" in ctx.system
    assert "last_assistant_answer:" in ctx.system
    assert "1+1 = 2" in ctx.system
    assert "last_numeric_value: 2" in ctx.system


def test_recent_exchange_omitted_when_empty(monkeypatch):
    monkeypatch.setattr("core.conversation_store._store", None, raising=False)
    sid = "rx-empty"
    get_store().get_or_create(sid)
    # No note_answer call — both slots remain None.

    ctx = assemble_context(
        session_id=sid,
        base_prompt="You are main.",
        tools=[],
        history=[],
        runtime_block="",
        profile_name="main",
    )
    assert "recent_exchange:" not in ctx.system
    assert "last_assistant_answer" not in ctx.system


# ───────────────────────────────────────────────────────────────────────
# Arithmetic fast-path
# ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "msg,anchor,expected",
    [
        ("+ 4", 2.0, "2 + 4 = 6"),
        ("- 1", 5.0, "5 - 1 = 4"),
        ("* 2", 2.5, "2.5 * 2 = 5"),
        ("/2", 10.0, "10 / 2 = 5"),
        ("  +   3   ", 7.0, "7 + 3 = 10"),
        ("+4", 2.0, "2 + 4 = 6"),
        ("- -3", 5.0, "5 - -3 = 8"),
    ],
)
def test_fastpath_evaluates_operator_leading_messages(msg, anchor, expected):
    assert _try_arithmetic_fastpath(msg, anchor) == expected


def test_fastpath_returns_none_without_anchor():
    assert _try_arithmetic_fastpath("+ 4", None) is None


def test_fastpath_returns_none_for_non_pure_message():
    # Anything beyond `<op> <number>` should defer to the LLM so the
    # pronoun policy can do nuanced binding.
    assert _try_arithmetic_fastpath("what's the result + 4", 2.0) is None
    assert _try_arithmetic_fastpath("add 4 to that", 2.0) is None
    assert _try_arithmetic_fastpath("1 + 1", 2.0) is None  # leading digit, not operator
    assert _try_arithmetic_fastpath("", 2.0) is None


def test_fastpath_handles_divide_by_zero_gracefully():
    out = _try_arithmetic_fastpath("/ 0", 5.0)
    assert out is not None
    assert "0" in out
    assert "undefined" in out or "divide by zero" in out
