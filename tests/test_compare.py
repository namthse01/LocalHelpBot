"""v6 — Compare (blind side-by-side model eval): store, run, vote, history.

All model calls go through an injected ``provider_factory`` so these tests use
a fake provider and never touch Ollama / the network.
"""
from __future__ import annotations

import pytest

from core.compare import (
    Comparison,
    ComparisonStore,
    reset_compare_store_for_tests,
    run_comparison,
)
from core.providers import ModelResponse


# ── fakes ──────────────────────────────────────────────────────────────────
class FakeProvider:
    """Stand-in for LocalProvider — canned answer, no network."""

    ANSWERS: dict = {}

    def __init__(self, model: str):
        self.model = model

    def chat(self, messages, options=None):
        ans = FakeProvider.ANSWERS.get(self.model, f"answer from {self.model}")
        return ModelResponse(
            content=ans,
            provider="fake",
            model=self.model,
            latency_ms=10,
            completion_tokens=7,
            tok_per_sec=42.0,
        )


class BoomProvider:
    """Provider whose chat() raises — exercises the per-model error slot."""

    def __init__(self, model: str):
        self.model = model

    def chat(self, messages, options=None):
        raise RuntimeError("model exploded")


def _factory(m: str):
    return FakeProvider(m)


@pytest.fixture
def fresh_store(tmp_path):
    return reset_compare_store_for_tests(path=tmp_path / "compare" / "comparisons.jsonl")


# ── run_comparison ──────────────────────────────────────────────────────────
def test_run_returns_one_blind_slot_per_model(fresh_store):
    comp = run_comparison("hi", ["m1", "m2", "m3"], provider_factory=_factory)
    pub = comp.public_dict(reveal=False)
    assert pub["n"] == 3
    assert len(pub["slots"]) == 3
    # Blind: no model names, winner, or pick-list leaked.
    for s in pub["slots"]:
        assert "model" not in s
        assert s["content"]
    assert "winner" not in pub
    assert "models" not in pub
    assert pub["voted"] is False
    assert pub["revealed"] is False


def test_run_persists_to_store(fresh_store):
    comp = run_comparison("hi", ["m1", "m2"], provider_factory=_factory)
    got = fresh_store.get(comp.id)
    assert got is not None
    assert got.id == comp.id
    assert {s["model"] for s in got.slots} == {"m1", "m2"}


def test_run_empty_prompt_raises(fresh_store):
    with pytest.raises(ValueError):
        run_comparison("   ", ["m1"], provider_factory=_factory)


def test_run_no_usable_models_raises(fresh_store):
    with pytest.raises(ValueError):
        run_comparison("hi", ["", "   "], provider_factory=_factory)


def test_run_dedupes_and_caps_models(fresh_store):
    comp = run_comparison(
        "hi", ["m1", "m1", "m2", "m3", "m4", "m5"],
        provider_factory=_factory, max_models=4,
    )
    # Dup m1 removed, then capped to the first 4 distinct names.
    assert comp.models == ["m1", "m2", "m3", "m4"]
    assert len(comp.slots) == 4


def test_run_blind_order_decoupled_from_pick_order(fresh_store, monkeypatch):
    # Force a deterministic non-identity shuffle so display != pick order.
    import core.compare as cmp_mod
    monkeypatch.setattr(cmp_mod.random, "shuffle", lambda lst: lst.reverse())
    comp = run_comparison("hi", ["a", "b", "c"], provider_factory=_factory)
    assert [s["model"] for s in comp.slots] == ["c", "b", "a"]
    assert comp.models == ["a", "b", "c"]   # pick order preserved


def test_run_per_model_error_slot(fresh_store):
    def factory(m):
        return BoomProvider(m) if m == "bad" else FakeProvider(m)

    comp = run_comparison("hi", ["good", "bad"], provider_factory=factory)
    by_model = {s["model"]: s for s in comp.slots}
    assert by_model["bad"]["error"]
    assert by_model["bad"]["content"] == ""
    assert by_model["good"]["error"] == ""
    assert by_model["good"]["content"]


# ── vote / reveal ───────────────────────────────────────────────────────────
def test_vote_maps_slot_to_model_and_reveals(fresh_store):
    comp = run_comparison("hi", ["m1", "m2", "m3"], provider_factory=_factory)
    winning_model = comp.slots[1]["model"]
    updated = fresh_store.set_winner(comp.id, 1)
    assert updated.voted is True
    assert updated.winner == winning_model
    pub = updated.public_dict(reveal=True)
    assert pub["winner"] == winning_model
    assert pub["revealed"] is True
    assert pub["slots"][1]["model"] == winning_model
    assert pub["slots"][1]["is_winner"] is True
    assert pub["models"] == ["m1", "m2", "m3"]


def test_vote_bad_slot_raises_valueerror(fresh_store):
    comp = run_comparison("hi", ["m1", "m2"], provider_factory=_factory)
    with pytest.raises(ValueError):
        fresh_store.set_winner(comp.id, 9)


def test_vote_unknown_id_raises_keyerror(fresh_store):
    with pytest.raises(KeyError):
        fresh_store.set_winner("does-not-exist", 0)


def test_voted_comparison_reload_keeps_winner(tmp_path):
    path = tmp_path / "compare" / "comparisons.jsonl"
    store = reset_compare_store_for_tests(path=path)
    comp = run_comparison("hi", ["m1", "m2"], provider_factory=_factory, store=store)
    win = comp.slots[0]["model"]
    store.set_winner(comp.id, 0)
    # Fresh instance against the same file reloads the voted state.
    reloaded = ComparisonStore(path=path)
    got = reloaded.get(comp.id)
    assert got is not None
    assert got.voted is True
    assert got.winner == win


# ── history ─────────────────────────────────────────────────────────────────
def test_history_most_recent_first_and_limit(fresh_store):
    ids = []
    for i in range(5):
        c = run_comparison(f"prompt {i}", ["m1", "m2"], provider_factory=_factory)
        ids.append(c.id)
    recent = fresh_store.list(limit=3)
    assert len(recent) == 3
    assert [c.id for c in recent] == list(reversed(ids))[:3]


def test_summary_dict_shape(fresh_store):
    comp = run_comparison("hello world", ["m1", "m2"], provider_factory=_factory)
    fresh_store.set_winner(comp.id, 0)
    s = fresh_store.get(comp.id).summary_dict()
    assert set(s.keys()) >= {"id", "ts", "prompt", "models", "winner", "voted", "n"}
    assert s["models"] == ["m1", "m2"]
    assert s["voted"] is True


def test_clear_wipes_store(fresh_store):
    run_comparison("hi", ["m1"], provider_factory=_factory)
    assert fresh_store.clear() >= 1
    assert fresh_store.all() == []


def test_blind_public_dict_hides_then_reveals(fresh_store):
    comp = run_comparison("hi", ["m1", "m2"], provider_factory=_factory)
    blind = comp.public_dict(reveal=False)
    assert all("model" not in s for s in blind["slots"])
    revealed = comp.public_dict(reveal=True)
    assert all("model" in s for s in revealed["slots"])
