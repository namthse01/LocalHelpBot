"""v6 — Documents: living-documents store + agent tools.

Store tests drive :class:`core.documents.DocumentsStore` through a temp JSONL
path; tool tests call the :mod:`core.plugins.documents_tools` handlers directly
(they go through the module singleton, which the fixture repoints at a temp
file). A controllable clock is injected so the 60-second version-coalesce
window is deterministic. No network, no Ollama.
"""
from __future__ import annotations

import pytest

from core import documents as dm
from core.plugins import documents_tools as tools
from core.tool_schema import ErrorCode


class _Clock:
    """Monotonic-ish fake clock; advance with ``tick``."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = float(t)

    def time(self) -> float:
        return self.t

    def tick(self, dt: float) -> float:
        self.t += float(dt)
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(dm, "time", c)
    return c


@pytest.fixture
def store(tmp_path, clock):
    return dm.DocumentsStore(path=tmp_path / "documents" / "documents.jsonl")


@pytest.fixture
def tool_store(tmp_path, clock):
    """Repoint the singleton at a temp path for tool-handler tests."""
    return dm.reset_documents_store_for_tests(path=tmp_path / "documents.jsonl")


@pytest.fixture(autouse=True)
def _restore_singleton():
    yield
    dm.reset_documents_store_for_tests()  # restore default store after each test


# ── derive_title ─────────────────────────────────────────────────────────
def test_derive_title_markdown_header():
    assert dm.derive_title("# Hello World\n\nbody") == "Hello World"
    assert dm.derive_title("### Deep heading") == "Deep heading"


def test_derive_title_html_heading():
    assert dm.derive_title("<h2>From HTML</h2><p>x</p>") == "From HTML"


def test_derive_title_first_line():
    assert dm.derive_title("Just a sentence here\nmore text") == "Just a sentence here"


def test_derive_title_blank_is_untitled():
    assert dm.derive_title("   \n  ") == "Untitled"
    assert dm.derive_title(None) == "Untitled"  # type: ignore[arg-type]


def test_derive_title_clips_long():
    t = dm.derive_title("# " + "x" * 200)
    assert len(t) <= 80 and t.endswith("…")


# ── detect_language ──────────────────────────────────────────────────────
@pytest.mark.parametrize("content,expected", [
    ("", "markdown"),
    ("# Title\n- a\n- b", "markdown"),
    ("<!DOCTYPE html><html><body>x</body></html>", "html"),
    ('{"a": 1, "b": [2, 3]}', "json"),
    ("import os\n\ndef f():\n    return 1", "python"),
    ("just some plain words with no structure", "text"),
])
def test_detect_language(content, expected):
    assert dm.detect_language(content) == expected


# ── Store: add / get ─────────────────────────────────────────────────────
def test_add_creates_v1(store):
    d = store.add(content="# My Doc\n\nhello")
    assert d.id and d.title == "My Doc"
    assert d.language == "markdown"
    assert d.version_count == 1
    assert d.versions[0]["summary"] == "Initial version"
    assert d.versions[0]["content"] == "# My Doc\n\nhello"
    assert not d.archived


def test_add_explicit_title_and_language(store):
    d = store.add(title="Custom", content="x", language="text")
    assert d.title == "Custom" and d.language == "text"


def test_get_roundtrip(store):
    d = store.add(content="alpha")
    got = store.get(d.id)
    assert got is not None and got.content == "alpha"
    assert store.get("nope") is None


# ── Store: update / version coalescing ───────────────────────────────────
def test_update_coalesces_within_window(store, clock):
    d = store.add(content="v1 body")
    clock.tick(30)  # within 60s, same source ("ui")
    upd = store.update(d.id, content="v1 edited")
    assert upd is not None
    assert upd.version_count == 1                 # coalesced, no new version
    assert upd.content == "v1 edited"
    assert upd.versions[0]["content"] == "v1 edited"


def test_update_new_version_after_window(store, clock):
    d = store.add(content="v1 body")
    clock.tick(120)  # past 60s window
    upd = store.update(d.id, content="v2 body", summary="big change")
    assert upd.version_count == 2
    assert upd.versions[-1]["content"] == "v2 body"
    assert upd.versions[-1]["summary"] == "big change"
    assert upd.versions[-1]["version_number"] == 2


def test_update_unchanged_content_no_new_version(store, clock):
    d = store.add(content="same")
    clock.tick(120)
    upd = store.update(d.id, content="same")
    assert upd.version_count == 1                 # identical content → no version


def test_update_metadata_only_no_version(store, clock):
    d = store.add(content="body")
    clock.tick(120)
    upd = store.update(d.id, title="Renamed", language="text")
    assert upd.title == "Renamed" and upd.language == "text"
    assert upd.version_count == 1                 # metadata edit → no version


def test_update_missing_doc_returns_none(store):
    assert store.update("ghost", content="x") is None


def test_update_different_source_does_not_coalesce(store, clock):
    d = store.add(content="orig")               # source "ui"
    clock.tick(10)                               # within window…
    upd = store.update(d.id, content="agent edit", source="agent")
    assert upd.version_count == 2                 # …but different source → new version


# ── Store: version trim ──────────────────────────────────────────────────
def test_versions_trimmed_to_cap(store, clock, monkeypatch):
    monkeypatch.setattr(dm, "DEFAULT_MAX_VERSIONS", 4)
    # store reads cap via _cfg → config.DOCUMENTS_MAX_VERSIONS; force small cap
    monkeypatch.setattr(store, "_max_versions", lambda: 4)
    d = store.add(content="c0")
    for i in range(1, 10):
        clock.tick(120)                          # force a new version each time
        store.update(d.id, content=f"c{i}")
    got = store.get(d.id)
    assert got.version_count == 4
    assert got.versions[0]["version_number"] == 1   # origin kept
    assert got.versions[-1]["content"] == "c9"      # latest kept


# ── Store: delete / archive ──────────────────────────────────────────────
def test_delete(store):
    d = store.add(content="bye")
    assert store.delete(d.id) is True
    assert store.get(d.id) is None
    assert store.delete(d.id) is False


def test_toggle_archive(store):
    d = store.add(content="x")
    assert store.toggle_archive(d.id).archived is True
    assert store.toggle_archive(d.id).archived is False
    assert store.toggle_archive("ghost") is None


# ── Store: restore_version ───────────────────────────────────────────────
def test_restore_version(store, clock):
    d = store.add(content="original")
    clock.tick(120)
    store.update(d.id, content="rewritten")
    assert d.version_count == 2
    clock.tick(120)
    restored = store.restore_version(d.id, 1)
    assert restored is not None
    assert restored.content == "original"        # back to v1 content
    assert restored.version_count == 3           # …as a NEW version
    assert "Restored from v1" in restored.versions[-1]["summary"]


def test_restore_version_bad_number(store, clock):
    d = store.add(content="x")
    assert store.restore_version(d.id, 99) is None
    assert store.restore_version("ghost", 1) is None


# ── Store: list (search / facet / sort) ──────────────────────────────────
def test_list_search_and_terms(store):
    store.add(title="Alpha", content="quick brown fox")
    store.add(title="Beta", content="lazy dog sleeps")
    res = store.list(query="brown fox")
    assert [d.title for d in res] == ["Alpha"]
    assert store.list(query="brown nonexistent") == []   # AND: all terms required


def test_list_language_facet(store):
    store.add(content="# md doc")
    store.add(content="plain words only", language="text")
    res = store.list(language="text")
    assert len(res) == 1 and res[0].language == "text"
    assert set(store.languages()) == {"markdown", "text"}


def test_list_excludes_archived_by_default(store):
    a = store.add(content="visible")
    b = store.add(content="hidden")
    store.toggle_archive(b.id)
    assert [d.id for d in store.list()] == [a.id]
    assert len(store.list(include_archived=True)) == 2


def test_list_sort_modes(store, clock):
    a = store.add(title="banana", content="a")
    clock.tick(120)
    b = store.add(title="apple", content="b")
    clock.tick(120)
    store.update(b.id, content="b2")             # b now has 2 versions, newest
    recent = [d.title for d in store.list(sort="recent")]
    assert recent[0] == "apple"                  # most recently updated first
    oldest = [d.title for d in store.list(sort="oldest")]
    assert oldest[0] == "banana"
    alpha = [d.title for d in store.list(sort="alpha")]
    assert alpha == ["apple", "banana"]
    edits = [d.title for d in store.list(sort="edits")]
    assert edits[0] == "apple"                   # most versions first


# ── Store: cap on number of documents ────────────────────────────────────
def test_max_entries_cap(store, monkeypatch):
    monkeypatch.setattr(store, "_max_entries", lambda: 3)
    ids = [store.add(content=f"doc {i}").id for i in range(5)]
    remaining = {d.id for d in store.all()}
    assert len(remaining) == 3
    assert ids[0] not in remaining and ids[-1] in remaining  # oldest trimmed


# ── Store: persistence round-trip ────────────────────────────────────────
def test_persistence_roundtrip(tmp_path, clock):
    path = tmp_path / "documents.jsonl"
    s1 = dm.DocumentsStore(path=path)
    d = s1.add(content="persist me")
    clock.tick(120)
    s1.update(d.id, content="persist me v2")
    # Fresh store reading the same file.
    s2 = dm.DocumentsStore(path=path)
    got = s2.get(d.id)
    assert got is not None
    assert got.content == "persist me v2"
    assert got.version_count == 2


def test_load_backfills_v1_when_missing(tmp_path):
    path = tmp_path / "documents.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    # A legacy/hand-written row with content but no versions list.
    path.write_text('{"id": "abc", "ts": 1, "content": "legacy body"}\n', encoding="utf-8")
    s = dm.DocumentsStore(path=path)
    got = s.get("abc")
    assert got is not None and got.version_count == 1
    assert got.versions[0]["content"] == "legacy body"


# ── Store: tidy ──────────────────────────────────────────────────────────
def test_tidy_flags_junk_dryrun(store):
    junk = store.add(title="test", content="x")            # junk title + tiny
    keep = store.add(title="Real", content="lots of meaningful content here")
    out = store.tidy()                                       # dry-run
    assert junk.id in out["junk"]
    assert keep.id not in out["junk"]
    assert out["removed"] == []                             # nothing deleted
    assert store.get(junk.id) is not None


def test_tidy_apply_deletes(store):
    junk = store.add(title="asdf", content="")
    keep = store.add(title="Keeper", content="substantial real content body")
    out = store.tidy(apply=True)
    assert junk.id in out["removed"]
    assert store.get(junk.id) is None
    assert store.get(keep.id) is not None


def test_tidy_dedup_keeps_longest(store, clock):
    a = store.add(title="Doc A", content="shared content")
    clock.tick(1)
    b = store.add(title="Doc B", content="shared content extra longer body")
    out = store.tidy(apply=True)
    # identical fingerprint? No — different content. Make them identical:
    assert out["duplicates"] == []  # different content → not dupes


def test_tidy_dedup_identical(store, clock):
    a = store.add(title="One", content="exact same body")
    clock.tick(5)
    b = store.add(title="Two", content="exact same body")
    out = store.tidy(apply=True)
    # one of them removed; the surviving has the same fingerprint
    survivors = [d.id for d in store.all()]
    assert len(survivors) == 1
    assert (a.id in out["removed"]) ^ (b.id in out["removed"])  # exactly one


# ── Tools ────────────────────────────────────────────────────────────────
def test_tool_create_and_list(tool_store):
    r = tools._create_document({"content": "# Tool Doc\n\nbody"})
    assert r.ok and r.meta.get("document_id")
    listed = tools._list_documents({})
    assert listed.ok and "Tool Doc" in listed.output


def test_tool_create_requires_content_or_title(tool_store):
    r = tools._create_document({"content": "   "})
    assert not r.ok and r.error_code == ErrorCode.INVALID_ARGS


def test_tool_get(tool_store):
    cid = tools._create_document({"title": "Readable", "content": "the body text"}).meta["document_id"]
    r = tools._get_document({"id": cid})
    assert r.ok and "the body text" in r.output
    assert tools._get_document({"id": "ghost"}).error_code == ErrorCode.FILE_NOT_FOUND
    assert tools._get_document({}).error_code == ErrorCode.INVALID_ARGS


def test_tool_get_truncates(tool_store, monkeypatch):
    monkeypatch.setattr(tools, "_GET_MAX_CHARS", 20)
    cid = tools._create_document({"content": "y" * 100}).meta["document_id"]
    r = tools._get_document({"id": cid})
    assert r.ok and r.meta["truncated"] is True
    assert "truncated at 20 chars" in r.output


def test_tool_update(tool_store, clock):
    cid = tools._create_document({"content": "first"}).meta["document_id"]
    clock.tick(120)
    r = tools._update_document({"id": cid, "content": "second", "summary": "rev"})
    assert r.ok and r.meta["version_count"] == 2
    assert tools._update_document({"id": "ghost", "content": "x"}).error_code == ErrorCode.FILE_NOT_FOUND
    assert tools._update_document({"id": cid}).error_code == ErrorCode.INVALID_ARGS


def test_tool_delete(tool_store):
    cid = tools._create_document({"content": "temp"}).meta["document_id"]
    assert tools._delete_document({"id": cid}).ok
    assert tools._delete_document({"id": cid}).error_code == ErrorCode.FILE_NOT_FOUND
    assert tools._delete_document({}).error_code == ErrorCode.INVALID_ARGS


def test_tools_registered():
    from core.tool_schema import ToolRegistry
    reg = ToolRegistry()
    tools.register(reg)
    for name in ("create_document", "list_documents", "get_document",
                 "update_document", "delete_document"):
        t = reg.get(name)
        assert t is not None and t.category == "documents"
