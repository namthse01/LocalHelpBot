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


def _ollama_chat(messages: list) -> str:
    body = json.dumps({
        "model":    REAL_MODEL,
        "messages": messages,
        "stream":   False,
        "options":  {"temperature": 0.2, "num_predict": 2048},
    }).encode()

    req = urllib.request.Request(
        OLLAMA_BASE + "/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    return data["message"]["content"]


def run_agent(messages: list, tools: dict, stream_cb=None) -> str:
    """
    Agentic loop.
    tools: dict of tool_name -> callable(args_dict) -> str
    stream_cb: optional callable(text) for streaming intermediate steps
    Returns final answer string.
    """
    conversation = list(messages)
    has_web = "search_web" in tools
    error_streak = 0          # consecutive error turns
    MAX_ERROR_STREAK = 4      # give up recovery after this many consecutive errors

    for turn in range(MAX_TURNS):
        content = _ollama_chat(conversation)

        # Look for ACTION:
        match = ACTION_RE.search(content)
        if not match:
            # No action → this is the final answer
            return content

        # Show reasoning before the action
        before_action = content[:match.start()].strip()
        if before_action and stream_cb:
            stream_cb(before_action + "\n\n")

        # Parse ACTION JSON
        try:
            args = json.loads(match.group(1))
            tool_name = args.pop("tool")
        except (json.JSONDecodeError, KeyError) as e:
            tool_result = f"ERROR: Could not parse ACTION JSON: {e}\nRaw: {match.group(1)}"
            tool_name   = "unknown"
            args        = {}

        # Execute tool
        if tool_name not in tools:
            tool_result = (
                f"ERROR: Unknown tool '{tool_name}'. "
                f"Available tools: {', '.join(sorted(tools.keys()))}"
            )
        else:
            if stream_cb:
                stream_cb(f"[{tool_name}] {json.dumps(args, ensure_ascii=False)}\n")
            try:
                tool_result = tools[tool_name](args)
            except Exception as e:
                tool_result = f"ERROR executing {tool_name}: {e}"

        # Stream result preview
        if stream_cb:
            preview = tool_result[:400] + ("..." if len(tool_result) > 400 else "")
            stream_cb(f"Result:\n{preview}\n\n")

        # Build feedback — error-aware
        is_err = _is_error(tool_result)
        if is_err:
            error_streak += 1
            if error_streak >= MAX_ERROR_STREAK:
                # Force the model to stop trying and summarize what happened
                feedback_msg = (
                    f"Tool `{tool_name}` result:\n```\n{tool_result}\n```\n\n"
                    f"You have encountered {error_streak} consecutive errors. "
                    "Stop attempting recovery and give a final answer explaining what went wrong "
                    "and what you tried. Do NOT use any more ACTION lines."
                )
            else:
                feedback_msg = (
                    f"Tool `{tool_name}` result:\n```\n{tool_result}\n```\n\n"
                    + _recovery_hint(tool_name, tool_result, has_web)
                )
        else:
            error_streak = 0   # reset on success
            feedback_msg = (
                f"Tool result for `{tool_name}`:\n"
                f"```\n{tool_result}\n```\n\n"
                "Continue. Use another ACTION if you have more steps, "
                "or give your final answer (no ACTION lines) when done."
            )

        # Feed result back into conversation
        conversation.append({"role": "assistant", "content": content})
        conversation.append({"role": "user", "content": feedback_msg})

    return "ERROR: Max turns reached without a final answer."
