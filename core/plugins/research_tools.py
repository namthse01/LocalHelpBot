"""Deep Research tool — multi-round web research → visual report.

Wraps `core.deep_research.run_deep_research` as an agent tool. The agent calls
this for questions that need synthesizing several web sources into one
comprehensive, cited answer (comparisons, "state of X in 2026", literature
scans). It runs an iterative plan→search→read→synthesize loop and writes a
self-contained HTML report under data/research/.

This is a heavyweight tool (many internal web calls + several LLM turns) — the
family budget counts it as a single 'delegate'-class call. Prefer it over
hand-rolling 5 search_web/fetch_url calls.
"""
from __future__ import annotations

from typing import Any, Dict

from core.tool_schema import ErrorCode, Tool, ToolRegistry, ToolResult


def _deep_research(args: Dict[str, Any]) -> ToolResult:
    question = (args.get("question") or args.get("query") or args.get("topic") or "").strip()
    if not question:
        return ToolResult.error(ErrorCode.INVALID_ARGS, "deep_research requires 'question'.", retryable=False)
    try:
        max_rounds = max(1, min(int(args.get("max_rounds") or 3), 6))
    except (TypeError, ValueError):
        max_rounds = 3
    try:
        from core.deep_research import run_deep_research
        result = run_deep_research(question, max_rounds=max_rounds)
    except Exception as e:  # noqa: BLE001
        return ToolResult.error(
            ErrorCode.UNKNOWN, f"deep_research failed: {e}",
            hint="Check network access / that search_web works.",
        )

    if not result.get("sources"):
        return ToolResult.error(
            ErrorCode.NO_RESULTS,
            "deep_research found no usable web sources for this question.",
            hint="Rephrase the question or answer from base knowledge.",
        )

    md = result["report_md"]
    html_path = result.get("html_path") or ""
    footer = ""
    if html_path:
        footer = f"\n\n---\n_Visual HTML report saved to_ `{html_path}` ({result['rounds']} rounds, {len(result['sources'])} sources)."
    # Cap the inline body so the agent's context stays manageable; the full
    # report lives on disk.
    body = md if len(md) <= 6000 else md[:6000] + "\n\n…[report truncated — full version saved to disk]"
    return ToolResult.success(
        body + footer,
        html_path=html_path,
        md_path=result.get("md_path", ""),
        rounds=result["rounds"],
        sources=len(result["sources"]),
    )


def register(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="deep_research",
        description=(
            "Run multi-round web research on a question and return a "
            "comprehensive, cited report (also saved as a visual HTML file). "
            "Use for questions that need synthesizing SEVERAL sources — "
            "comparisons, 'state of X', surveys, due diligence. It plans, "
            "searches, reads pages, and iterates. Prefer this over many manual "
            "search_web/fetch_url calls. Slow (multiple rounds) — use sparingly."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The research question."},
                "max_rounds": {"type": "integer", "description": "1-6 search rounds (default 3)."},
            },
            "required": ["question"],
        },
        handler=_deep_research,
        category="web",
    ))
