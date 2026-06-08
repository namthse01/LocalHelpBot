"""Arithmetic fast-path + numeric/locale parsing for the agent loop.

Extracted from :mod:`core.agent` (behavior-preserving move — no logic
changes). Detects pure operator-leading follow-ups (e.g. "+ 4") against a
numeric anchor from the previous turn and evaluates them deterministically —
no LLM call. Also holds the last-user-message extractor and locale-aware
number parsing. Re-exported from ``core.agent``.
"""

from __future__ import annotations

import operator as _op
import re
from typing import Any, Dict, List, Optional


# Matches "+ 4", "- 2.5", "* 10", "/3" — operator followed by a number,
# possibly with whitespace, nothing else on the line.
_FASTPATH_RE = re.compile(r"^\s*([+\-*/])\s*(-?\d+(?:\.\d+)?)\s*$")

# Matches any number in a string. Vietnamese / European thousand separators
# use "." and decimal commas; English uses commas as thousand separators.
# We accept both by stripping commas before float() and treating "," as the
# decimal point only when it's the LAST grouping in the token.
_NUMBER_RE = re.compile(r"-?\d{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?|-?\d+(?:[.,]\d+)?")

_FASTPATH_OPS = {
    "+": _op.add,
    "-": _op.sub,
    "*": _op.mul,
    "/": _op.truediv,
}


def _parse_number_token(tok: str) -> Optional[float]:
    """Parse a number that may use comma or dot as decimal/thousand separator."""
    if not tok:
        return None
    # Strategy: if both `,` and `.` appear, treat the LAST one as the decimal
    # and the others as grouping. If only one appears with 3 digits after it,
    # it's a thousand separator; otherwise it's the decimal point.
    last_dot = tok.rfind(".")
    last_com = tok.rfind(",")
    if last_dot >= 0 and last_com >= 0:
        if last_dot > last_com:
            cleaned = tok.replace(",", "")
        else:
            cleaned = tok.replace(".", "").replace(",", ".")
    elif last_com >= 0:
        # only commas
        tail = tok[last_com + 1:]
        if len(tail) == 3 and tail.isdigit():
            cleaned = tok.replace(",", "")  # thousand grouping
        else:
            cleaned = tok.replace(",", ".")  # decimal
    elif last_dot >= 0:
        # only dots — assume decimal point
        cleaned = tok
    else:
        cleaned = tok
    try:
        return float(cleaned)
    except ValueError:
        return None


def _extract_last_number(text: str) -> Optional[float]:
    """Return the LAST number that appears in `text`, or None if none found.

    Used to capture the numeric answer from an assistant turn like
    "1+1 = 2" → 2.0 or "the total is 1,234.5" → 1234.5.
    """
    if not text:
        return None
    matches = _NUMBER_RE.findall(text)
    for tok in reversed(matches):
        val = _parse_number_token(tok)
        if val is not None:
            return val
    return None


def _last_user_message(messages: List[Dict[str, Any]]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content", "")
            if isinstance(c, list):
                return " ".join(
                    p.get("text", "") for p in c
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            return str(c or "")
    return ""


def _try_arithmetic_fastpath(user_msg: str, prev_numeric: Optional[float]) -> Optional[str]:
    """If `user_msg` is a pure operator-leading expression (e.g. "+ 4") and
    `prev_numeric` is known, evaluate and return a formatted answer.
    Returns None when the message doesn't qualify or no anchor exists."""
    if prev_numeric is None or not user_msg:
        return None
    m = _FASTPATH_RE.match(user_msg)
    if not m:
        return None
    op_sym, operand_tok = m.group(1), m.group(2)
    try:
        operand = float(operand_tok)
    except ValueError:
        return None
    fn = _FASTPATH_OPS.get(op_sym)
    if fn is None:
        return None
    if op_sym == "/" and operand == 0.0:
        return f"{_fmt_num(prev_numeric)} / 0 = undefined (cannot divide by zero)"
    try:
        result = fn(prev_numeric, operand)
    except (ZeroDivisionError, OverflowError, ValueError) as e:
        return f"{_fmt_num(prev_numeric)} {op_sym} {operand_tok} = error ({e})"
    return f"{_fmt_num(prev_numeric)} {op_sym} {_fmt_num(operand)} = {_fmt_num(result)}"


def _fmt_num(x: float) -> str:
    """Render a float without trailing ".0" when it's whole."""
    if x == int(x) and abs(x) < 1e16:
        return str(int(x))
    return f"{x:g}"
