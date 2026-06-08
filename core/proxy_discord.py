"""Discord gateway subprocess management.

Extracted from :mod:`core.proxy` (behavior-preserving move — no logic
changes). Starts/stops/queries the ``core/discord_gateway.py`` child process.

The ``_discord_proc`` module global is fully encapsulated by these three
functions — callers only ever invoke the functions, never touch the global —
so moving the whole cluster (global + its mutators) keeps the shared-state
contract intact. ``core.proxy`` re-imports the three functions but NOT
``_discord_proc`` itself: re-importing a rebindable global would snapshot a
stale ``None`` that diverges once ``_start_discord`` reassigns it here.

``bot_root`` resolves from ``Path(__file__).parent.parent`` — the project
root — which is identical whether this file lives at ``core/proxy.py`` or
``core/proxy_discord.py`` since both sit in ``core/``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# ── Discord subprocess management ────────────────────────────
_discord_proc = None


def _start_discord():
    global _discord_proc
    if _discord_proc and _discord_proc.poll() is None:
        return False, "Discord is already running"
    bot_root = Path(__file__).parent.parent
    python_exe = bot_root / "venv" / "Scripts" / "python.exe"
    gateway_script = bot_root / "core" / "discord_gateway.py"
    _discord_proc = subprocess.Popen(
        [str(python_exe), str(gateway_script)],
        cwd=str(bot_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return True, "Discord connected"


def _stop_discord():
    global _discord_proc
    if _discord_proc and _discord_proc.poll() is None:
        _discord_proc.terminate()
        _discord_proc.wait(timeout=5)
        _discord_proc = None
        return True, "Discord disconnected"
    _discord_proc = None
    return False, "Discord is not running"


def _discord_status():
    return _discord_proc is not None and _discord_proc.poll() is None
