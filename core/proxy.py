"""
RAG-aware + Agentic Ollama proxy  — port 11435
"""

import copy
import json
import mimetypes
import multiprocessing
import os
import socket
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

# ── Split-B re-exports ───────────────────────────────────────
# The virtual-model registry, request preprocessing, raw forwarding, Discord
# subprocess management, and server/runtime hardening were extracted into
# sibling ``core/proxy_*.py`` modules (behavior-preserving). They're re-imported
# here so (a) every symbol keeps its original ``core.proxy.<name>`` import path
# and (b) ProxyHandler / main() keep referencing them as bare module globals.
from core.proxy_virtual import (  # noqa: E402,F401  (re-export)
    REAL_MODEL, CAD_MODEL, UI_MODEL, CODE_MODEL, WEB_MODEL, BROWSER_MODEL,
    DEEP_MODEL, AUTO_MODEL, VISION_MODEL_V, RESEARCH_MODEL, VIRTUAL_MODELS,
    UI_SYSTEM, CODE_SYSTEM, BROWSER_SYSTEM, WEB_SYSTEM, _tags_with_virtual,
)
from core.proxy_forward import _forward  # noqa: E402,F401  (re-export)
from core.proxy_prep import (  # noqa: E402,F401  (re-export)
    _ensure_budget, _inject_system, _session_id_from_request, _last_user_msg,
)
from core.proxy_discord import (  # noqa: E402,F401  (re-export)
    _start_discord, _stop_discord, _discord_status,
)
from core.proxy_runtime import (  # noqa: E402,F401  (re-export)
    ExclusiveThreadingHTTPServer, _assert_healthy_environment,
)

def _init_orchestrator():
    FILE_TOOLS, WEB_TOOLS = _get_tools()
    all_tools = {**FILE_TOOLS, **WEB_TOOLS}
    return AgentOrchestrator(all_tools)

orchestrator = _init_orchestrator()

# ── Discord subprocess management ────────────────────────────
# Moved to core/proxy_discord.py (Split B); re-imported above. The
# ``_discord_proc`` global is owned by that module and mutated only through
# _start_discord / _stop_discord / _discord_status.

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

    def _serve_data_file(self, subdir, allowed_exts=None):
        """Serve one file from data/<subdir>/ with path-traversal protection.

        Rejects absolute paths, drive letters, and ``..`` segments, then
        confirms the resolved path still lives inside data/<subdir>/. When
        ``allowed_exts`` is given (a set of lowercase suffixes like
        ``{".html"}``), any other extension is refused. Sends the complete
        HTTP response (status + body) itself.
        """
        rel = self.path[len("/data/"):]
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
        allowed_root = (data_root / subdir).resolve()
        abs_path = (data_root / decoded).resolve()
        # Defense in depth: even after normalization, confirm the resolved
        # path is inside data/<subdir>/.
        try:
            abs_path.relative_to(allowed_root)
        except ValueError:
            self.send_response(403); self.end_headers(); return
        if allowed_exts is not None and abs_path.suffix.lower() not in allowed_exts:
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

        # ── /data/<subtree>/ static routes (path-traversal guarded) ───
        # generated/ → images from the `generate_image` tool so the chat UI
        #   can render them inline via markdown <img> (any file type).
        # research/  → Deep-Research reports (self-contained HTML + their .md
        #   siblings only) so the Research tab can open them.
        # Every OTHER /data/* subtree (sessions, uploads, skills, …) is
        # explicitly NEVER served.
        if self.path.startswith("/data/generated/"):
            self._serve_data_file("generated")
            return
        if self.path.startswith("/data/research/"):
            self._serve_data_file("research", allowed_exts={".html", ".htm", ".md", ".txt"})
            return
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

        if self.path.startswith("/api/compare/history"):
            # Recent blind comparisons (revealed — they're already voted/past).
            from urllib.parse import urlparse, parse_qs
            from core.compare import get_compare_store
            qs = parse_qs(urlparse(self.path).query)
            limit = 50
            try:
                if qs.get("limit"):
                    limit = max(1, min(200, int(qs["limit"][0])))
            except (TypeError, ValueError):
                limit = 50
            items = [c.summary_dict() for c in get_compare_store().list(limit=limit)]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"comparisons": items}).encode())
            return

        if self.path == "/api/skills" or self.path.startswith("/api/skills?"):
            # List learned skills (SkillStore) for the Skills tab — most-used
            # first, then alphabetical. Read-only; delete is a separate POST.
            from core.skills import get_skills_store
            try:
                skills = sorted(
                    (s.to_dict() for s in get_skills_store().load_all()),
                    key=lambda d: (-int(d.get("uses") or 0), str(d.get("name") or "")),
                )
            except Exception as e:  # noqa: BLE001
                log.warning(f"skills list failed: {e}")
                skills = []
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"skills": skills}).encode())
            return

        if self.path == "/api/research" or self.path.startswith("/api/research?"):
            # List saved Deep-Research reports for the Research tab. Each entry
            # links to the self-contained HTML (served via /data/research/).
            import html as _html
            import re as _re
            from core.deep_research import RESEARCH_DIR
            reports = []
            try:
                root = Path(RESEARCH_DIR)
                paths = sorted(root.glob("*.html"),
                               key=lambda p: p.stat().st_mtime, reverse=True)
                for p in paths[:100]:
                    try:
                        head = p.read_text(encoding="utf-8", errors="replace")[:2000]
                    except OSError:
                        head = ""
                    m = _re.search(r"<title>(.*?)</title>", head, _re.IGNORECASE | _re.DOTALL)
                    title = (_html.unescape(m.group(1)).strip() if m else "") or p.stem
                    st = p.stat()
                    md = p.with_suffix(".md")
                    reports.append({
                        "file": p.name,
                        "url": f"/data/research/{p.name}",
                        "title": title,
                        "ts": st.st_mtime,
                        "size": st.st_size,
                        "md_url": (f"/data/research/{md.name}" if md.exists() else None),
                    })
            except Exception as e:  # noqa: BLE001
                log.warning(f"research list failed: {e}")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"reports": reports}).encode())
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

        if path == "/api/compare/run":
            # Blind side-by-side: run one prompt against N models and return the
            # answers in a shuffled (model-anonymised) order. Identities stay
            # hidden until the user votes (/api/compare/vote).
            try:
                from core.compare import run_comparison
                prompt = (payload.get("prompt") or "").strip()
                models = payload.get("models") or []
                if not isinstance(models, list):
                    models = []
                comp = run_comparison(prompt, models)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(comp.public_dict(reveal=False)).encode())
            except ValueError as e:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            except Exception as e:  # noqa: BLE001
                log.error(f"compare run failed: {e}")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return

        if path == "/api/compare/vote":
            # Map the chosen blind slot back to its real model, record the
            # winner, and return the fully-revealed comparison.
            try:
                from core.compare import get_compare_store
                cid = (payload.get("id") or "").strip()
                slot = int(payload.get("slot"))
                comp = get_compare_store().set_winner(cid, slot)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps(comp.public_dict(reveal=True)).encode())
            except KeyError as e:
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            except (ValueError, TypeError) as e:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return

        if path == "/api/skills/delete":
            # Remove a learned skill by slug (Skills tab delete button).
            from core.skills import get_skills_store
            name = (payload.get("name") or "").strip()
            ok = False
            if name:
                try:
                    ok = get_skills_store().delete(name)
                except Exception as e:  # noqa: BLE001
                    log.warning(f"skill delete failed: {e}")
            self.send_response(200 if ok else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": ok}).encode())
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

        if model == RESEARCH_MODEL and is_chat:
            # v5: research-agent routes through the `researcher` specialist,
            # which has the deep_research tool + web/RAG tools.
            messages = _ensure_budget(payload.get("messages", []), session_id=sid)
            messages = pipeline.prepare(messages, base_system="")
            user_q = _last_user_msg(messages)
            self._run_agent_response(messages, {}, specialist="researcher", user_q=user_q, session_id=sid)
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

        # Defense in depth: a virtual model should have been handled by one of
        # the branches above. If it reaches here, the orchestrator failed to
        # initialize (e.g. missing deps / wrong Python) — forwarding it to
        # Ollama would yield a confusing "model not found" 404. Return a clear
        # error instead so the cause is obvious.
        if is_chat and model in VIRTUAL_MODELS:
            log.error(f"virtual model '{model}' reached forward path — orchestrator unavailable")
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({
                "error": (
                    f"Virtual model '{model}' could not be served — the agent "
                    f"orchestrator is unavailable. This usually means the proxy "
                    f"is running under the wrong Python (missing dependencies). "
                    f"Restart with the venv: .\\start_theagent0.bat"
                )
            }).encode())
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

# ── Server hardening + environment guard ─────────────────────
# ExclusiveThreadingHTTPServer and _assert_healthy_environment moved to
# core/proxy_runtime.py (Split B); re-imported above. main() below references
# them as module globals.


def main():
    global _server_ref
    # Guard the interpreter FIRST: a broken/system Python that can't serve the
    # orchestrator is the historical cause of intermittent virtual-model 404s.
    _assert_healthy_environment()
    # Enable session persistence so a restarted proxy still recognises
    # in-flight conversations from the same day. JSONL is best-effort —
    # request handling never blocks on disk IO.
    sessions_dir = Path(__file__).parent.parent / "data" / "sessions"
    try:
        get_store().enable_jsonl_persistence(sessions_dir)
    except Exception as e:
        log.warning(f"session-store persistence disabled: {e}")

    # Preflight: refuse to start if another proxy already owns the port.
    # On Windows, HTTPServer sets SO_REUSEADDR, so a second `python -m
    # core.proxy` (e.g. accidentally launched with system Python instead of
    # the venv) can silently CO-BIND port 11435. The OS then splits incoming
    # connections between the two processes — and if one has a broken
    # environment (missing deps → no orchestrator), virtual models like
    # `auto-agent` get forwarded to Ollama and return an intermittent
    # "model not found" 404. Fail fast instead.
    import socket as _socket
    _probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    _probe.settimeout(1.0)
    try:
        if _probe.connect_ex(("127.0.0.1", PROXY_PORT)) == 0:
            print(
                f"\n[proxy] ERROR: something is already listening on "
                f"127.0.0.1:{PROXY_PORT}.\n"
                f"        Another TheAgent0 proxy is probably running (check for a\n"
                f"        stray `python -m core.proxy`, possibly under SYSTEM Python\n"
                f"        instead of the venv). Stop it first, then relaunch:\n"
                f"          Windows:  taskkill /F /IM python.exe   (or close that window)\n"
                f"          then:     .\\start_theagent0.bat\n",
                flush=True,
            )
            raise SystemExit(2)
    finally:
        _probe.close()

    # Exclusive bind is the real guarantee against two proxies co-binding the
    # port (the connect_ex preflight above is only a friendly early check and
    # has an inherent TOCTOU race). If a second proxy races us to the port, the
    # bind raises OSError here — turn that into a clear message, not a traceback.
    try:
        server = ExclusiveThreadingHTTPServer(("localhost", PROXY_PORT), ProxyHandler)
    except OSError as e:
        print(
            f"\n[proxy] ERROR: cannot bind 127.0.0.1:{PROXY_PORT} ({e}).\n"
            f"        Another TheAgent0 proxy already owns this port (exclusive\n"
            f"        bind). Stop it first, then relaunch:\n"
            f"          Windows:  taskkill /F /IM python.exe   (or close that window)\n"
            f"          then:     .\\start_theagent0.bat\n",
            flush=True,
        )
        raise SystemExit(2)
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
        f"  research-agent-> multi-round web research + visual HTML report\n"
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
