"""
RAG-aware + Agentic Ollama proxy  — port 11435
"""

import copy
import json
import multiprocessing
import subprocess
import sys
import threading
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

def _get_rag():
    from core.query import query_rag
    return query_rag

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

VIRTUAL_MODELS = [CAD_MODEL, UI_MODEL, CODE_MODEL, WEB_MODEL, BROWSER_MODEL, DEEP_MODEL, AUTO_MODEL]
SCORING_THRESHOLD = 0.3

CAD_SYSTEM = "You are a CAD/AutoCAD specialist agent..."
UI_SYSTEM = "You are a UI/Frontend specialist agent..."
CODE_SYSTEM = "You are an autonomous coding agent..."
BROWSER_SYSTEM = "You are browser-agent, a local browser data reader..."
WEB_SYSTEM = "You are web-creep, an autonomous web research agent..."

def _ensure_budget(messages: list) -> list:
    """Server-side compaction safety net.

    If the incoming message list is over the soft token budget, fold the
    oldest turns into a summary system message and keep the most recent
    turns verbatim. Runs BEFORE pipeline/orchestrator rebuild the system
    prompt (our memory helpers then carry the summary forward).

    No-op if already under budget or if summarization fails — in both cases
    we return the original list so downstream routing still works.
    """
    try:
        if not messages:
            return messages
        tokens = mem.estimate_tokens(messages)
        if tokens <= mem.DEFAULT_MAX_TOKENS:
            return messages
        from core.providers import smart_provider as _sp
        system, to_summarize, keep = mem.split_for_compaction(
            messages, keep_recent_turns=mem.DEFAULT_KEEP_RECENT_TURNS
        )
        if not to_summarize:
            return messages
        # Include any prior summary from existing system messages so the
        # summarizer can extend-not-replace.
        prior = mem.extract_summary(system)
        prompt_msgs = mem.build_summarizer_messages(to_summarize)
        if prior:
            prompt_msgs[-1]["content"] = (
                f"Prior summary (extend this, do not discard facts):\n{prior}\n\n"
                + prompt_msgs[-1]["content"]
            )
        print(f"[compact] server-side safety-net firing — "
              f"tokens~{tokens} > {mem.DEFAULT_MAX_TOKENS}, "
              f"summarizing {len(to_summarize)} old turns, keeping {len(keep)}", flush=True)
        resp = _sp.chat(prompt_msgs, options={"temperature": 0.1, "num_predict": 1024})
        summary = mem.wrap_summary_output(resp.content or "")
        if not summary:
            return messages
        # Rebuild: non-summary system msgs, then summary system, then kept turns
        non_summary_system = [m for m in system
                              if not mem._looks_like_summary(mem._content_text(m))]
        summary_msg = {
            "role": "system",
            "content": f"{mem.SUMMARY_MARKER}\n{summary}\n{mem.SUMMARY_END}",
        }
        return non_summary_system + [summary_msg] + keep
    except Exception as e:
        print(f"[compact] safety-net failed (non-fatal): {e}", flush=True)
        return messages


def _rag_context(query: str) -> str:
    try:
        query_rag = _get_rag()
        resp  = query_rag(query, n_results=3)
        docs  = resp.get("documents", [[]])[0]
        metas = resp.get("metadatas", [[]])[0]
        dists = resp.get("distances", [[]])[0]
        if not docs: return "NO_DATA"
        lines, good = [], False
        for doc, meta, dist in zip(docs, metas, dists):
            score = 1 - dist
            if score >= SCORING_THRESHOLD:
                good = True
                lines.append(f"[score={score:.3f} | {meta.get('file_name','')}]\n{doc.strip()}")
        return "\n\n---\n".join(lines) if good else "NO_DATA"
    except Exception as e:
        return f"NO_DATA (error: {e})"

def _inject_rag(messages: list, user_q: str) -> list:
    ctx = _rag_context(user_q)
    # Preserve any carried conversation summary so long CAD chats don't lose
    # context when we overwrite the system prompt with the RAG block.
    carried = mem.extract_summary(messages)
    base = f"{CAD_SYSTEM}\n\n=== RETRIEVED CONTEXT ===\n{ctx}\n=== END CONTEXT ==="
    new_sys = mem.merge_summary(base, carried)
    # Drop ALL existing system messages (we folded the summary into new_sys).
    out = [{"role": "system", "content": new_sys}]
    for msg in messages:
        if msg.get("role") == "system":
            continue
        out.append(msg)
    return out

def _inject_system(messages: list, system: str) -> list:
    # Always preserve any carried summary, even when the client already sent
    # a system message. We merge summary → new system and drop summary-only
    # system messages to avoid duplicating the block.
    carried = mem.extract_summary(messages)
    non_summary_system = [
        m for m in messages
        if m.get("role") == "system" and not mem._looks_like_summary(mem._content_text(m))
    ]
    if non_summary_system and not carried:
        # Nothing to merge — keep the caller's system as-is.
        return messages
    merged = mem.merge_summary(system, carried)
    out = [{"role": "system", "content": merged}]
    for m in messages:
        if m.get("role") == "system":
            # If it's a non-summary system the caller sent, fold it in too.
            if not mem._looks_like_summary(mem._content_text(m)):
                out[0]["content"] = out[0]["content"] + "\n\n" + mem._content_text(m)
            continue
        out.append(m)
    return out

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
    print("[shutdown] All services stopped. Exiting.", flush=True)
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
            print(f"[heartbeat] UI idle for {int(idle)}s (>{HEARTBEAT_TIMEOUT}s) "
                  f"and no in-flight work — shutting down.", flush=True)
            _shutdown_all()
            break

class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[proxy] {fmt % args}", flush=True)

    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            try:
                with open("HelpBotUI/index.html", "rb") as f:
                    self.wfile.write(f.read())
            except Exception as e:
                self.wfile.write(f"Error loading index.html: {e}".encode())
            return

        if self.path.startswith("/static/"):
            file_path = self.path[len("/static/"):]
            try:
                with open(f"HelpBotUI/{file_path}", "rb") as f:
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

        if self.path == "/api/stats":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"recent": _RECENT_STATS}).encode())
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
                        print(f"[config] smart_provider reload failed: {e}")

                print(f"[config] Applied & persisted: {applied}")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "success", "applied": applied}).encode())
                return
            except Exception as e:
                print(f"[config] ERROR: {e}")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
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
            print(f"[discord] {msg}", flush=True)
            return

        if path == "/api/discord/disconnect":
            ok, msg = _stop_discord()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"success": ok, "message": msg, "connected": _discord_status()}).encode())
            print(f"[discord] {msg}", flush=True)
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
                print("[ui-closing] UI signaled close — 15s grace window started.", flush=True)
            else:
                print("[ui-closing] UI closing but work in flight — ignored.", flush=True)
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

        if model == AUTO_MODEL and is_chat:
            messages = _ensure_budget(payload.get("messages", []))
            # Enrich with extracted facts (paths, filenames, verbs) + output
            # contract. Orchestrator owns the role prompt, so base_system="".
            messages = pipeline.prepare(messages, base_system="")
            user_q = _last_user_msg(messages)
            print(f"[orchestrator] handling request: {user_q!r}", flush=True)
            self._run_agent_response(messages, {}, orchestrator_mode=True, user_q=user_q)
            return

        if model == CAD_MODEL and is_chat:
            messages = _ensure_budget(payload.get("messages", []))
            user_q   = _last_user_msg(messages)
            payload["messages"] = _inject_rag(messages, user_q)
            payload["model"]    = REAL_MODEL
            raw = json.dumps(payload).encode()
            self._forward_and_reply(path, raw)
            return

        if model == UI_MODEL and is_chat:
            messages = _ensure_budget(payload.get("messages", []))
            payload["messages"] = _inject_system(messages, UI_SYSTEM)
            payload["model"]    = REAL_MODEL
            raw = json.dumps(payload).encode()
            self._forward_and_reply(path, raw)
            return

        if model == CODE_MODEL and is_chat:
            FILE_TOOLS, WEB_TOOLS = _get_tools()
            all_tools = {**FILE_TOOLS, **WEB_TOOLS}
            messages = _ensure_budget(payload.get("messages", []))
            messages = pipeline.prepare(messages, CODE_SYSTEM)
            self._run_agent_response(messages, all_tools)
            return

        if model == WEB_MODEL and is_chat:
            _, WEB_TOOLS = _get_tools()
            messages = _ensure_budget(payload.get("messages", []))
            messages = pipeline.prepare(messages, WEB_SYSTEM)
            self._run_agent_response(messages, WEB_TOOLS)
            return

        if model == BROWSER_MODEL and is_chat:
            BROWSER_TOOLS = _get_browser_tools()
            messages = _ensure_budget(payload.get("messages", []))
            messages = pipeline.prepare(messages, BROWSER_SYSTEM)
            self._run_agent_response(messages, BROWSER_TOOLS)
            return

        if model == DEEP_MODEL and is_chat:
            # Deep model gets its own compaction too — it's the most likely
            # to have a long context buildup since users pick it for complex
            # multi-turn discussions.
            payload["messages"] = _ensure_budget(payload.get("messages", []))
            payload["model"] = LARGE_MODEL
            raw = json.dumps(payload).encode()
            self._forward_and_reply(path, raw)
            return

        headers = dict(self.headers)
        status, resp_body, ct = _forward(path, raw, headers)
        self.send_response(status)
        self.send_header("Content-Type", ct)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)

    def _forward_and_reply(self, path: str, raw: bytes):
        headers = dict(self.headers)
        status, resp_body, ct = _forward(path, raw, headers)
        self.send_response(status)
        self.send_header("Content-Type", ct)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)

    def _stream_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
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

    def _run_agent_response(self, messages: list, tools: dict, orchestrator_mode: bool = False, user_q: str = ""):
        import time as _t
        self._stream_headers()
        wfile = self.wfile
        emit = self._event_emitter(wfile)
        t0 = _t.perf_counter()
        _inflight_inc()
        try:
            if orchestrator_mode:
                final = orchestrator.handle_request(user_q, conversation=messages, stream_cb=emit)
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
        if orchestrator_mode:
            _record_stat(user_q, total_ms, active, last_timing)
        done_chunk = json.dumps({
            "model":          REAL_MODEL,
            "message":        {"role": "assistant", "content": final},
            "agent_event":    {"type": "done", "total_ms": total_ms, "active": active},
            "done":           True,
            "done_reason":    "stop",
            "total_duration": 0,
        }) + "\n"
        try:
            wfile.write(done_chunk.encode())
            wfile.flush()
        except Exception:
            pass

def main():
    global _server_ref
    server = ThreadingHTTPServer(("localhost", PROXY_PORT), ProxyHandler)
    server.daemon_threads = True
    _server_ref = server
    print(f"[rag-proxy] http://localhost:{PROXY_PORT}", flush=True)
    print(f"  Embed model   : mxbai-embed-large (Ollama)", flush=True)
    print(f"  cad-rag       -> RAG + {REAL_MODEL}", flush=True)
    print(f"  ui-agent      -> UI system + {REAL_MODEL}", flush=True)
    print(f"  code-agent    -> agentic file/cmd/web loop [{REAL_MODEL}]", flush=True)
    print(f"  web-creep     -> agentic web search/fetch loop [{REAL_MODEL}]", flush=True)
    print(f"  browser-agent -> local Chrome/Edge cookies & storage reader", flush=True)
    print(f"  deep-agent    -> deep reasoning [{LARGE_MODEL}]", flush=True)
    print(f"  auto-agent    -> Smart Orchestrator (delegates to sub-agents)", flush=True)
    print(f"  [pipeline]    input preprocessing ACTIVE (normalize + extract + assemble)", flush=True)
    print(f"  [auto-shutdown] Proxy will stop when UI tab is closed.", flush=True)
    threading.Thread(target=_heartbeat_watchdog, daemon=True).start()
    server.serve_forever()

if __name__ == "__main__":
    main()
