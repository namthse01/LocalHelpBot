"""
RAG-aware + Agentic Ollama proxy  — port 11435
"""

import copy
import json
import mimetypes
import multiprocessing
import os
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if __name__ == "__main__":
    multiprocessing.freeze_support()

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import OLLAMA_BASE, CHAT_MODEL, LARGE_MODEL, PROXY_PORT, AGENT_PROFILES
from core.orchestrator import AgentOrchestrator
from core import pipeline  # input preprocessing: normalize + extract + assemble
from core import memory as mem  # session-memory helpers (summary preservation, budget)
from core.conversation_store import derive_session_id, get_store
from core.logs import get_logger

log = get_logger("proxy")

def _get_tools():
    from core.tools import FILE_TOOLS, WEB_TOOLS
    return FILE_TOOLS, WEB_TOOLS

def _get_browser_tools():
    from core.browser import BROWSER_TOOLS
    return BROWSER_TOOLS

def _get_agent():
    from core.agent import run_agent
    return run_agent

REAL_MODEL = CHAT_MODEL
CAD_MODEL     = "cad-rag"
UI_MODEL      = "ui-agent"
CODE_MODEL    = "code-agent"
WEB_MODEL     = "web-creep"
BROWSER_MODEL = "browser-agent"
DEEP_MODEL    = "deep-agent"
AUTO_MODEL     = "auto-agent"
VISION_MODEL_V = "vision-agent"   # v4 Slice 5

VIRTUAL_MODELS = [CAD_MODEL, UI_MODEL, CODE_MODEL, WEB_MODEL, BROWSER_MODEL, DEEP_MODEL, AUTO_MODEL, VISION_MODEL_V]

# (Legacy: CAD_SYSTEM + SCORING_THRESHOLD removed in v4. cad-rag now routes
# through `cad-rag-specialist` profile + agentic query_rag tool. See config.py.)
UI_SYSTEM = "You are a UI/Frontend specialist agent..."
CODE_SYSTEM = "You are an autonomous coding agent..."
BROWSER_SYSTEM = "You are browser-agent, a local browser data reader..."
WEB_SYSTEM = "You are web-creep, an autonomous web research agent..."

def _ensure_budget(messages: list, *, session_id: str = "") -> list:
    """Server-side compaction safety net.

    Thin wrapper around `MemoryEngine.compact()`. Persists the resulting
    summary to the conversation store when a session_id is known so the
    next turn can read it from T2 instead of relying on the client to
    echo the marker block back.
    """
    if not messages:
        return messages
    try:
        from core.providers import smart_provider as _sp
        engine = mem.get_default_engine()
        prior = engine.extract_summary(messages)
        result = engine.compact(messages, summarizer=_sp, prior_summary=prior)
        if result.fired:
            log.info("compact safety-net fired", extra={"session_id": session_id})
            if session_id:
                get_store().note(session_id, summary=result.summary)
        return result.messages
    except Exception as e:  # noqa: BLE001 — never fail a request on this
        log.warning(f"compact safety-net failed (non-fatal): {e}", extra={"session_id": session_id})
        return messages


def _inject_system(messages: list, system: str, *, session_id: str = "") -> list:
    """Replace the system prompt for forward-only specialists (ui-agent,
    web-creep, browser-agent). Always merges the virtual-model `system`
    with any carried summary; any non-summary system message from the
    client is appended below ours rather than skipped.

    v4 Slice 0.3: dropped the legacy early-return that silently kept the
    client's system message instead of merging ours — that path could
    swallow `UI_SYSTEM` / `WEB_SYSTEM` entirely.
    """
    engine = mem.get_default_engine()
    carried = ""
    if session_id:
        sess = get_store().get(session_id)
        if sess and sess.summary:
            carried = sess.summary
    if not carried:
        carried = engine.extract_summary(messages)
    merged = engine.merge_summary(system, carried)
    out = [{"role": "system", "content": merged}]
    for m in messages:
        if m.get("role") == "system":
            # Fold any non-summary system content the caller sent into ours.
            if not mem._looks_like_summary(mem._content_text(m)):
                out[0]["content"] = out[0]["content"] + "\n\n" + mem._content_text(m)
            continue
        out.append(m)
    return out

def _session_id_from_request(headers: dict, payload: dict) -> str:
    """Derive a stable session_id for this chat request.

    Order of preference:
      1. `X-Session-Id` HTTP header (UI / Discord adapter / MCP send it).
      2. `session_id` field in the JSON body (some clients prefer body).
      3. Hash of (first user message + today's date) — fallback for
         vanilla Ollama clients that ship no identity at all.
    """
    explicit = ""
    if isinstance(headers, dict):
        for k, v in headers.items():
            if k.lower() == "x-session-id":
                explicit = (v or "").strip()
                break
    if not explicit and isinstance(payload, dict):
        explicit = (payload.get("session_id") or "").strip()
    return derive_session_id(payload.get("messages", []) if isinstance(payload, dict) else [], explicit=explicit)


def _last_user_msg(messages: list) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            c = msg.get("content", "")
            if isinstance(c, list):
                return " ".join(p.get("text", "") for p in c if p.get("type") == "text")
            return str(c)
    return ""

def _forward(path: str, body: bytes, headers: dict):
    url = OLLAMA_BASE + path
    req = urllib.request.Request(url, data=body, method="POST")
    for k, v in headers.items():
        if k.lower() in ("content-type", "accept"):
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "application/json")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), "application/json"
    except Exception as e:
        return 500, json.dumps({"error": str(e)}).encode(), "application/json"

def _tags_with_virtual() -> bytes:
    try:
        with urllib.request.urlopen(OLLAMA_BASE + "/api/tags", timeout=5) as r:
            data = json.loads(r.read())
    except Exception:
        data = {"models": []}
    virtual = [
        {"name": CAD_MODEL, "model": CAD_MODEL, "modified_at": "2026-01-01T00:00:00Z", "size": 0, "details": {"parameter_size": "7.6B", "family": "CAD/AutoCAD RAG agent"}},
        {"name": UI_MODEL, "model": UI_MODEL, "modified_at": "2026-01-01T00:00:00Z", "size": 0, "details": {"parameter_size": "7.6B", "family": "UI/Frontend agent"}},
        {"name": CODE_MODEL, "model": CODE_MODEL, "modified_at": "2026-01-01T00:00:00Z", "size": 0, "details": {"parameter_size": "7.6B", "family": "Agentic code agent"}},
        {"name": WEB_MODEL, "model": WEB_MODEL, "modified_at": "2026-01-01T00:00:00Z", "size": 0, "details": {"parameter_size": "7.6B", "family": "Web research agent"}},
        {"name": BROWSER_MODEL, "model": BROWSER_MODEL, "modified_at": "2026-01-01T00:00:00Z", "size": 0, "details": {"parameter_size": "7.6B", "family": "Browser cookies/storage reader"}},
        {"name": DEEP_MODEL, "model": DEEP_MODEL, "modified_at": "2026-01-01T00:00:00Z", "size": 0, "details": {"parameter_size": "19B", "family": "Deep reasoning agent (glm-4.7-flash)"}},
        {"name": AUTO_MODEL, "model": AUTO_MODEL, "modified_at": "2026-01-01T00:00:00Z", "size": 0, "details": {"parameter_size": "7.6B", "family": "Smart Router Agent"}},
        {"name": VISION_MODEL_V, "model": VISION_MODEL_V, "modified_at": "2026-01-01T00:00:00Z", "size": 0, "details": {"parameter_size": "?", "family": "Multimodal vision agent (llava)"}},
    ]
    data["models"] = virtual + data.get("models", [])
    return json.dumps(data).encode()

def _init_orchestrator():
    FILE_TOOLS, WEB_TOOLS = _get_tools()
    all_tools = {**FILE_TOOLS, **WEB_TOOLS}
    return AgentOrchestrator(all_tools)

orchestrator = _init_orchestrator()

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

# ── Shutdown management ──────────────────────────────────────
_server_ref = None
_last_heartbeat = 0.0
_heartbeat_active = False

_RECENT_STATS = []  # newest-first, capped at 20
def _record_stat(prompt: str, total_ms: int, active: str, llm_timing: str):
    _RECENT_STATS.insert(0, {
        "prompt": (prompt or "")[:120],
        "total_ms": total_ms,
        "active": active,
        "llm": llm_timing,
    })
    del _RECENT_STATS[20:]
# Shutdown only after prolonged UI silence. 5 minutes gives slow-tab
# browsers (Chrome throttles background setInterval to ~1/min) plenty of
# room, and any in-flight request bumps the deadline anyway.
HEARTBEAT_TIMEOUT = 300
_inflight_requests = 0  # active agentic requests — watchdog won't shut down while > 0
_inflight_lock = threading.Lock()

def _mark_activity():
    """Any UI-originating request counts as a liveness signal."""
    import time
    global _last_heartbeat, _heartbeat_active
    _last_heartbeat = time.time()
    _heartbeat_active = True

def _inflight_inc():
    global _inflight_requests
    with _inflight_lock:
        _inflight_requests += 1

def _inflight_dec():
    global _inflight_requests
    with _inflight_lock:
        _inflight_requests = max(0, _inflight_requests - 1)

def _shutdown_all():
    _stop_discord()
    log.info("All services stopped. Exiting.")
    if _server_ref:
        _server_ref.shutdown()
    sys.exit(0)

def _heartbeat_watchdog():
    """Shutdown only if UI has been idle AND no agent request is in flight.
    An in-flight request is strong evidence the user is still actively using
    the bot — never kill a working session."""
    import time
    global _last_heartbeat, _heartbeat_active
    while True:
        time.sleep(10)
        if not _heartbeat_active:
            continue
        if _inflight_requests > 0:
            # User is actively waiting on us — extend deadline, don't kill.
            _last_heartbeat = time.time()
            continue
        idle = time.time() - _last_heartbeat
        if idle > HEARTBEAT_TIMEOUT:
            log.info(f"UI idle for {int(idle)}s (>{HEARTBEAT_TIMEOUT}s) "
                     f"and no in-flight work — shutting down.")
            _shutdown_all()
            break

class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # HTTP access lines go at DEBUG level — they're noisy (heartbeats, etc.)
        log.debug(fmt % args)

    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            try:
                with open("TheAgent0UI/index.html", "rb") as f:
                    self.wfile.write(f.read())
            except Exception as e:
                self.wfile.write(f"Error loading index.html: {e}".encode())
            return

        if self.path.startswith("/static/"):
            file_path = self.path[len("/static/"):]
            try:
                with open(f"TheAgent0UI/{file_path}", "rb") as f:
                    content = f.read()
                self.send_response(200)
                mime = "text/javascript" if file_path.endswith(".js") else "text/css" if file_path.endswith(".css") else "application/octet-stream"
                self.send_header("Content-Type", mime)
                self.end_headers()
                self.wfile.write(content)
                return
            except Exception as e:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(f"File not found: {e}".encode())
                return

        # ── /data/generated/ static route ─────────────────────────────
        # Serves images produced by the `generate_image` tool so the chat
        # UI can render them inline via markdown <img>. Scoped strictly to
        # data/generated/ — NEVER serves data/sessions/ (user prompts) or
        # other sensitive subtrees. Path traversal is rejected.
        if self.path.startswith("/data/generated/"):
            rel = self.path[len("/data/"):]  # e.g. "generated/sd-X.png"
            # Strip query string before resolving on disk.
            if "?" in rel:
                rel = rel.split("?", 1)[0]
            try:
                decoded = urllib.parse.unquote(rel)
            except Exception:
                self.send_response(400); self.end_headers(); return
            # Reject absolute paths, drive letters, traversal segments.
            if (
                decoded.startswith("/")
                or decoded.startswith("\\")
                or ":" in decoded
                or ".." in decoded.replace("\\", "/").split("/")
            ):
                self.send_response(403); self.end_headers(); return
            project_root = Path(__file__).resolve().parent.parent
            data_root = (project_root / "data").resolve()
            allowed_root = (data_root / "generated").resolve()
            abs_path = (data_root / decoded).resolve()
            # Defense in depth: even after normalization, confirm the
            # resolved path is inside data/generated/.
            try:
                abs_path.relative_to(allowed_root)
            except ValueError:
                self.send_response(403); self.end_headers(); return
            if not abs_path.exists() or not abs_path.is_file():
                self.send_response(404); self.end_headers(); return
            ct, _ = mimetypes.guess_type(str(abs_path))
            data = abs_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ct or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(data)
            return

        # Any other /data/* subtree is explicitly NOT served.
        if self.path.startswith("/data/"):
            self.send_response(404); self.end_headers(); return

        if self.path == "/api/config":
            from config import DISCORD_SETTINGS, AGENT_PROFILES, AUTOMATION_TASKS, MODEL_PROVIDERS
            from core.secrets import mask_secret
            import copy
            masked_providers = copy.deepcopy(MODEL_PROVIDERS)
            for _slot in ("primary", "fallback"):
                _p = masked_providers.get(_slot) or {}
                if _p.get("api_key"):
                    _p["api_key"] = mask_secret(_p["api_key"])
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "discord": DISCORD_SETTINGS,
                "agents": AGENT_PROFILES,
                "tasks": AUTOMATION_TASKS,
                "providers": masked_providers
            }).encode())
            return

        if self.path == "/api/discord/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"connected": _discord_status()}).encode())
            return

        if self.path == "/api/permissions/pending":
            from core.permissions import list_pending
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"pending": list_pending()}).encode())
            return

        if self.path == "/api/stats" or self.path == "/api/stats/turns":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"recent": _RECENT_STATS}).encode())
            return

        # v4 Slice 1: Conversation sidebar — list known sessions or
        # rehydrate one. /api/sessions returns most-recent-first session
        # summaries; /api/sessions/<sid> returns the full turn history
        # from the JSONL crash log.
        if self.path == "/api/sessions":
            try:
                from core.conversation_store import get_store
                sessions = sorted(
                    get_store().all_sessions(),
                    key=lambda s: s.last_seen,
                    reverse=True,
                )
                payload = {"sessions": [
                    {
                        "sid": s.sid,
                        "goal": s.goal,
                        "last_seen": s.last_seen,
                        "turn_count": s.turn_count,
                        "last_profile": s.last_profile,
                        "source": s.source,
                    }
                    for s in sessions[:50]
                ]}
            except Exception as e:
                payload = {"error": str(e), "sessions": []}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())
            return

        if self.path.startswith("/api/sessions/"):
            sid = self.path[len("/api/sessions/"):].split("?", 1)[0]
            try:
                from core.conversation_store import get_store
                sess = get_store().get(sid)
                if sess is None:
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "not found"}).encode())
                    return
                # Read JSONL events for transcript reconstruction.
                jsonl = Path("data/sessions") / f"{sid}.jsonl"
                events = []
                if jsonl.exists():
                    try:
                        for line in jsonl.read_text(encoding="utf-8").splitlines():
                            try:
                                events.append(json.loads(line))
                            except json.JSONDecodeError:
                                continue
                    except OSError:
                        pass
                payload = {
                    "session": {
                        "sid": sess.sid,
                        "goal": sess.goal,
                        "summary": sess.summary,
                        "files_touched": sess.files_touched,
                        "sticky": sess.sticky,
                        "last_seen": sess.last_seen,
                        "last_profile": sess.last_profile,
                        "turn_count": sess.turn_count,
                        "source": sess.source,
                    },
                    "events": events,
                }
            except Exception as e:
                payload = {"error": str(e)}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())
            return

        if self.path == "/api/healthcheck":
            try:
                from core.healthcheck import run_all, to_dict
                overall, results = run_all(is_self=True)
                payload = to_dict(overall, results)
            except Exception as e:
                payload = {"status": "red", "checks": [{"name": "healthcheck", "status": "fail", "message": str(e)}]}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())
            return

        if self.path.startswith("/api/logs/tail"):
            # Query params: subsystem=<name>&since=<ts>&limit=<n>
            from urllib.parse import urlparse, parse_qs
            from core.logs import tail_events, SUBSYSTEMS
            qs = parse_qs(urlparse(self.path).query)
            subsystem = (qs.get("subsystem") or [None])[0]
            since = None
            try:
                if qs.get("since"):
                    since = float(qs["since"][0])
            except (TypeError, ValueError):
                since = None
            limit = 200
            try:
                if qs.get("limit"):
                    limit = max(1, min(1000, int(qs["limit"][0])))
            except (TypeError, ValueError):
                limit = 200
            events = tail_events(subsystem=subsystem, since_ts=since, limit=limit)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({
                "events": events,
                "subsystems": SUBSYSTEMS,
            }).encode())
            return

        if self.path == "/api/tags":
            body = _tags_with_virtual()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return

        url = OLLAMA_BASE + self.path
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                data = r.read()
                ct   = r.headers.get("Content-Type", "application/json")
                self.send_response(r.status)
                self.send_header("Content-Type", ct)
                self.end_headers()
                self.wfile.write(data)
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_POST(self):
        length  = int(self.headers.get("Content-Length", 0))
        raw     = self.rfile.read(length)
        path    = self.path
        is_chat = path in ("/api/chat", "/v1/chat/completions")

        # Any POST from the UI counts as liveness — heartbeat alone isn't
        # reliable when the tab is backgrounded or when a streaming chat
        # response is hogging the connection.
        _mark_activity()

        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}

        if path == "/api/config":
            try:
                import config as _config
                from pathlib import Path as _Path
                overrides_path = _Path(_config.__file__).parent / "runtime_overrides.json"
                existing = {}
                if overrides_path.exists():
                    try:
                        existing = json.loads(overrides_path.read_text(encoding="utf-8"))
                    except Exception:
                        existing = {}

                applied = []
                if "providers" in payload:
                    from core.secrets import encrypt_secret
                    incoming = payload["providers"]
                    # Preserve existing encrypted key if UI echoes the mask
                    existing_providers = existing.get("providers") or {}
                    for _slot in ("primary", "fallback"):
                        _new = incoming.get(_slot) or {}
                        _key = _new.get("api_key", "")
                        if _key and set(_key) <= {"•", "*"}:
                            _new["api_key"] = (existing_providers.get(_slot) or {}).get("api_key", "")
                    # Persist with encrypted api_key, keep in-memory plaintext
                    to_persist = copy.deepcopy(incoming)
                    for _slot in ("primary", "fallback"):
                        _p = to_persist.get(_slot) or {}
                        if _p.get("api_key"):
                            _p["api_key"] = encrypt_secret(_p["api_key"])
                    existing["providers"] = to_persist
                    # In-memory stays plaintext for actual API calls
                    in_mem = copy.deepcopy(incoming)
                    _config.MODEL_PROVIDERS = in_mem
                    applied.append("providers")
                if "agents" in payload:
                    _config.AGENT_PROFILES = payload["agents"]
                    existing["agents"] = payload["agents"]
                    applied.append("agents")
                if "discord" in payload:
                    _config.DISCORD_SETTINGS = payload["discord"]
                    existing["discord"] = payload["discord"]
                    applied.append("discord")
                if "tasks" in payload:
                    _config.AUTOMATION_TASKS = payload["tasks"]
                    existing["tasks"] = payload["tasks"]
                    applied.append("tasks")

                overrides_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")

                if "providers" in payload:
                    try:
                        from core.providers import smart_provider
                        smart_provider.reload()
                    except Exception as e:
                        log.warning(f"smart_provider reload failed: {e}")

                log.info(f"config applied & persisted: {applied}")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "success", "applied": applied}).encode())
                return
            except Exception as e:
                log.error(f"config save failed: {e}")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
                return

        # v4 Slice 1: drag-and-drop file upload.
        # Body shape: { "filename": "doc.pdf", "content_b64": "...", "session_id": "..." }
        # Saves to data/uploads/<sid>/<filename>, returns the absolute path
        # so the agent's first tool call can read_file / read_pdf / read_docx it.
        if path == "/api/upload":
            try:
                import base64
                filename = (payload.get("filename") or "").strip()
                content_b64 = payload.get("content_b64") or ""
                sid = (payload.get("session_id") or "default").strip() or "default"
                if not filename or not content_b64:
                    raise ValueError("filename and content_b64 are required")
                # Sanitize filename — strip path separators.
                filename = Path(filename).name
                if not filename:
                    raise ValueError("invalid filename after sanitization")
                # Cap size at 30 MB to avoid OOM on large payloads.
                raw = base64.b64decode(content_b64)
                if len(raw) > 30 * 1024 * 1024:
                    raise ValueError(f"file too large ({len(raw)} bytes; max 30 MB)")
                uploads_dir = Path("data") / "uploads" / sid
                uploads_dir.mkdir(parents=True, exist_ok=True)
                dest = uploads_dir / filename
                dest.write_bytes(raw)
                abs_path = str(dest.resolve())
                log.info(f"upload: {filename} ({len(raw)} bytes) -> {abs_path}", extra={"session_id": sid})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "ok": True,
                    "path": abs_path,
                    "filename": filename,
                    "size": len(raw),
                }).encode())
                return
            except Exception as e:
                log.warning(f"upload failed: {e}")
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode())
                return

        if path == "/api/memory/summarize":
            # Tier-1 in-session memory compression.
            # Client posts the oldest slice of chat history; we return a
            # structured 8-section summary (Primary Request / Decisions /
            # Files / Tools / Errors / Feedback / Pending / Current State —
            # port of claude-code's compact prompt scaled for local models).
            # The client wraps the result with SUMMARY_MARKER so the server
            # recognises and preserves it across later system-prompt rewrites.
            try:
                from core.providers import smart_provider
                msgs = payload.get("messages", []) or []
                prior_summary = (payload.get("prior_summary") or "").strip()
                summary = ""
                if msgs:
                    prompt_msgs = mem.build_summarizer_messages(msgs)
                    if prior_summary:
                        prompt_msgs[-1]["content"] = (
                            f"Prior summary (extend this, do not discard facts):\n"
                            f"{prior_summary}\n\n" + prompt_msgs[-1]["content"]
                        )
                    resp = smart_provider.chat(
                        prompt_msgs, options={"temperature": 0.1, "num_predict": 1024}
                    )
                    summary = mem.wrap_summary_output(resp.content or "")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                # Include marker so the client can round-trip it into a tagged
                # system message without knowing our constants.
                self.wfile.write(json.dumps({
                    "summary": summary,
                    "marker": mem.SUMMARY_MARKER,
                    "marker_end": mem.SUMMARY_END,
                }).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e), "summary": ""}).encode())
            return

        if path == "/api/permissions/resolve":
            from core.permissions import resolve
            ok = resolve(payload.get("id", ""), bool(payload.get("approved")), payload.get("scope", "once"))
            self.send_response(200 if ok else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": ok}).encode())
            return

        if path == "/api/discord/connect":
            ok, msg = _start_discord()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"success": ok, "message": msg, "connected": _discord_status()}).encode())
            log.info(f"discord: {msg}")
            return

        if path == "/api/discord/disconnect":
            ok, msg = _stop_discord()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"success": ok, "message": msg, "connected": _discord_status()}).encode())
            log.info(f"discord: {msg}")
            return

        if path == "/api/ui-closing":
            # UI beacon on tab close / navigation away. We DON'T shutdown
            # immediately (user might be refreshing) — instead we rewind the
            # heartbeat clock so the watchdog fires ~15s from now unless a
            # new heartbeat arrives (reload sends one instantly). Also refuse
            # to wind down if there's still work in flight.
            import time
            global _last_heartbeat
            if _inflight_requests <= 0:
                _last_heartbeat = time.time() - (HEARTBEAT_TIMEOUT - 15)
                log.info("UI signaled close — 15s grace window started.")
            else:
                log.info("UI closing but work in flight — ignored.")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            return

        if path == "/api/heartbeat":
            # _mark_activity() already fired above; this endpoint is now a
            # simple 200 so the UI ping contract stays stable.
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())
            return

        if path == "/api/shutdown":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "shutting_down"}).encode())
            threading.Thread(target=_shutdown_all, daemon=True).start()
            return

        model = payload.get("model", "")

        # Derive a stable session id once per chat request and make sure
        # the store knows about it. We pass `sid` down into every model
        # branch so memory tiers can read T2 (goal/files/sticky/summary).
        sid = _session_id_from_request(dict(self.headers), payload) if is_chat else ""
        if sid:
            get_store().get_or_create(sid, source="ui")
            user_first = _last_user_msg(payload.get("messages", []))
            if user_first:
                get_store().note(sid, goal=user_first)

        if model == AUTO_MODEL and is_chat:
            messages = _ensure_budget(payload.get("messages", []), session_id=sid)
            # Enrich with extracted facts (paths, filenames, verbs) + output
            # contract. Orchestrator owns the role prompt, so base_system="".
            messages = pipeline.prepare(messages, base_system="")
            user_q = _last_user_msg(messages)
            log.info(f"orchestrator handling request: {user_q!r}", extra={"session_id": sid})
            self._run_agent_response(messages, {}, orchestrator_mode=True, user_q=user_q, session_id=sid)
            return

        if model == CAD_MODEL and is_chat:
            # v4 Slice 0.2: cad-rag now routes through the orchestrator like
            # any other specialist. Agent calls `query_rag` itself when it
            # needs retrieval — no more auto-injection.
            messages = _ensure_budget(payload.get("messages", []), session_id=sid)
            messages = pipeline.prepare(messages, base_system="")
            user_q = _last_user_msg(messages)
            self._run_agent_response(messages, {}, specialist="cad-rag-specialist", user_q=user_q, session_id=sid)
            return

        if model == UI_MODEL and is_chat:
            messages = _ensure_budget(payload.get("messages", []), session_id=sid)
            payload["messages"] = _inject_system(messages, UI_SYSTEM, session_id=sid)
            payload["model"]    = REAL_MODEL
            raw = json.dumps(payload).encode()
            self._forward_and_reply(path, raw, session_id=sid)
            return

        if model == CODE_MODEL and is_chat:
            # Route through the orchestrator's "main" specialist so the request
            # gets the full tool registry (incl. read_pdf/write_pdf/install_package
            # loaded from core/plugins/) AND the main profile's PLAN/ACT/VERIFY +
            # WHEN-A-TOOL-FAILS prompt. The legacy FILE_TOOLS+WEB_TOOLS path had
            # none of those, which is why PDF tasks silently failed.
            messages = _ensure_budget(payload.get("messages", []), session_id=sid)
            messages = pipeline.prepare(messages, base_system="")
            user_q = _last_user_msg(messages)
            self._run_agent_response(messages, {}, specialist="main", user_q=user_q, session_id=sid)
            return

        if model == WEB_MODEL and is_chat:
            _, WEB_TOOLS = _get_tools()
            messages = _ensure_budget(payload.get("messages", []), session_id=sid)
            messages = pipeline.prepare(messages, WEB_SYSTEM)
            self._run_agent_response(messages, WEB_TOOLS, session_id=sid)
            return

        if model == BROWSER_MODEL and is_chat:
            BROWSER_TOOLS = _get_browser_tools()
            messages = _ensure_budget(payload.get("messages", []), session_id=sid)
            messages = pipeline.prepare(messages, BROWSER_SYSTEM)
            self._run_agent_response(messages, BROWSER_TOOLS, session_id=sid)
            return

        if model == VISION_MODEL_V and is_chat:
            # v4 Slice 5: vision-agent routes through orchestrator using
            # the vision-specialist profile.
            messages = _ensure_budget(payload.get("messages", []), session_id=sid)
            messages = pipeline.prepare(messages, base_system="")
            user_q = _last_user_msg(messages)
            self._run_agent_response(messages, {}, specialist="vision-specialist", user_q=user_q, session_id=sid)
            return

        if model == DEEP_MODEL and is_chat:
            # Deep model gets its own compaction too — it's the most likely
            # to have a long context buildup since users pick it for complex
            # multi-turn discussions.
            payload["messages"] = _ensure_budget(payload.get("messages", []), session_id=sid)
            payload["model"] = LARGE_MODEL
            raw = json.dumps(payload).encode()
            self._forward_and_reply(path, raw, session_id=sid)
            return

        headers = dict(self.headers)
        status, resp_body, ct = _forward(path, raw, headers)
        self.send_response(status)
        self.send_header("Content-Type", ct)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)

    def _forward_and_reply(self, path: str, raw: bytes, *, session_id: str = ""):
        headers = dict(self.headers)
        status, resp_body, ct = _forward(path, raw, headers)
        self.send_response(status)
        self.send_header("Content-Type", ct)
        self.send_header("Access-Control-Allow-Origin", "*")
        if session_id:
            self.send_header("X-Session-Id", session_id)
        self.end_headers()
        self.wfile.write(resp_body)

    def _stream_headers(self, *, session_id: str = ""):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers", "X-Session-Id")
        if session_id:
            self.send_header("X-Session-Id", session_id)
        self.end_headers()

    def _event_emitter(self, wfile):
        def _human(event: dict) -> str:
            t = event.get("type")
            if t == "thought":
                return event.get("text", "") + "\n\n"
            if t == "tool_call":
                try:
                    args_str = json.dumps(event.get("args", {}), ensure_ascii=False)
                except Exception:
                    args_str = str(event.get("args", {}))
                if len(args_str) > 200:
                    args_str = args_str[:200] + "…"
                return f"[{event.get('tool','?')}] {args_str}\n"
            if t == "tool_result":
                ok = "✓" if event.get("ok") else "✗"
                preview = (event.get("preview") or "")[:300]
                return f"  {ok} {preview}\n\n"
            if t == "status":
                return f"· {event.get('text','')}\n"
            if t == "agent_start":
                return f"▶ [{event.get('agent','agent')}] start\n"
            if t == "agent_end":
                return ""
            if t == "final":
                return ""  # sent as done chunk separately
            return event.get("text", "")

        def emit(event):
            if isinstance(event, str):
                event = {"type": "text", "text": event}
            frame = {
                "model":       REAL_MODEL,
                "message":     {"role": "assistant", "content": _human(event)},
                "agent_event": event,
                "done":        False,
            }
            try:
                wfile.write((json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8"))
                wfile.flush()
            except Exception:
                pass
        return emit

    def _run_agent_response(self, messages: list, tools: dict, orchestrator_mode: bool = False, user_q: str = "", specialist: str = "", *, session_id: str = ""):
        import time as _t
        self._stream_headers(session_id=session_id)
        wfile = self.wfile
        emit = self._event_emitter(wfile)
        t0 = _t.perf_counter()
        _inflight_inc()
        try:
            if orchestrator_mode:
                final = orchestrator.handle_request(user_q, conversation=messages, stream_cb=emit, session_id=session_id)
            elif specialist:
                final = orchestrator.run_specialist(specialist, user_q, conversation=messages, stream_cb=emit, session_id=session_id)
            else:
                run_agent = _get_agent()
                final = run_agent(messages, tools, stream_cb=emit)
        except Exception as e:
            final = f"ERROR in agent loop: {e}"
        finally:
            _inflight_dec()
            _mark_activity()  # post-run bump so the next idle window starts fresh
        total_ms = int((_t.perf_counter() - t0) * 1000)
        try:
            from core.providers import smart_provider as _sp
            active = f"{_sp._last_provider or '?'}/{_sp._last_model or '?'}"
            last_timing = getattr(_sp, "_last_timing", "")
        except Exception:
            active, last_timing = "?", ""
        if orchestrator_mode or specialist:
            _record_stat(user_q, total_ms, active, last_timing)
        done_chunk = json.dumps({
            "model":          REAL_MODEL,
            "message":        {"role": "assistant", "content": final},
            "agent_event":    {"type": "done", "total_ms": total_ms, "active": active, "session_id": session_id},
            "done":           True,
            "done_reason":    "stop",
            "total_duration": 0,
            "session_id":     session_id,
        }) + "\n"
        try:
            wfile.write(done_chunk.encode())
            wfile.flush()
        except Exception:
            pass

def main():
    global _server_ref
    # Enable session persistence so a restarted proxy still recognises
    # in-flight conversations from the same day. JSONL is best-effort —
    # request handling never blocks on disk IO.
    sessions_dir = Path(__file__).parent.parent / "data" / "sessions"
    try:
        get_store().enable_jsonl_persistence(sessions_dir)
    except Exception as e:
        log.warning(f"session-store persistence disabled: {e}")

    server = ThreadingHTTPServer(("localhost", PROXY_PORT), ProxyHandler)
    server.daemon_threads = True
    _server_ref = server

    banner = (
        f"TheAgent0 proxy listening on http://localhost:{PROXY_PORT}\n"
        f"  cad-rag       -> RAG + {REAL_MODEL}\n"
        f"  ui-agent      -> UI system + {REAL_MODEL}\n"
        f"  code-agent    -> main specialist (full registry)\n"
        f"  web-creep     -> agentic web search/fetch loop\n"
        f"  browser-agent -> local Chrome/Edge cookies & storage reader\n"
        f"  deep-agent    -> deep reasoning [{LARGE_MODEL}]\n"
        f"  auto-agent    -> Smart Orchestrator (delegates to sub-agents)\n"
        f"  embed model    : mxbai-embed-large (Ollama)\n"
        f"  pipeline       : input preprocessing ACTIVE\n"
        f"  session store  : {sessions_dir}\n"
        f"  auto-shutdown  : proxy stops when UI tab closes."
    )
    log.info(banner)
    threading.Thread(target=_heartbeat_watchdog, daemon=True).start()
    server.serve_forever()

if __name__ == "__main__":
    main()
