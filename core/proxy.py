"""
RAG-aware + Agentic Ollama proxy  — port 11435
"""

import json
import multiprocessing
import sys
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

if __name__ == "__main__":
    multiprocessing.freeze_support()

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import OLLAMA_BASE, CHAT_MODEL, LARGE_MODEL, PROXY_PORT, AGENT_PROFILES
from core.orchestrator import AgentOrchestrator

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
    new_sys = f"{CAD_SYSTEM}\n\n=== RETRIEVED CONTEXT ===\n{ctx}\n=== END CONTEXT ==="
    out, has_sys = [], False
    for msg in messages:
        if msg.get("role") == "system":
            out.append({"role": "system", "content": new_sys})
            has_sys = True
        else:
            out.append(msg)
    if not has_sys: out.insert(0, {"role": "system", "content": new_sys})
    return out

def _inject_system(messages: list, system: str) -> list:
    has_sys = any(m.get("role") == "system" for m in messages)
    if has_sys: return messages
    return [{"role": "system", "content": system}] + list(messages)

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
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "discord": DISCORD_SETTINGS,
                "agents": AGENT_PROFILES,
                "tasks": AUTOMATION_TASKS,
                "providers": MODEL_PROVIDERS
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

        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}

        if path == "/api/config":
            try:
                print(f"[config] Updating settings: {json.dumps(payload)}")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"status": "success"}).encode())
                return
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
                return

        model = payload.get("model", "")

        if model == AUTO_MODEL and is_chat:
            messages = payload.get("messages", [])
            user_q = _last_user_msg(messages)
            print(f"[orchestrator] handling request: {user_q!r}", flush=True)
            final_answer = orchestrator.handle_request(user_q, conversation=messages)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({
                "model": REAL_MODEL,
                "message": {"role": "assistant", "content": final_answer},
                "done": True
            }).encode())
            return

        if model == CAD_MODEL and is_chat:
            messages = payload.get("messages", [])
            user_q   = _last_user_msg(messages)
            payload["messages"] = _inject_rag(messages, user_q)
            payload["model"]    = REAL_MODEL
            raw = json.dumps(payload).encode()
            self._forward_and_reply(path, raw)
            return

        if model == UI_MODEL and is_chat:
            payload["messages"] = _inject_system(payload.get("messages", []), UI_SYSTEM)
            payload["model"]    = REAL_MODEL
            raw = json.dumps(payload).encode()
            self._forward_and_reply(path, raw)
            return

        if model == CODE_MODEL and is_chat:
            FILE_TOOLS, WEB_TOOLS = _get_tools()
            all_tools = {**FILE_TOOLS, **WEB_TOOLS}
            messages = _inject_system(payload.get("messages", []), CODE_SYSTEM)
            self._run_agent_response(messages, all_tools)
            return

        if model == WEB_MODEL and is_chat:
            _, WEB_TOOLS = _get_tools()
            messages = _inject_system(payload.get("messages", []), WEB_SYSTEM)
            self._run_agent_response(messages, WEB_TOOLS)
            return

        if model == BROWSER_MODEL and is_chat:
            BROWSER_TOOLS = _get_browser_tools()
            messages = _inject_system(payload.get("messages", []), BROWSER_SYSTEM)
            self._run_agent_response(messages, BROWSER_TOOLS)
            return

        if model == DEEP_MODEL and is_chat:
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

    def _run_agent_response(self, messages: list, tools: dict):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        wfile = self.wfile
        def emit(text: str):
            chunk = json.dumps({
                "model":      REAL_MODEL,
                "message":    {"role": "assistant", "content": text},
                "done":       False,
            }) + "\n"
            try:
                wfile.write(chunk.encode())
                wfile.flush()
            except Exception:
                pass
        try:
            run_agent = _get_agent()
            final = run_agent(messages, tools, stream_cb=emit)
        except Exception as e:
            final = f"ERROR in agent loop: {e}"
        done_chunk = json.dumps({
            "model":              REAL_MODEL,
            "message":            {"role": "assistant", "content": final},
            "done":               True,
            "done_reason":        "stop",
            "total_duration":     0,
            "prompt_eval_count":  0,
            "eval_count":         0,
        }) + "\n"
        try:
            wfile.write(done_chunk.encode())
            wfile.flush()
        except Exception:
            pass

def main():
    server = HTTPServer(("localhost", PROXY_PORT), ProxyHandler)
    print(f"[rag-proxy] http://localhost:{PROXY_PORT}", flush=True)
    print(f"  Embed model   : mxbai-embed-large (Ollama)", flush=True)
    print(f"  cad-rag       -> RAG + {REAL_MODEL}", flush=True)
    print(f"  ui-agent      -> UI system + {REAL_MODEL}", flush=True)
    print(f"  code-agent    -> agentic file/cmd/web loop [{REAL_MODEL}]", flush=True)
    print(f"  web-creep     -> agentic web search/fetch loop [{REAL_MODEL}]", flush=True)
    print(f"  browser-agent -> local Chrome/Edge cookies & storage reader", flush=True)
    print(f"  deep-agent    -> deep reasoning [{LARGE_MODEL}]", flush=True)
    print(f"  auto-agent    -> Smart Orchestrator (delegates to sub-agents)", flush=True)
    server.serve_forever()

if __name__ == "__main__":
    main()
