"""Server bind hardening + environment guard for the proxy.

Extracted from :mod:`core.proxy` (behavior-preserving move — no logic
changes). ``ExclusiveThreadingHTTPServer`` refuses to share its port (the
Task-1 co-bind fix); ``_assert_healthy_environment`` fails fast when launched
under an interpreter that can't serve the orchestrator. Re-exported from
``core.proxy`` (``main()`` references both as module globals).

Both compute the project root from ``Path(__file__).resolve().parent.parent``,
which is identical whether this file lives at ``core/proxy.py`` or
``core/proxy_runtime.py`` since both sit in ``core/`` — so the move is safe.
"""

from __future__ import annotations

import os
import socket
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path


class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that refuses to share its port.

    Plain HTTPServer sets ``allow_reuse_address = True`` (SO_REUSEADDR). On
    Windows that lets a *second* process CO-BIND the same port; the OS then
    splits incoming connections across both listeners. If the second proxy has
    a broken environment (e.g. system Python with missing deps → no working
    orchestrator), virtual models like ``auto-agent`` fall through to Ollama and
    return an intermittent "model not found" 404. Forcing exclusive ownership
    makes any second bind fail hard with "address already in use".
    """

    allow_reuse_address = False

    def server_bind(self):
        # Windows: SO_EXCLUSIVEADDRUSE is the strong guarantee — it blocks any
        # other socket from binding the same (addr, port) while we hold it,
        # which plain ``allow_reuse_address = False`` does not fully provide.
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        super().server_bind()


def _assert_healthy_environment() -> None:
    """Refuse to limp along under an interpreter that can't serve the agent.

    Past root cause of intermittent chat 404s: the proxy was launched under
    SYSTEM Python (missing project deps) instead of the venv. Its orchestrator
    couldn't init, so virtual models fell through to Ollama and 404'd. Worse,
    that broken proxy co-bound port 11435 next to a healthy one, making the
    failure flaky. The exclusive bind stops the co-bind; this guard stops the
    broken interpreter from running at all — fail fast with a clear message.

    Only invoked from ``main()`` (never on import) so the test suite, which
    imports ``core.proxy``, is unaffected.
    """
    # Packaged (PyInstaller) builds bundle their own deps and have no venv.
    if getattr(sys, "frozen", False):
        return

    import importlib.util

    project_root = Path(__file__).resolve().parent.parent
    nt = os.name == "nt"
    venv_py = project_root / "venv" / ("Scripts/python.exe" if nt else "bin/python")

    # Heuristic: are we running from a venv that lives under the project root?
    try:
        prefix = Path(sys.prefix).resolve()
        in_project_venv = prefix == project_root or project_root in prefix.parents
    except Exception:
        in_project_venv = False

    # Key third-party deps the request/orchestrator path needs. These are
    # import names, not pip names. find_spec() locates without importing.
    key_deps = ["requests", "pydantic", "tiktoken", "chromadb", "cryptography"]
    missing = [d for d in key_deps if importlib.util.find_spec(d) is None]

    if not in_project_venv:
        print(
            f"\n[proxy] WARNING: running OUTSIDE the project venv.\n"
            f"        interpreter : {sys.executable}\n"
            f"        sys.prefix  : {sys.prefix}\n"
            f"        expected    : {venv_py}\n"
            f"        Launch with the venv to avoid missing-dependency 404s:\n"
            f"          Windows:  .\\start_theagent0.bat\n"
            f"          *nix:     ./start_theagent0.sh\n",
            flush=True,
        )

    if missing:
        print(
            f"\n[proxy] FATAL: required dependencies are missing: "
            f"{', '.join(missing)}.\n"
            f"        This interpreter ({sys.executable}) cannot serve the agent\n"
            f"        orchestrator — virtual models like 'auto-agent' would 404.\n"
            f"        You are almost certainly running the wrong Python (system\n"
            f"        Python instead of the venv). Relaunch with the venv:\n"
            f"          {venv_py} -m core.proxy\n"
            f"        or simply:  .\\start_theagent0.bat\n",
            flush=True,
        )
        raise SystemExit(3)
