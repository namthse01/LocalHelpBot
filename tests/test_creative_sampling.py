"""Tests for creative-mode detection + sampling-option threading.

Covers:
  * `_looks_creative` — heuristic regex over EN + VI creative requests.
  * `run_agent` passes SAMPLING_CREATIVE to the provider on creative
    user messages, None (= provider default) otherwise.
"""
from __future__ import annotations

import pytest

from core import agent as agent_mod
from core.agent import _looks_creative
from core.providers import SAMPLING_CREATIVE
from core.tool_schema import Tool, ToolRegistry, ToolResult


# ───────────────────────────────────────────────────────────────────────
# _looks_creative
# ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "msg",
    [
        "write me a short story about a cat",
        "Write a chapter where the detective walks in",
        "Compose a poem about loss",
        "narrate a scene at midnight",
        "describe a character with a scar",
        "continue the scene with more dialogue",
        "let's do a roleplay",
        "role-play the priest from chapter 2",
        "RP as the bartender",
        "give me some NSFW writing",
        "18+ scene please",
        "viết một truyện ngắn về một chàng trai",
        "kể chuyện về thám tử lúc nửa đêm",
        "sáng tác một bài thơ về biển",
        "miêu tả một nhân vật phản diện",
        "tả lại cảnh hoàng hôn",
        "tiếp tục câu chuyện",
        "viết một đoạn người lớn",
    ],
)
def test_looks_creative_positives(msg):
    assert _looks_creative(msg), f"expected True for {msg!r}"


@pytest.mark.parametrize(
    "msg",
    [
        "",
        "read file x.py",
        "+ 4",
        "what is 1+1",
        "list files in this directory",
        "đọc file config.py",
        "ls",
        "run pytest",
        "fix the bug in agent.py",
        "summarize this PDF",
    ],
)
def test_looks_creative_negatives(msg):
    assert not _looks_creative(msg), f"expected False for {msg!r}"


# ───────────────────────────────────────────────────────────────────────
# run_agent threads options through smart_provider
# ───────────────────────────────────────────────────────────────────────


class _RecordingProvider:
    """Captures every (messages, options) pair handed to .chat()."""

    def __init__(self, content="Done."):
        self.content = content
        self.calls: list = []

    def chat(self, messages, options=None):
        self.calls.append({"options": options})

        class _R:
            def __init__(self_, c):
                self_.content = c
                self_.provider = "fake"
                self_.model = "fake-7b"

            def timing_str(self_):
                return "(test)"

        return _R(self.content)


def _empty_registry():
    return ToolRegistry([])


def test_creative_message_gets_sampling_creative(monkeypatch):
    rec = _RecordingProvider(content="Once upon a time, the cat...")
    monkeypatch.setattr(agent_mod, "smart_provider", rec)

    agent_mod.run_agent(
        [{"role": "user", "content": "write me a short story about a cat"}],
        _empty_registry(),
        max_turns=2,
    )

    assert rec.calls, "provider was never called"
    first = rec.calls[0]
    assert first["options"] == SAMPLING_CREATIVE, (
        f"creative message did not get SAMPLING_CREATIVE; got {first['options']!r}"
    )


def test_non_creative_message_passes_none_options(monkeypatch):
    rec = _RecordingProvider(content="There are 3 files: a, b, c.")
    monkeypatch.setattr(agent_mod, "smart_provider", rec)

    agent_mod.run_agent(
        [{"role": "user", "content": "list files in this directory"}],
        _empty_registry(),
        max_turns=2,
    )

    assert rec.calls
    assert rec.calls[0]["options"] is None, (
        "non-creative message should pass options=None so the provider picks its default"
    )


def test_vietnamese_creative_message_gets_sampling_creative(monkeypatch):
    rec = _RecordingProvider(content="Trong đêm tối, thám tử bước vào...")
    monkeypatch.setattr(agent_mod, "smart_provider", rec)

    agent_mod.run_agent(
        [{"role": "user", "content": "viết một truyện ngắn 800 từ về một thám tử"}],
        _empty_registry(),
        max_turns=2,
    )

    assert rec.calls
    assert rec.calls[0]["options"] == SAMPLING_CREATIVE
