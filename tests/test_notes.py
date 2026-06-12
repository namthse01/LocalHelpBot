"""v6 — Notes: Keep-style notes + checklists store and agent tools.

Store tests drive :class:`core.notes.NotesStore` through a temp JSONL path;
tool tests call the :mod:`core.plugins.notes_tools` handlers directly (they go
through the module singleton, which the fixture repoints at a temp file). No
network, no Ollama.
"""
from __future__ import annotations

import pytest

from core import notes as nt
from core.plugins import notes_tools as tools
from core.tool_schema import ErrorCode


@pytest.fixture
def store(tmp_path):
    return nt.reset_notes_store_for_tests(path=tmp_path / "notes" / "notes.jsonl")


@pytest.fixture(autouse=True)
def _restore_singleton():
    yield
    nt.reset_notes_store_for_tests()  # restore the default store after each test


# ── Store: create + type detection ───────────────────────────────────────
def test_add_plain_note(store):
    n = store.add(title="Buy milk", content="2% organic")
    assert n.note_type == nt.NOTE
    assert n.title == "Buy milk"
    assert n.content == "2% organic"
    assert n.items == []
    assert n.id and not n.archived and not n.pinned


def test_add_checklist_autodetects_type(store):
    n = store.add(title="Groceries", items=["milk", "eggs", "bread"])
    assert n.note_type == nt.CHECKLIST
    assert [it["text"] for it in n.items] == ["milk", "eggs", "bread"]
    assert all(it["done"] is False for it in n.items)


def test_items_accept_newline_string(store):
    n = store.add(title="x", items="a\n\nb\n c ")
    assert [it["text"] for it in n.items] == ["a", "b", "c"]


def test_explicit_note_type_overrides(store):
    n = store.add(title="empty checklist", note_type="checklist")
    assert n.note_type == nt.CHECKLIST and n.items == []


# ── Store: get / update / delete ─────────────────────────────────────────
def test_get_and_update(store):
    n = store.add(title="draft")
    got = store.get(n.id)
    assert got is not None and got.title == "draft"
    upd = store.update(n.id, title="final", label="work")
    assert upd.title == "final" and upd.label == "work"
    assert upd.updated >= upd.ts


def test_update_missing_returns_none(store):
    assert store.update("nope", title="x") is None


def test_adding_items_promotes_note_to_checklist(store):
    n = store.add(title="plan", content="body")
    upd = store.update(n.id, items=["step 1", "step 2"])
    assert upd.note_type == nt.CHECKLIST
    assert len(upd.items) == 2


def test_delete(store):
    n = store.add(title="temp")
    assert store.delete(n.id) is True
    assert store.get(n.id) is None
    assert store.delete(n.id) is False


# ── Store: toggles ───────────────────────────────────────────────────────
def test_toggle_pin_and_archive(store):
    n = store.add(title="t")
    assert store.toggle_pin(n.id).pinned is True
    assert store.toggle_pin(n.id).pinned is False
    assert store.toggle_archive(n.id).archived is True
    assert store.toggle_archive(n.id).archived is False


def test_toggle_item_flips_done(store):
    n = store.add(title="c", items=["a", "b"])
    upd = store.toggle_item(n.id, 1)
    assert upd.items[1]["done"] is True
    assert upd.items[0]["done"] is False
    assert store.toggle_item(n.id, 1).items[1]["done"] is False


def test_toggle_item_out_of_range(store):
    n = store.add(title="c", items=["a"])
    assert store.toggle_item(n.id, 5) is None
    assert store.toggle_item("missing", 0) is None


# ── Store: listing, filters, ordering ────────────────────────────────────
def test_list_excludes_archived_by_default(store):
    a = store.add(title="keep")
    b = store.add(title="gone")
    store.toggle_archive(b.id)
    titles = [n.title for n in store.list()]
    assert "keep" in titles and "gone" not in titles
    assert "gone" in [n.title for n in store.list(include_archived=True)]


def test_list_pinned_first(store):
    store.add(title="first")
    second = store.add(title="second")
    store.toggle_pin(second.id)
    order = [n.title for n in store.list()]
    assert order[0] == "second"  # pinned floats up regardless of recency


def test_list_label_and_query_filters(store):
    store.add(title="alpha", label="work", content="report")
    store.add(title="beta", label="home", content="garden")
    assert [n.title for n in store.list(label="work")] == ["alpha"]
    assert [n.title for n in store.list(query="garden")] == ["beta"]
    # query also searches checklist item text
    store.add(title="gamma", items=["water the plants"])
    assert {n.title for n in store.list(query="plants")} == {"gamma"}


def test_labels(store):
    store.add(title="a", label="work")
    store.add(title="b", label="home")
    store.add(title="c", label="work")
    assert store.labels() == ["home", "work"]


def test_reorder(store):
    a = store.add(title="a")
    b = store.add(title="b")
    c = store.add(title="c")
    store.reorder([c.id, a.id, b.id])
    assert [n.title for n in store.list()] == ["c", "a", "b"]


# ── Store: persistence + cap ─────────────────────────────────────────────
def test_persistence_reload(tmp_path):
    path = tmp_path / "notes" / "notes.jsonl"
    s1 = nt.reset_notes_store_for_tests(path=path)
    s1.add(title="persisted", items=["x"])
    s2 = nt.NotesStore(path=path)  # fresh instance, same file
    rows = s2.all()
    assert len(rows) == 1 and rows[0].title == "persisted"
    assert rows[0].note_type == nt.CHECKLIST


def test_max_entries_cap(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "NOTES_MAX_ENTRIES", 3, raising=False)
    s = nt.reset_notes_store_for_tests(path=tmp_path / "notes" / "notes.jsonl")
    for i in range(5):
        s.add(title=f"n{i}")
    assert len(s.all()) == 3
    assert [n.title for n in s.all()] == ["n2", "n3", "n4"]


# ── Tools (via plugin handlers) ──────────────────────────────────────────
def test_tool_add_note(store):
    r = tools._add_note({"title": "Call dentist"})
    assert r.ok and r.meta.get("note_id")
    assert store.get(r.meta["note_id"]).title == "Call dentist"


def test_tool_add_checklist(store):
    r = tools._add_note({"title": "todo", "items": ["a", "b"]})
    assert r.ok
    n = store.get(r.meta["note_id"])
    assert n.note_type == nt.CHECKLIST and len(n.items) == 2


def test_tool_add_note_requires_content(store):
    r = tools._add_note({})
    assert not r.ok and r.error_code is ErrorCode.INVALID_ARGS


def test_tool_list_notes(store):
    store.add(title="one")
    store.add(title="two", label="x")
    r = tools._list_notes({})
    assert r.ok and "note(s)" in r.output and r.meta.get("count") == 2
    r2 = tools._list_notes({"label": "x"})
    assert r2.meta.get("count") == 1


def test_tool_update_note(store):
    n = store.add(title="old")
    r = tools._update_note({"id": n.id, "title": "new"})
    assert r.ok and store.get(n.id).title == "new"


def test_tool_update_missing(store):
    r = tools._update_note({"id": "nope", "title": "x"})
    assert not r.ok and r.error_code is ErrorCode.FILE_NOT_FOUND


def test_tool_update_needs_a_field(store):
    n = store.add(title="x")
    r = tools._update_note({"id": n.id})
    assert not r.ok and r.error_code is ErrorCode.INVALID_ARGS


def test_tool_complete_item_by_index(store):
    n = store.add(title="c", items=["a", "b"])
    r = tools._complete_note_item({"id": n.id, "index": 0})
    assert r.ok and store.get(n.id).items[0]["done"] is True


def test_tool_complete_item_by_text(store):
    n = store.add(title="c", items=["water plants", "feed cat"])
    r = tools._complete_note_item({"id": n.id, "text": "cat"})
    assert r.ok and store.get(n.id).items[1]["done"] is True


def test_tool_complete_item_bad_index(store):
    n = store.add(title="c", items=["a"])
    r = tools._complete_note_item({"id": n.id, "index": 9})
    assert not r.ok and r.error_code is ErrorCode.INVALID_ARGS


def test_tool_delete_note(store):
    n = store.add(title="bye")
    assert tools._delete_note({"id": n.id}).ok
    assert not tools._delete_note({"id": n.id}).ok
