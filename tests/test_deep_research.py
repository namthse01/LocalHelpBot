"""v5 — Deep Research: pure helpers + offline loop with fakes."""
from __future__ import annotations

import core.deep_research as dr
from core.tool_schema import ToolResult


# ── Pure helpers ──────────────────────────────────────────────────────

def test_markdown_to_html_headings_and_lists():
    html = dr._markdown_to_html("# Title\n\nsome **bold** text\n\n- one\n- two")
    assert "<h1>Title</h1>" in html
    assert "<strong>bold</strong>" in html
    assert "<li>one</li>" in html and "<li>two</li>" in html


def test_markdown_to_html_links_and_citations():
    html = dr._markdown_to_html("See https://example.com for more [1]")
    assert '<a href="https://example.com"' in html
    assert "<sup>[1]</sup>" in html


def test_parse_json_array_tolerant():
    assert dr._parse_json_array('["a", "b", "c"]') == ["a", "b", "c"]
    assert dr._parse_json_array('```json\n["x"]\n```') == ["x"]
    # Fallback to lines.
    assert dr._parse_json_array("- first\n- second") == ["first", "second"]


def test_render_html_has_metadata_badges():
    html = dr._render_html("My question", "# Report\n\nbody", ["http://a", "http://b"], 2)
    assert "Deep Research" in html
    assert "2 round(s)" in html
    assert "2 source(s)" in html


# ── Offline loop ──────────────────────────────────────────────────────

class _FakeProvider:
    """Returns canned content based on which prompt phase is calling."""

    def chat(self, messages, options=None):
        system = messages[0]["content"].lower()
        user = messages[-1]["content"].lower()

        class R:
            pass
        r = R()
        if "planner" in system:
            r.content = '{"sub_questions": ["q1", "q2"], "key_topics": ["t"], "success_criteria": "done"}'
        elif "search queries" in system:
            r.content = '["python news 2026"]'
        elif "synthesizer" in system:
            r.content = "Evolving report with findings [1]."
        elif "completeness" in system:
            r.content = "NO — still gaps."
        elif "technical writer" in system:
            r.content = "# Final Report\n\nComprehensive answer [1].\n"
        else:
            r.content = "ok"
        return r


def test_run_deep_research_offline(tmp_path, monkeypatch):
    import core.tools as tools

    def fake_search(q):
        return ToolResult.success("1. Result\n   URL: https://example.com/a\n   snippet")

    def fake_fetch(u):
        return ToolResult.success("Some page text about the topic.")

    monkeypatch.setattr(tools, "search_web", fake_search)
    monkeypatch.setattr(tools, "fetch_url", fake_fetch)
    monkeypatch.setattr(dr, "RESEARCH_DIR", tmp_path / "research")

    result = dr.run_deep_research(
        "What is new in python 2026?",
        max_rounds=1, queries_per_round=1, pages_per_query=1,
        provider=_FakeProvider(),
    )
    assert "Final Report" in result["report_md"]
    assert result["sources"] == ["https://example.com/a"]
    assert result["rounds"] == 1
    assert result["html_path"] and (tmp_path / "research").exists()


def test_search_extracts_urls(monkeypatch):
    import core.tools as tools
    monkeypatch.setattr(
        tools, "search_web",
        lambda q: ToolResult.success("1. A\n   URL: https://x.com/1\n2. B\n   URL: https://y.com/2"),
    )
    assert dr._search("q") == ["https://x.com/1", "https://y.com/2"]
