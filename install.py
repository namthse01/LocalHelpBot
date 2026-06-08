#!/usr/bin/env python3
"""install.py — one-command, cross-platform setup for TheAgent0.

Goal: a fresh clone on Windows / Linux / macOS becomes a working
install with a single command, auto-downloading exactly the model
weights that fit the machine's RAM / VRAM (see ``core/hardware.py``).

    python install.py            # interactive, picks models for this box
    python install.py --yes      # non-interactive (CI / unattended)
    python install.py --skip-models --skip-rag   # deps + venv only

What it does, in order:
  1. Probe hardware and pick a model tier.
  2. Create a virtualenv (``venv/``) and install ``requirements.txt``.
  3. Ensure Ollama is installed and the server is running.
  4. ``ollama pull`` the recommended chat / embed / vision (+large) models.
  5. Write the chosen models into ``runtime_overrides.json`` (no source edits).
  6. Activate the git pre-commit hook.
  7. Build the RAG index (``data/indexer.py``).

Every step is best-effort and prints a clear next-action on failure, so a
partial run never leaves the user guessing. Re-running is safe (idempotent).

This script runs on the *system* Python and must stay stdlib-only — the
project deps don't exist yet when it starts.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core import hardware  # noqa: E402  (stdlib-only module, safe pre-install)

IS_WINDOWS = platform.system() == "Windows"
OLLAMA_BASE = "http://localhost:11434"
MIN_PY = (3, 10)


# ── tiny console helpers (ASCII-only — safe on every codepage) ────
def _supports_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


_C = _supports_color()
def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _C else s


def step(n: int, total: int, msg: str) -> None:
    print(_c("1;36", f"\n[{n}/{total}] {msg}"))


def ok(msg: str) -> None:
    print(_c("32", "  OK   ") + msg)


def warn(msg: str) -> None:
    print(_c("33", "  WARN ") + msg)


def fail(msg: str) -> None:
    print(_c("31", "  FAIL ") + msg)


def info(msg: str) -> None:
    print("       " + msg)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """Run a command, streaming its output to this console."""
    info(_c("90", "$ " + " ".join(cmd)))
    return subprocess.run(cmd, **kw)


def ask_yes(prompt: str, assume_yes: bool, default: bool = True) -> bool:
    if assume_yes:
        return True
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        ans = input(prompt + suffix).strip().lower()
    except EOFError:
        return default
    if not ans:
        return default
    return ans in ("y", "yes")


# ── step 1: hardware ─────────────────────────────────────────────
def probe_hardware() -> hardware.Recommendation:
    print(hardware.summary())
    return hardware.recommend()


# ── step 2: venv + deps ──────────────────────────────────────────
def venv_python() -> Path:
    if IS_WINDOWS:
        return ROOT / "venv" / "Scripts" / "python.exe"
    return ROOT / "venv" / "bin" / "python"


def ensure_venv() -> Path:
    py = venv_python()
    if py.exists():
        ok(f"venv already exists -> {py}")
    else:
        info("Creating virtualenv at ./venv ...")
        cp = run([sys.executable, "-m", "venv", "venv"])
        if cp.returncode != 0 or not py.exists():
            fail("Could not create venv. Is the 'venv' module available?")
            sys.exit(1)
        ok("venv created")
    return py


def install_requirements(py: Path) -> None:
    info("Upgrading pip ...")
    run([str(py), "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
    info("Installing requirements.txt (this can take a few minutes) ...")
    cp = run([str(py), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")])
    if cp.returncode != 0:
        fail("pip install failed. Scroll up for the error; fix and re-run.")
        sys.exit(1)
    ok("Python dependencies installed")


# ── step 3: Ollama ───────────────────────────────────────────────
def ollama_server_up() -> bool:
    try:
        with urllib.request.urlopen(OLLAMA_BASE + "/api/tags", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def ollama_installed() -> bool:
    return shutil.which("ollama") is not None


def try_install_ollama(assume_yes: bool) -> bool:
    """Attempt an OS-appropriate auto-install of Ollama. Returns success."""
    system = platform.system()
    if system == "Linux":
        if not ask_yes("Install Ollama via the official script?", assume_yes):
            return False
        info("Running: curl -fsSL https://ollama.com/install.sh | sh")
        cp = subprocess.run(
            "curl -fsSL https://ollama.com/install.sh | sh", shell=True
        )
        return cp.returncode == 0 and ollama_installed()

    if system == "Darwin":
        if shutil.which("brew") and ask_yes(
            "Install Ollama via Homebrew?", assume_yes
        ):
            cp = run(["brew", "install", "ollama"])
            return cp.returncode == 0 and ollama_installed()
        return False

    if system == "Windows":
        if shutil.which("winget") and ask_yes(
            "Install Ollama via winget?", assume_yes
        ):
            cp = run(["winget", "install", "--id", "Ollama.Ollama",
                      "-e", "--accept-source-agreements",
                      "--accept-package-agreements"])
            return cp.returncode == 0 and ollama_installed()
        return False

    return False


def ensure_ollama(assume_yes: bool) -> bool:
    """Make sure Ollama is installed and the server is reachable."""
    if ollama_server_up():
        ok("Ollama server is running")
        return True

    if not ollama_installed():
        warn("Ollama is not installed.")
        if not try_install_ollama(assume_yes):
            fail("Ollama is required to run local models.")
            info("Install it from https://ollama.com/download, then re-run "
                 "this installer (it will skip everything already done).")
            return False
        ok("Ollama installed")

    # Installed but not serving — start it in the background.
    info("Starting 'ollama serve' in the background ...")
    try:
        kwargs: dict = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = 0x00000008  # DETACHED_PROCESS
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs,
        )
    except Exception as e:
        warn(f"Could not auto-start the server ({e}).")

    for _ in range(15):
        if ollama_server_up():
            ok("Ollama server is running")
            return True
        time.sleep(1)

    warn("Ollama is installed but the server did not come up in time.")
    info("Open a terminal, run 'ollama serve', then re-run this installer.")
    return False


def pull_models(rec: hardware.Recommendation, assume_yes: bool) -> None:
    models = rec.pull_list()
    print()
    info("Models selected for this machine:")
    for m in models:
        info("  - " + m)
    if not ask_yes("Download these now?", assume_yes):
        warn("Skipping model download. Pull them later with 'ollama pull <name>'.")
        return

    # What's already local? Skip those.
    have: set[str] = set()
    try:
        with urllib.request.urlopen(OLLAMA_BASE + "/api/tags", timeout=3) as r:
            tags = json.load(r)
        have = {m.get("name", "") for m in tags.get("models", [])}
    except Exception:
        pass

    for m in models:
        if m in have:
            ok(f"{m} already present")
            continue
        info(f"Pulling {m} ...")
        cp = subprocess.run(["ollama", "pull", m])
        if cp.returncode == 0:
            ok(f"{m} ready")
        else:
            warn(f"Failed to pull {m}. You can retry with 'ollama pull {m}'.")


# ── step 5: runtime_overrides.json ───────────────────────────────
def write_overrides(rec: hardware.Recommendation) -> None:
    path = ROOT / "runtime_overrides.json"
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            warn("Existing runtime_overrides.json is unreadable; rewriting it.")
            data = {}

    providers = data.get("providers") or {}
    primary = providers.get("primary") or {}
    fallback = providers.get("fallback") or {}

    # Point the local primary at the hardware-matched chat model. Preserve
    # any encrypted api_key / provider choice the user set in the UI before.
    if primary.get("type", "local") == "local" or not primary:
        primary.update({"type": "local", "provider": "ollama",
                        "model": rec.chat_model})
    fallback.update({"type": "local", "provider": "ollama",
                     "model": rec.chat_model})

    providers["primary"] = primary
    providers["fallback"] = fallback
    data["providers"] = providers

    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    ok(f"runtime_overrides.json points the local model at {rec.chat_model}")
    info("(Embedding/vision models are read from config.py defaults; the "
         "indexer + vision tools will use what was just pulled.)")


# ── step 6: git hooks ────────────────────────────────────────────
def setup_git_hooks() -> None:
    if not (ROOT / ".git").exists():
        info("Not a git checkout; skipping hook setup.")
        return
    cp = run(["git", "config", "core.hooksPath", ".githooks"])
    if cp.returncode == 0:
        ok("git pre-commit hook activated (blocks accidental key/log commits)")
    else:
        warn("Could not set core.hooksPath; run 'git config core.hooksPath "
             ".githooks' manually.")


# ── step 7: RAG index ────────────────────────────────────────────
def build_rag(py: Path, assume_yes: bool) -> None:
    if not ask_yes("Build the RAG index now (embeds docs/)?", assume_yes):
        warn("Skipping RAG build. Run 'python data/indexer.py' later.")
        return
    info("Indexing docs/ -> vector store (uses the embed model) ...")
    cp = run([str(py), "-m", "data.indexer"], cwd=str(ROOT))
    if cp.returncode == 0:
        ok("RAG index built")
    else:
        warn("RAG build failed (often: embed model not pulled yet). "
             "Re-run 'python data/indexer.py' after models finish downloading.")


# ── orchestration ────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="One-command setup for TheAgent0.")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="Assume yes to all prompts (unattended).")
    ap.add_argument("--skip-ollama", action="store_true",
                    help="Don't touch Ollama (no install / pull).")
    ap.add_argument("--skip-models", action="store_true",
                    help="Set up everything but don't download models.")
    ap.add_argument("--skip-rag", action="store_true",
                    help="Don't build the RAG index.")
    args = ap.parse_args()

    if sys.version_info < MIN_PY:
        fail(f"Python {MIN_PY[0]}.{MIN_PY[1]}+ required; you have "
             f"{platform.python_version()}.")
        return 1

    print(_c("1;35", "=" * 56))
    print(_c("1;35", "  TheAgent0 installer — auto-configure for this machine"))
    print(_c("1;35", "=" * 56))

    total = 7
    step(1, total, "Probing hardware")
    rec = probe_hardware()

    step(2, total, "Python virtualenv + dependencies")
    py = ensure_venv()
    install_requirements(py)

    ollama_ready = False
    if args.skip_ollama:
        step(3, total, "Ollama (skipped via --skip-ollama)")
        warn("Skipping Ollama setup.")
    else:
        step(3, total, "Ollama runtime")
        ollama_ready = ensure_ollama(args.yes)

    step(4, total, "Model weights")
    if args.skip_models or args.skip_ollama:
        warn("Skipping model download.")
    elif ollama_ready:
        pull_models(rec, args.yes)
    else:
        warn("Ollama not ready — skipping model download.")

    step(5, total, "Runtime config")
    write_overrides(rec)

    step(6, total, "Git hooks")
    setup_git_hooks()

    step(7, total, "RAG index")
    if args.skip_rag or args.skip_models or args.skip_ollama or not ollama_ready:
        warn("Skipping RAG build (deps/models not all ready).")
    else:
        build_rag(py, args.yes)

    # ── done ──
    print(_c("1;32", "\n" + "=" * 56))
    print(_c("1;32", "  Setup complete."))
    print(_c("1;32", "=" * 56))
    launcher = "start_theagent0.bat" if IS_WINDOWS else "./start_theagent0.sh"
    info("Launch the app with:")
    print(_c("1;37", f"    {launcher}"))
    info("Then open http://localhost:11435 in your browser.")
    info("Health check any time: "
         + f"{venv_python()} -m core.healthcheck")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(130)
