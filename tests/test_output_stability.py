"""Guards for output-stability fixes on the abliterated 14B model.

A user asked "tiger vs lion, which wins?" and the model answered in
Vietnamese, then mid-word switched to Chinese and emitted a self-addressed
meta-prompt ("请帮忙修改一下这句话…" = "please help revise this sentence").
That is generation degeneration — language drift + a leaked self-prompt.

Two mitigations, both locked in here so they can't be silently removed:
  1. SAMPLING_DEFAULT carries a `repeat_penalty` (loop/drift guard) and keeps
     the low-temperature, tool-use-friendly settings.
  2. MAIN_SYSTEM_PROMPT tells the model to stay in ONE language and emit only
     its answer to the user (no self-instructions, no both-sides role-play).
"""
from __future__ import annotations

from core.agent_prompts import MAIN_SYSTEM_PROMPT
from core.providers import SAMPLING_DEFAULT


def test_sampling_default_has_repeat_penalty():
    assert SAMPLING_DEFAULT.get("repeat_penalty") == 1.1
    # The other tool-use-friendly knobs must remain intact.
    assert SAMPLING_DEFAULT.get("temperature") == 0.3
    assert SAMPLING_DEFAULT.get("top_p") == 0.9
    assert SAMPLING_DEFAULT.get("num_predict") == 2048


def test_main_prompt_has_language_integrity_rule():
    p = MAIN_SYSTEM_PROMPT
    assert "OUTPUT LANGUAGE & INTEGRITY" in p
    # Same-language rule.
    assert "SAME language" in p
    assert "NEVER switch languages" in p
    # No self-addressed meta-prompt / both-sides role-play.
    assert "addressed to" in p
    assert "BOTH" in p and "sides of a conversation" in p
