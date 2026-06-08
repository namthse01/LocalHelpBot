"""Compare — blind side-by-side model evaluation.

Ported (adapted, not copied) from odysseus ``routes/compare_routes.py``. The
odysseus version is FastAPI + SQLAlchemy + multi-user auth + ephemeral SSE
sessions; TheAgent0 is a single-user local tool, so this is a synchronous,
stdlib-first, JSONL-backed re-implementation that keeps the *concepts*:

  • run the SAME prompt against N models,
  • show their answers **blind** (slot A/B/C… — no model names),
  • let the user vote for the best slot, then **reveal** which model won,
  • keep a small history.

Storage mirrors :mod:`core.lessons` exactly (append-only JSONL, ``threading``
lock, ``MAX_ENTRIES`` cap, module singleton + a ``reset_*_for_tests`` helper).

The model calls reuse :class:`core.providers.LocalProvider` — but the provider
is injected via ``provider_factory`` so tests can pass a mock and never touch
the network. Models run **concurrently** (one thread each) so comparing 4
models costs ~1× latency, not 4×; a per-model wall-clock cap
(``config.COMPARE_TIMEOUT_S``) turns a hung model into an error slot instead of
blocking the whole comparison.

Public surface:
    run_comparison(prompt, models, *, provider_factory=None, …) -> Comparison
    Comparison.public_dict(reveal=False)   # blind by default; reveal on vote
    get_compare_store() / reset_compare_store_for_tests(path)
"""
from __future__ import annotations

import json
import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


MAX_ENTRIES = 200
DEFAULT_PATH = Path("data") / "compare" / "comparisons.jsonl"


# ───────────────────────────────────────────────────────────────────────
# Data model
# ───────────────────────────────────────────────────────────────────────
@dataclass
class Comparison:
    """One blind comparison run.

    ``slots`` is the *shuffled* per-model result list — its index order is the
    blind A/B/C… order the user sees, deliberately decoupled from ``models``
    (the pick order) so the layout leaks nothing about identity. Each slot is a
    plain dict: ``{model, content, latency_ms, completion_tokens, tok_per_sec,
    error}``.
    """

    id: str
    ts: float
    prompt: str
    models: List[str] = field(default_factory=list)        # pick order
    slots: List[Dict[str, Any]] = field(default_factory=list)  # shuffled / display order
    is_blind: bool = True
    voted: bool = False
    winner: Optional[str] = None                            # winning *model name*

    def to_dict(self) -> dict:
        return asdict(self)

    # ── view helpers ─────────────────────────────────────────────────
    def public_dict(self, reveal: bool = False) -> dict:
        """Serialize for the API.

        Blind by default: slot ``model`` names (and the winner / pick list) are
        withheld until ``reveal=True`` (set after the user votes). A comparison
        that was never blind to begin with always reveals.
        """
        show = reveal or not self.is_blind
        out: Dict[str, Any] = {
            "id": self.id,
            "ts": self.ts,
            "prompt": self.prompt,
            "n": len(self.slots),
            "voted": self.voted,
            "is_blind": self.is_blind,
            "revealed": bool(show),
            "slots": [],
        }
        for i, s in enumerate(self.slots):
            slot: Dict[str, Any] = {
                "slot": i,
                "content": s.get("content", ""),
                "latency_ms": s.get("latency_ms", 0),
                "completion_tokens": s.get("completion_tokens", 0),
                "tok_per_sec": s.get("tok_per_sec", 0.0),
                "error": s.get("error", ""),
            }
            if show:
                slot["model"] = s.get("model", "")
                slot["is_winner"] = bool(self.winner and s.get("model") == self.winner)
            out["slots"].append(slot)
        if show:
            out["winner"] = self.winner
            out["models"] = list(self.models)
        return out

    def summary_dict(self) -> dict:
        """Compact record for the history list (always revealed — it's past)."""
        return {
            "id": self.id,
            "ts": self.ts,
            "prompt": self.prompt[:200],
            "models": list(self.models),
            "winner": self.winner,
            "voted": self.voted,
            "n": len(self.slots),
        }


# ───────────────────────────────────────────────────────────────────────
# Store (JSONL, append-only — mirrors core.lessons.LessonsStore)
# ───────────────────────────────────────────────────────────────────────
class ComparisonStore:
    """Append-only JSONL-backed comparison store. Thread-safe."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or DEFAULT_PATH
        self._lock = threading.Lock()
        self._entries: List[Comparison] = []
        self._loaded = False

    # ── persistence ──────────────────────────────────────────────────
    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._path.exists():
            return
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self._entries.append(Comparison(
                        id=str(d.get("id") or ""),
                        ts=float(d.get("ts") or 0),
                        prompt=str(d.get("prompt") or ""),
                        models=list(d.get("models") or []),
                        slots=list(d.get("slots") or []),
                        is_blind=bool(d.get("is_blind", True)),
                        voted=bool(d.get("voted", False)),
                        winner=(d.get("winner") or None),
                    ))
        except OSError:
            return
        if len(self._entries) > MAX_ENTRIES:
            self._entries = self._entries[-MAX_ENTRIES:]

    def _rewrite(self) -> None:
        """Rewrite the JSONL file from the in-memory list (after trim / vote)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                for e in self._entries:
                    fh.write(json.dumps(e.to_dict(), ensure_ascii=False) + "\n")
            tmp.replace(self._path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    def _append(self, entry: Comparison) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ── public API ───────────────────────────────────────────────────
    def record(self, comparison: Comparison) -> Comparison:
        """Persist a freshly-run comparison."""
        with self._lock:
            self._load()
            self._entries.append(comparison)
            self._append(comparison)
            if len(self._entries) > MAX_ENTRIES:
                self._entries = self._entries[-MAX_ENTRIES:]
                self._rewrite()
            return comparison

    def get(self, comparison_id: str) -> Optional[Comparison]:
        with self._lock:
            self._load()
            for c in self._entries:
                if c.id == comparison_id:
                    return c
        return None

    def set_winner(self, comparison_id: str, slot_index: int) -> Comparison:
        """Record the user's vote — maps a blind slot index to its real model,
        marks the comparison voted, and persists. Raises on bad id / slot."""
        with self._lock:
            self._load()
            for c in self._entries:
                if c.id == comparison_id:
                    if slot_index < 0 or slot_index >= len(c.slots):
                        raise ValueError(
                            f"slot {slot_index} out of range (0..{len(c.slots) - 1})"
                        )
                    c.winner = c.slots[slot_index].get("model", "") or None
                    c.voted = True
                    self._rewrite()
                    return c
        raise KeyError(f"comparison {comparison_id!r} not found")

    def list(self, *, limit: int = 50) -> List[Comparison]:
        """Most-recent-first list of comparisons."""
        with self._lock:
            self._load()
            return list(reversed(self._entries))[: max(0, limit)]

    def all(self) -> List[Comparison]:
        with self._lock:
            self._load()
            return list(self._entries)

    def clear(self) -> int:
        """Wipe the store. Returns the number of entries removed."""
        with self._lock:
            self._load()
            n = len(self._entries)
            self._entries = []
            try:
                self._path.unlink(missing_ok=True)
            except OSError:
                pass
            return n


# ───────────────────────────────────────────────────────────────────────
# Runner
# ───────────────────────────────────────────────────────────────────────
def _cfg(name: str, default: Any) -> Any:
    """Read a config tunable defensively (config may be partially loaded)."""
    try:
        import config  # type: ignore
        return getattr(config, name, default)
    except Exception:
        return default


def _error_slot(model: str, message: str) -> Dict[str, Any]:
    return {
        "model": model,
        "content": "",
        "latency_ms": 0,
        "completion_tokens": 0,
        "tok_per_sec": 0.0,
        "error": message,
    }


def _run_one(
    provider_factory: Callable[[str], Any],
    model: str,
    messages: List[Dict[str, str]],
) -> Dict[str, Any]:
    """Call a single model and shape its result into a slot dict."""
    provider = provider_factory(model)
    resp = provider.chat(messages)
    return {
        "model": model,
        "content": resp.content or "",
        "latency_ms": getattr(resp, "latency_ms", 0),
        "completion_tokens": getattr(resp, "completion_tokens", 0),
        "tok_per_sec": round(float(getattr(resp, "tok_per_sec", 0.0) or 0.0), 1),
        "error": "",
    }


def _clean_models(models: List[str], cap: int) -> List[str]:
    """Strip blanks, de-duplicate (preserving order), and cap the count."""
    seen: set = set()
    clean: List[str] = []
    for m in models or []:
        m = (m or "").strip()
        if m and m not in seen:
            seen.add(m)
            clean.append(m)
    if cap and len(clean) > cap:
        clean = clean[:cap]
    return clean


def run_comparison(
    prompt: str,
    models: List[str],
    *,
    provider_factory: Optional[Callable[[str], Any]] = None,
    system: Optional[str] = None,
    store: Optional[ComparisonStore] = None,
    max_models: Optional[int] = None,
    timeout_s: Optional[float] = None,
) -> Comparison:
    """Run ``prompt`` against ``models`` concurrently and persist a blind
    :class:`Comparison`.

    Parameters
    ----------
    provider_factory:
        ``model_name -> provider`` where provider has ``.chat(messages)``.
        Defaults to :class:`core.providers.LocalProvider` (Ollama). Injected so
        tests can supply a mock with zero network.
    system:
        Optional system message prepended to every model's prompt (default:
        none — a bare user prompt keeps the comparison neutral).
    max_models / timeout_s:
        Override ``config.COMPARE_MAX_MODELS`` / ``config.COMPARE_TIMEOUT_S``.

    Raises ``ValueError`` if the prompt is empty or no usable model is given.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("prompt is required")

    cap = max_models if max_models is not None else int(_cfg("COMPARE_MAX_MODELS", 4))
    clean = _clean_models(models, cap)
    if not clean:
        raise ValueError("at least one model is required")

    tmo = timeout_s if timeout_s is not None else float(_cfg("COMPARE_TIMEOUT_S", 120))

    if provider_factory is None:
        from core.providers import LocalProvider
        provider_factory = lambda m: LocalProvider(m)  # noqa: E731

    messages: List[Dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    # Run every model concurrently; bound the whole batch by `tmo`. We avoid the
    # `with ThreadPoolExecutor` context manager because its __exit__ does
    # shutdown(wait=True) — which would block on a hung model and defeat the
    # timeout. shutdown(wait=False) lets a stuck request linger harmlessly while
    # we return its slot as a timeout error.
    results: Dict[int, Dict[str, Any]] = {}
    ex = ThreadPoolExecutor(max_workers=max(1, len(clean)))
    try:
        fut_map = {
            ex.submit(_run_one, provider_factory, m, messages): (i, m)
            for i, m in enumerate(clean)
        }
        done, not_done = wait(list(fut_map.keys()), timeout=tmo)
        for fut in done:
            i, m = fut_map[fut]
            try:
                results[i] = fut.result()
            except Exception as e:  # noqa: BLE001
                results[i] = _error_slot(m, f"{type(e).__name__}: {e}")
        for fut in not_done:
            i, m = fut_map[fut]
            results[i] = _error_slot(m, f"timed out after {tmo:g}s")
            fut.cancel()
    finally:
        ex.shutdown(wait=False)

    slots = [results[i] for i in range(len(clean))]
    random.shuffle(slots)   # blind ordering — decouple display from pick order

    comp = Comparison(
        id=uuid.uuid4().hex[:12],
        ts=time.time(),
        prompt=prompt,
        models=clean,
        slots=slots,
        is_blind=True,
        voted=False,
        winner=None,
    )
    (store or get_compare_store()).record(comp)
    return comp


# ───────────────────────────────────────────────────────────────────────
# Module-level singleton
# ───────────────────────────────────────────────────────────────────────
_store: Optional[ComparisonStore] = None
_singleton_lock = threading.Lock()


def get_compare_store() -> ComparisonStore:
    global _store
    if _store is None:
        with _singleton_lock:
            if _store is None:
                _store = ComparisonStore()
    return _store


def reset_compare_store_for_tests(path: Optional[Path] = None) -> ComparisonStore:
    """Test helper — swap in a fresh store, optionally with a custom path."""
    global _store
    with _singleton_lock:
        _store = ComparisonStore(path=path)
    return _store


__all__ = [
    "Comparison",
    "ComparisonStore",
    "run_comparison",
    "get_compare_store",
    "reset_compare_store_for_tests",
]
