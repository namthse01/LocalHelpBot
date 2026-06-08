"""v5 — SkillStore: SKILL.md round-trip, retrieval, extraction."""
from __future__ import annotations

import pytest

from core import skills as sk


@pytest.fixture
def store(tmp_path):
    return sk.reset_skills_store_for_tests(root=tmp_path / "skills")


# ── SKILL.md format round-trip ────────────────────────────────────────

def test_skill_markdown_roundtrip():
    s = sk.Skill(
        name="open-pr", description="Open a PR from a branch",
        when_to_use="when asked to open a github PR",
        procedure=["git push origin HEAD", "gh pr create --fill"],
        pitfalls=["forgetting to push first"], tags=["git", "github"],
        confidence=0.9,
    )
    md = s.to_markdown()
    again = sk.Skill.from_markdown(md)
    assert again.name == "open-pr"
    assert again.procedure == ["git push origin HEAD", "gh pr create --fill"]
    assert again.pitfalls == ["forgetting to push first"]
    assert again.tags == ["git", "github"]
    assert abs(again.confidence - 0.9) < 1e-6


def test_slugify():
    assert sk.slugify("Open a PR from Branch!!") == "open-a-pr-from-branch"
    assert sk.slugify("") == "skill"


def test_slugify_transliterates_vietnamese():
    # Diacritics fold to readable ASCII instead of being dropped char-by-char.
    assert sk.slugify("Cách tạo tệp mới") == "cach-tao-tep-moi"
    assert sk.slugify("Đặt lại mật khẩu") == "dat-lai-mat-khau"
    # A title that is *only* non-ASCII still produces a usable slug, never "".
    assert sk.slugify("Tải lên") == "tai-len"
    # Pure non-foldable script degrades to the fallback (not garbage).
    assert sk.slugify("日本語") == "skill"


# ── Store persistence ─────────────────────────────────────────────────

def test_add_get_delete(store):
    s = store.add(title="Deploy app", when_to_use="on release",
                  procedure=["build", "push", "restart"], tags=["ops"])
    assert s is not None
    got = store.get("deploy-app")
    assert got is not None and got.procedure == ["build", "push", "restart"]
    assert store.has_title("Deploy app")
    assert store.delete("deploy-app") is True
    assert store.get("deploy-app") is None


def test_add_requires_title(store):
    assert store.add(title="  ", procedure=["x"]) is None


# ── Retrieval ─────────────────────────────────────────────────────────

def test_search_keyword_scoring(store):
    store.add(title="Open github PR", when_to_use="open a pull request",
              procedure=["git push", "gh pr create"], tags=["git", "github", "pr"])
    store.add(title="Resize an image", when_to_use="scale a picture",
              procedure=["pillow resize"], tags=["image"])
    hits = store.search("how do I open a github pull request")
    assert hits and hits[0].name == "open-github-pr"
    # Unrelated query returns nothing above threshold.
    assert store.search("quantum chromodynamics") == []


def test_render_for_prompt_block(store):
    store.add(title="Open github PR", when_to_use="open a pull request",
              procedure=["git push", "gh pr create"], tags=["git", "github"])
    block = store.render_for_prompt(query="open a github pr")
    assert "<skills>" in block and "open-github-pr" in block
    assert store.render_for_prompt(query="totally unrelated zzz") == ""


def test_drafts_excluded_from_search(store):
    s = store.add(title="Draft thing", procedure=["x"], tags=["draftword"])
    # Force draft status on disk.
    s.status = "draft"
    store.save(s)
    assert store.search("draftword") == []


# ── Auto-extraction ───────────────────────────────────────────────────

class _FakeProvider:
    def __init__(self, content):
        self._content = content

    def chat(self, messages, options=None):
        class R:
            pass
        r = R()
        r.content = self._content
        return r


def test_maybe_extract_skill_disabled_by_default(store, monkeypatch):
    # Default config has SKILLS_AUTO_EXTRACT False.
    out = sk.maybe_extract_skill([{"role": "user", "content": "x"}], rounds=3, tool_count=3, store=store)
    assert out is None


def test_maybe_extract_skill_parses_json(store, monkeypatch):
    import config
    monkeypatch.setattr(config, "SKILLS_AUTO_EXTRACT", True, raising=False)
    payload = (
        '{"title": "Build and push docker", "when_to_use": "deploying", '
        '"steps": ["docker build", "docker push"], "tags": ["docker"], "confidence": 0.8}'
    )
    fake = _FakeProvider(payload)
    convo = [
        {"role": "user", "content": "deploy the service"},
        {"role": "assistant", "content": "ran docker build then push"},
    ]
    out = sk.maybe_extract_skill(convo, rounds=2, tool_count=2, summarizer=fake, store=store)
    assert out is not None
    assert out.name == "build-and-push-docker"
    assert store.get("build-and-push-docker") is not None


def test_maybe_extract_skill_null_returns_none(store, monkeypatch):
    import config
    monkeypatch.setattr(config, "SKILLS_AUTO_EXTRACT", True, raising=False)
    fake = _FakeProvider("null")
    out = sk.maybe_extract_skill(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
        rounds=2, tool_count=2, summarizer=fake, store=store,
    )
    assert out is None


def test_maybe_extract_skill_below_threshold(store, monkeypatch):
    import config
    monkeypatch.setattr(config, "SKILLS_AUTO_EXTRACT", True, raising=False)
    fake = _FakeProvider('{"title":"x","steps":["a"],"confidence":0.9}')
    out = sk.maybe_extract_skill([{"role": "user", "content": "hi"}],
                                 rounds=1, tool_count=1, summarizer=fake, store=store)
    assert out is None


def test_low_confidence_dropped(store, monkeypatch):
    import config
    monkeypatch.setattr(config, "SKILLS_AUTO_EXTRACT", True, raising=False)
    fake = _FakeProvider('{"title":"flaky","steps":["a","b"],"confidence":0.2}')
    out = sk.maybe_extract_skill(
        [{"role": "user", "content": "do x"}, {"role": "assistant", "content": "did x"}],
        rounds=2, tool_count=2, summarizer=fake, store=store,
    )
    assert out is None
