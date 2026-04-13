"""
Agentic loop: sends messages to Ollama, parses ACTION: lines,
executes tools, feeds results back, repeats until final answer.

Improvements:
- Error detection in tool results → specific recovery hints
- Auto web-search suggestion when encountering unfamiliar errors
- Separate error-retry counter to track recovery attempts
"""

import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import OLLAMA_BASE, CHAT_MODEL

REAL_MODEL = CHAT_MODEL
MAX_TURNS  = 15   # increased from 10 to allow more recovery steps

ACTION_RE = re.compile(r"ACTION:\s*(\{.*?\})", re.DOTALL)

# Patterns that indicate a tool result is an error
_ERROR_SIGNALS = [
    "ERROR:",
    "Error:",
    "Traceback (most recent call last)",
    "SyntaxError",
    "NameError",
    "TypeError",
    "ValueError",
    "AttributeError",
    "ImportError",
    "ModuleNotFoundError",
    "FileNotFoundError",
    "PermissionError",
    "ConnectionError",
    "TimeoutError",
    "EXIT: 1",
    "EXIT: 2",
    "EXIT: 127",   # command not found
    "not found",
    "cannot find",
    "No such file",
    "failed with",
]


def _is_error(result: str) -> bool:
    """Return True if the tool result looks like an error."""
    first_500 = result[:500]
    return any(sig in first_500 for sig in _ERROR_SIGNALS)


def _recovery_hint(tool_name: str, result: str, has_web: bool) -> str:
    """Build an actionable recovery message after a tool error."""
    lines = [
        f"Tool `{tool_name}` returned an error. Do NOT give up — attempt recovery:",
        "",
        "Diagnose first:",
        "- Read the error message carefully.",
        "- If it's a wrong path/file, use list_dir or read_file to confirm the correct path.",
        "- If the command failed (non-zero EXIT), fix the command and retry.",
        "- If it's a missing dependency/module, run the install command first.",
        "- If it's a code bug you caused, read_file that file and fix it.",
    ]
    if has_web:
        lines += [
            "- If the error is unfamiliar or cryptic, use search_web with the exact error text.",
            "- After reading the search result, apply the fix.",
        ]
    lines += [
        "",
        "Give your next ACTION to recover. Only give a final answer when the task is actually solved.",
    ]
    return "\n".join(lines)


import json
import re
import sys
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional, Callable

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import OLLAMA_BASE, CHAT_MODEL
from core.providers import smart_provider

logger = logging.getLogger(__name__)

MAX_TURNS  = 20
ACTION_RE = re.compile(r"ACTION:\s*(\{.*?\})", re.DOTALL)

_ERROR_SIGNALS = [
    "ERROR:", "Error:", "Traceback", "SyntaxError", "NameError", "TypeError",
    "ValueError", "AttributeError", "ImportError", "ModuleNotFoundError",
    "FileNotFoundError", "PermissionError", "ConnectionError", "TimeoutError",
    "EXIT: 1", "EXIT: 2", "EXIT: 127", "not found", "cannot find", "No such file", "failed with",
]

def _is_error(result: str) -> bool:
    return any(sig in result[:500] for sig in _ERROR_SIGNALS)

def _recovery_hint(tool_name: str, result: str) -> str:
    return (
        f"Tool `{tool_name}` returned an error. To fix this, follow this loop:\n"
        "1. ANALYZE: Read the error. Is it a path issue? A missing dependency? A syntax bug?\n"
        "2. PLAN: State exactly what you will do to fix it (e.g., 'I will run pip install X').\n"
        "3. EXECUTE: Use an ACTION to apply the fix.\n"
        "4. VERIFY: Run the failing command again to confirm it works.\n\n"
        "Current Error:\n```\n{result}\n```\n"
        "Please provide your next ACTION."
    ).format(result=result)

def run_agent(messages: list, tools: dict, stream_cb=None) -> str:
    import time as _time
    conversation = list(messages)
    error_streak = 0
    MAX_ERROR_STREAK = 5
    t_start = _time.perf_counter()
    turn_timings = []

    for turn in range(MAX_TURNS):
        # Use the smart provider (API -> Local fallback)
        t_turn = _time.perf_counter()
        response = smart_provider.chat(conversation)
        turn_ms = int((_time.perf_counter() - t_turn) * 1000)
        turn_timings.append(turn_ms)
        logger.info(f"[agent] turn {turn+1} — {response.provider}/{response.model} {response.timing_str()}")
        content = response.content

        match = ACTION_RE.search(content)
        if not match:
            total_ms = int((_time.perf_counter() - t_start) * 1000)
            logger.info(f"[agent] DONE in {turn+1} turn(s), {total_ms}ms total (per-turn: {turn_timings})")
            return content

        before_action = content[:match.start()].strip()
        if before_action and stream_cb:
            stream_cb(before_action + "\n\n")

        try:
            args = json.loads(match.group(1))
            tool_name = args.pop("tool")
        except (json.JSONDecodeError, KeyError) as e:
            tool_result = f"ERROR: Could not parse ACTION JSON: {e}"
            tool_name, args = "unknown", {}

        if tool_name not in tools:
            tool_result = f"ERROR: Unknown tool '{tool_name}'. Available: {', '.join(sorted(tools.keys()))}"
        else:
            if stream_cb:
                stream_cb(f"[{tool_name}] {json.dumps(args, ensure_ascii=False)}\n")
            try:
                tool_result = tools[tool_name](args)
            except Exception as e:
                tool_result = f"ERROR executing {tool_name}: {e}"

        if stream_cb:
            stream_cb(f"Result:\n{tool_result[:400]}...\n\n")

        if _is_error(tool_result):
            error_streak += 1
            if error_streak >= MAX_ERROR_STREAK:
                feedback_msg = f"Too many consecutive errors. Please summarize the problem and stop.\n\nResult: {tool_result}"
            else:
                feedback_msg = _recovery_hint(tool_name, tool_result)
        else:
            error_streak = 0
            feedback_msg = f"Tool result for `{tool_name}`:\n```\n{tool_result}\n```\n\nContinue or provide final answer."

        conversation.append({"role": "assistant", "content": content})
        conversation.append({"role": "user", "content": feedback_msg})

    return "ERROR: Max turns reached."
