"""v6 — proxy routes behind the Notes UI tab.

  • GET  /api/notes               → notes + labels (filters: ?archived, ?label)
  • POST /api/notes/create        → make a note/checklist
  • POST /api/notes/update        → patch fields
  • POST /api/notes/pin           → toggle pinned
  • POST /api/notes/archive       → toggle archived
  • POST /api/notes/item-toggle   → flip a checklist item's done flag
  • POST /api/notes/delete        → remove a note
  • POST /api/notes/reorder       → set manual order

Reuses the proxy-server harness from tests/test_image_inline.py: a fake project
root, ``core.proxy.__file__`` patched, an ephemeral ThreadingHTTPServer, and the
notes-store singleton repointed at the temp tree. No network, no Ollama.
"""
from __future__ import annotations

import json
import threading
from http.client import HTTPConnection

import pytest


@pytest.fixture
def proxy_server(tmp_path, monkeypatch):
    from core import proxy as proxy_mod
    from core import notes as nt

    project_root = tmp_path / "TheAgent0"
    (project_root / "core").mkdir(parents=True)
    (project_root / "TheAgent0UI").mkdir(parents=True)
    (project_root / "TheAgent0UI" / "index.html").write_text("<html>ok</html>", encoding="utf-8")

    # Isolate the notes store singleton to the temp tree.
    store = nt.reset_notes_store_for_tests(path=project_root / "data" / "notes" / "notes.jsonl")

    monkeypatch.setattr(proxy_mod, "__file__", str(project_root / "core" / "proxy.py"))

    server = proxy_mod.ThreadingHTTPServer(("127.0.0.1", 0), proxy_mod.ProxyHandler)
    host, port = server.server_address
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield host, port, store
    finally:
        server.shutdown()
        server.server_close()
        nt.reset_notes_store_for_tests()  # restore default singleton


def _get(host, port, path):
    conn = HTTPConnection(host, port, timeout=10)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, json.loads(body) if body else {}


def _post(host, port, path, obj):
    conn = HTTPConnection(host, port, timeout=10)
    conn.request("POST", path, body=json.dumps(obj).encode(),
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, json.loads(body) if body else {}


# ── GET /api/notes ───────────────────────────────────────────────────────
def test_notes_empty(proxy_server):
    host, port, _ = proxy_server
    status, body = _get(host, port, "/api/notes")
    assert status == 200
    assert body["notes"] == [] and body["labels"] == []


def test_create_and_list(proxy_server):
    host, port, _ = proxy_server
    status, body = _post(host, port, "/api/notes/create",
                         {"title": "hello", "content": "world", "label": "work"})
    assert status == 200 and body["ok"] is True
    nid = body["note"]["id"]
    assert nid

    status, body = _get(host, port, "/api/notes")
    assert status == 200
    assert [n["title"] for n in body["notes"]] == ["hello"]
    assert body["labels"] == ["work"]


def test_create_checklist(proxy_server):
    host, port, _ = proxy_server
    _, body = _post(host, port, "/api/notes/create",
                    {"title": "todo", "items": ["a", "b"], "note_type": "checklist"})
    note = body["note"]
    assert note["note_type"] == "checklist"
    assert [it["text"] for it in note["items"]] == ["a", "b"]


def test_update(proxy_server):
    host, port, _ = proxy_server
    _, body = _post(host, port, "/api/notes/create", {"title": "old"})
    nid = body["note"]["id"]
    status, body = _post(host, port, "/api/notes/update", {"id": nid, "title": "new"})
    assert status == 200 and body["note"]["title"] == "new"


def test_update_missing_is_404(proxy_server):
    host, port, _ = proxy_server
    status, body = _post(host, port, "/api/notes/update", {"id": "nope", "title": "x"})
    assert status == 404 and body["ok"] is False


def test_pin_toggle(proxy_server):
    host, port, _ = proxy_server
    _, body = _post(host, port, "/api/notes/create", {"title": "p"})
    nid = body["note"]["id"]
    _, body = _post(host, port, "/api/notes/pin", {"id": nid})
    assert body["note"]["pinned"] is True
    _, body = _post(host, port, "/api/notes/pin", {"id": nid})
    assert body["note"]["pinned"] is False


def test_archive_hides_from_default_list(proxy_server):
    host, port, _ = proxy_server
    _, body = _post(host, port, "/api/notes/create", {"title": "a"})
    nid = body["note"]["id"]
    _post(host, port, "/api/notes/archive", {"id": nid})
    _, body = _get(host, port, "/api/notes")
    assert body["notes"] == []
    _, body = _get(host, port, "/api/notes?archived=1")
    assert [n["title"] for n in body["notes"]] == ["a"]


def test_item_toggle(proxy_server):
    host, port, _ = proxy_server
    _, body = _post(host, port, "/api/notes/create", {"title": "c", "items": ["x", "y"]})
    nid = body["note"]["id"]
    _, body = _post(host, port, "/api/notes/item-toggle", {"id": nid, "index": 1})
    assert body["note"]["items"][1]["done"] is True


def test_item_toggle_bad_index_is_404(proxy_server):
    host, port, _ = proxy_server
    _, body = _post(host, port, "/api/notes/create", {"title": "c", "items": ["x"]})
    nid = body["note"]["id"]
    status, body = _post(host, port, "/api/notes/item-toggle", {"id": nid, "index": 9})
    assert status == 404 and body["ok"] is False


def test_delete(proxy_server):
    host, port, _ = proxy_server
    _, body = _post(host, port, "/api/notes/create", {"title": "bye"})
    nid = body["note"]["id"]
    status, body = _post(host, port, "/api/notes/delete", {"id": nid})
    assert status == 200 and body["ok"] is True
    status, body = _post(host, port, "/api/notes/delete", {"id": nid})
    assert status == 404 and body["ok"] is False


def test_label_filter(proxy_server):
    host, port, _ = proxy_server
    _post(host, port, "/api/notes/create", {"title": "w", "label": "work"})
    _post(host, port, "/api/notes/create", {"title": "h", "label": "home"})
    _, body = _get(host, port, "/api/notes?label=work")
    assert [n["title"] for n in body["notes"]] == ["w"]


def test_reorder(proxy_server):
    host, port, _ = proxy_server
    ids = []
    for t in ("a", "b", "c"):
        _, body = _post(host, port, "/api/notes/create", {"title": t})
        ids.append(body["note"]["id"])
    status, body = _post(host, port, "/api/notes/reorder",
                         {"ids": [ids[2], ids[0], ids[1]]})
    assert status == 200 and body["moved"] == 3
    _, body = _get(host, port, "/api/notes")
    assert [n["title"] for n in body["notes"]] == ["c", "a", "b"]


def test_unknown_action_is_404(proxy_server):
    host, port, _ = proxy_server
    status, body = _post(host, port, "/api/notes/frobnicate", {"id": "x"})
    assert status == 404 and body["ok"] is False
