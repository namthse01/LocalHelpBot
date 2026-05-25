"""Startup + on-demand health validation.

Two entry points:

  CLI:  `python -m core.healthcheck`           — prints a rich table.
  HTTP: `GET /api/healthcheck` (proxy)         — JSON summary the UI shows.

What we validate, in order (least-expensive first so we fail fast):

  1. Ollama is reachable on `OLLAMA_BASE`.
  2. The 3 models we depend on exist locally (CHAT_MODEL, EMBED_MODEL,
     LARGE_MODEL if set).
  3. The embedding DB (`cad_db/`) is openable AND its vector dimension
     matches what `EMBED_MODEL` produces — most common mismatch is
     `mxbai-embed-large` (1024-d) vs an older `all-mpnet-base-v2`
     (768-d) DB.
  4. The sessions dir (`data/sessions/`) is writable.
  5. Proxy port (default 11435) — at most a warning if already taken;
     this check is informational because the proxy itself owns the port.

Each check returns a `CheckResult`. Overall status is `green` (all pass),
`yellow` (warnings only), or `red` (any FAIL).
"""

from __future__ import annotations

import json
import os
import socket
import sys
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).parent.parent


# ───────────────────────────────────────────────────────────────────────
# Result type
# ───────────────────────────────────────────────────────────────────────


@dataclass
class CheckResult:
    name: str
    status: str   # "pass" | "warn" | "fail"
    message: str
    detail: Optional[str] = None

    def is_fail(self) -> bool:
        return self.status == "fail"


# ───────────────────────────────────────────────────────────────────────
# Individual checks
# ───────────────────────────────────────────────────────────────────────


def _ollama_models(base: str, timeout: float = 4.0) -> Optional[List[str]]:
    try:
        with urllib.request.urlopen(base + "/api/tags", timeout=timeout) as r:
            data = json.loads(r.read())
        return [m.get("name") or m.get("model") for m in data.get("models", [])]
    except Exception:
        return None


def check_ollama(base: str) -> CheckResult:
    models = _ollama_models(base)
    if models is None:
        return CheckResult(
            name="Ollama reachable",
            status="fail",
            message=f"Could not reach Ollama at {base}",
            detail="Run `ollama serve` (or check it's installed: https://ollama.com/).",
        )
    return CheckResult(
        name="Ollama reachable",
        status="pass",
        message=f"OK — {len(models)} models installed",
    )


def check_models_present(base: str, wanted: List[str]) -> List[CheckResult]:
    """Check each model in `wanted` is locally installed.

    Empty / falsy entries are skipped (LARGE_MODEL may be intentionally
    blank when the user has no large model).
    """
    available = _ollama_models(base) or []
    results: List[CheckResult] = []
    avail_norm = [m.lower() for m in available]
    for w in wanted:
        if not w:
            continue
        wnorm = w.lower()
        # Ollama lists models with `:latest` suffix when tagged; tolerate either form.
        hit = wnorm in avail_norm or (wnorm + ":latest") in avail_norm or wnorm.split(":")[0] in [a.split(":")[0] for a in avail_norm]
        if hit:
            results.append(CheckResult(name=f"Model `{w}`", status="pass", message="installed"))
        else:
            results.append(CheckResult(
                name=f"Model `{w}`",
                status="fail",
                message="not installed locally",
                detail=f"Run: `ollama pull {w.split(':')[0]}`",
            ))
    return results


_EMBED_DIM_HINTS = {
    "mxbai-embed-large": 1024,
    "nomic-embed-text":  768,
    "all-minilm":        384,
    "all-mpnet-base-v2": 768,
}


def _expected_dim_for(model: str) -> Optional[int]:
    key = model.split(":")[0].lower()
    return _EMBED_DIM_HINTS.get(key)


def check_chroma_db(db_path: Path, embed_model: str) -> CheckResult:
    """Verify the local ChromaDB opens AND its vector dim matches our
    embedding model. A mismatch means the DB was indexed with a
    different model — the user must re-embed."""
    if not db_path.exists():
        return CheckResult(
            name="ChromaDB / RAG store",
            status="warn",
            message=f"`{db_path}` not found",
            detail="RAG features are disabled until you index docs. Run `python -m scripts.update_rag`.",
        )
    try:
        import chromadb  # type: ignore
        client = chromadb.PersistentClient(path=str(db_path))
        try:
            col = client.get_collection(name="cad_docs")
        except Exception:
            # Collection missing — DB is openable but empty.
            return CheckResult(
                name="ChromaDB / RAG store",
                status="warn",
                message="DB opens but `cad_docs` collection is empty",
                detail="Index documents with `python -m scripts.update_rag`.",
            )
        # Probe vector dim by inspecting one record.
        peek = col.peek(limit=1)
        embeds = peek.get("embeddings") or []
        if not embeds:
            return CheckResult(
                name="ChromaDB / RAG store",
                status="warn",
                message="collection has no embeddings yet",
            )
        dim = len(embeds[0])
        expected = _expected_dim_for(embed_model)
        if expected and dim != expected:
            return CheckResult(
                name="ChromaDB / RAG store",
                status="fail",
                message=f"vector dim mismatch — got {dim}, expected {expected} for `{embed_model}`",
                detail=f"Delete `{db_path}/` and re-run `python -m scripts.update_rag` so the DB matches your current embedding model.",
            )
        return CheckResult(
            name="ChromaDB / RAG store",
            status="pass",
            message=f"OK — dim {dim} matches `{embed_model}`",
        )
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            name="ChromaDB / RAG store",
            status="fail",
            message=f"could not open DB: {e}",
        )


def check_sessions_dir(path: Path) -> CheckResult:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".healthcheck"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return CheckResult(name="Sessions dir writable", status="pass", message=str(path))
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            name="Sessions dir writable",
            status="fail",
            message=f"cannot write to {path}: {e}",
        )


def check_proxy_port(port: int, *, is_self: bool = False) -> CheckResult:
    """If the proxy is already running, this'll see the port as taken —
    that's only a problem if we're trying to start a SECOND copy. The
    UI calls this with is_self=True so it doesn't flag itself."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.bind(("localhost", port))
        s.close()
        return CheckResult(name=f"Port {port} free", status="pass", message="available")
    except OSError:
        if is_self:
            return CheckResult(name=f"Port {port}", status="pass", message="bound (this process)")
        return CheckResult(
            name=f"Port {port} free",
            status="warn",
            message=f"port {port} already taken",
            detail="Another LocalHelpBot proxy might be running — close that tab first.",
        )
    finally:
        try:
            s.close()
        except Exception:
            pass


# ───────────────────────────────────────────────────────────────────────
# Aggregation
# ───────────────────────────────────────────────────────────────────────


def check_vision_model(base: str, vision_model: str) -> CheckResult:
    """v4 Slice 5: vision model is OPTIONAL — warn instead of fail."""
    if not vision_model:
        return CheckResult(
            name="Vision model (llava)", status="warn",
            message="VISION_MODEL is empty — describe_image / vision-agent disabled.",
        )
    available = _ollama_models(base) or []
    avail_norm = [m.lower() for m in available]
    target = vision_model.lower()
    if target in avail_norm or target.split(":")[0] in [a.split(":")[0] for a in avail_norm]:
        return CheckResult(name=f"Vision model `{vision_model}`", status="pass", message="installed")
    return CheckResult(
        name=f"Vision model `{vision_model}`", status="warn",
        message="not installed — describe_image will fail until you pull it",
        detail=f"Run: `ollama pull {vision_model.split(':')[0]}`",
    )


def check_lessons_writable(path: Path) -> CheckResult:
    """v4 Slice 4: lessons.jsonl must be writable."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        probe = path.parent / ".lessons-healthcheck"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return CheckResult(name="Lessons store writable", status="pass", message=str(path))
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            name="Lessons store writable", status="fail",
            message=f"cannot write to {path}: {e}",
        )


def run_all(*, is_self: bool = False) -> Tuple[str, List[CheckResult]]:
    """Run every check. Returns (overall_status, results).

    `overall_status`:
      "green"  — all pass
      "yellow" — at least one warn, no fail
      "red"    — at least one fail
    """
    from config import OLLAMA_BASE, CHAT_MODEL, EMBED_MODEL, LARGE_MODEL, PROXY_PORT
    try:
        from config import VISION_MODEL   # type: ignore
    except Exception:
        VISION_MODEL = ""   # noqa: N806

    results: List[CheckResult] = []
    results.append(check_ollama(OLLAMA_BASE))
    if results[-1].status != "fail":
        results.extend(check_models_present(OLLAMA_BASE, [CHAT_MODEL, EMBED_MODEL, LARGE_MODEL]))
        results.append(check_vision_model(OLLAMA_BASE, VISION_MODEL))
    results.append(check_chroma_db(ROOT / "cad_db", EMBED_MODEL))
    results.append(check_sessions_dir(ROOT / "data" / "sessions"))
    results.append(check_lessons_writable(ROOT / "data" / "lessons.jsonl"))
    results.append(check_proxy_port(PROXY_PORT, is_self=is_self))

    has_fail = any(r.status == "fail" for r in results)
    has_warn = any(r.status == "warn" for r in results)
    overall = "red" if has_fail else ("yellow" if has_warn else "green")
    return overall, results


def to_dict(overall: str, results: List[CheckResult]) -> Dict[str, Any]:
    return {
        "status": overall,
        "checks": [asdict(r) for r in results],
    }


# ───────────────────────────────────────────────────────────────────────
# CLI
# ───────────────────────────────────────────────────────────────────────


def _cli_main() -> int:
    overall, results = run_all(is_self=False)
    try:
        from rich.console import Console
        from rich.table import Table
        console = Console()
        color = {"green": "green", "yellow": "yellow", "red": "red"}[overall]
        console.rule(f"[bold {color}]LocalHelpBot healthcheck — {overall.upper()}[/bold {color}]")
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Check", style="cyan")
        table.add_column("Status")
        table.add_column("Message", overflow="fold")
        table.add_column("Hint", overflow="fold", style="dim")
        for r in results:
            symbol = {"pass": "[green]PASS[/green]", "warn": "[yellow]WARN[/yellow]", "fail": "[red]FAIL[/red]"}[r.status]
            table.add_row(r.name, symbol, r.message, r.detail or "")
        console.print(table)
    except Exception:
        # No rich — plain output.
        print(f"LocalHelpBot healthcheck — {overall.upper()}")
        for r in results:
            print(f"  [{r.status.upper():4}] {r.name}: {r.message}")
            if r.detail:
                print(f"        hint: {r.detail}")

    return 0 if overall != "red" else 1


if __name__ == "__main__":
    # Make sure project root is on sys.path before we touch config.
    sys.path.insert(0, str(ROOT))
    sys.exit(_cli_main())
