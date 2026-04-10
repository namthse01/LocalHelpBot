"""
RAG-aware + Agentic Ollama proxy  — port 11435

Virtual models:
  cad-rag     → auto-query RAG, inject context, answer with qwen2.5-coder:7b
  ui-agent    → UI/Frontend specialist, pass-through
  code-agent  → agentic loop: read/write files, run commands, fix bugs
  web-creep   → agentic loop: search web, fetch URLs, read content

All other models pass through to Ollama :11434 unchanged.
"""

import json
import multiprocessing
import sys
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# MUST be first — prevents Windows multiprocessing from re-spawning proxy on torch import
if __name__ == "__main__":
    multiprocessing.freeze_support()

sys.path.insert(0, str(Path(__file__).parent))

# Lazy imports — do NOT import at module level to avoid multiprocessing re-spawn
def _get_rag():
    from rag_query import query_rag
    return query_rag

def _get_tools():
    from tools import FILE_TOOLS, WEB_TOOLS
    return FILE_TOOLS, WEB_TOOLS

def _get_browser_tools():
    from browser_tools import BROWSER_TOOLS
    return BROWSER_TOOLS

def _get_agent():
    from agent_loop import run_agent
    return run_agent

OLLAMA_BASE = "http://localhost:11434"
PROXY_PORT  = 11435
REAL_MODEL  = "qwen2.5-coder:7b"

# ── Virtual model names ───────────────────
CAD_MODEL     = "cad-rag"
UI_MODEL      = "ui-agent"
CODE_MODEL    = "code-agent"
WEB_MODEL     = "web-creep"
BROWSER_MODEL = "browser-agent"

VIRTUAL_MODELS = [CAD_MODEL, UI_MODEL, CODE_MODEL, WEB_MODEL, BROWSER_MODEL]

SCORING_THRESHOLD = 0.3

# ── System prompts ────────────────────────

CAD_SYSTEM = """You are a CAD/AutoCAD specialist agent.
The context below (between ===) was retrieved from the local CAD knowledge base.

RULES:
1. Base your answer PRIMARILY on the retrieved context.
2. Cite the source file and score shown in the context.
3. If the context block says "NO_DATA", reply EXACTLY:
   "Không có dữ liệu trong knowledge base — có tìm trên mạng hay không?"
4. Never fabricate AutoCAD API details not found in the context.
5. Provide working C# code using AutoCAD .NET API when relevant."""

UI_SYSTEM = """You are a UI/Frontend specialist agent.
Focus on WinForms, WPF/XAML, MVVM, and web frontend (HTML/CSS/JS/TS/React/Vue/Angular).
Provide practical, code-oriented answers. Use C# for desktop, JS/TS for web."""

CODE_SYSTEM = """You are an autonomous coding agent. You can read/write files, run commands, search the web, and fix bugs end-to-end.

Available tools — call ONE at a time using this EXACT format on its own line:
ACTION: {"tool": "tool_name", "arg1": "value1", ...}

Tools:
  read_file    — {"tool":"read_file",   "path":"<path>"}
  write_file   — {"tool":"write_file",  "path":"<path>", "content":"<full file content>"}
  list_dir     — {"tool":"list_dir",    "path":"<path>"}
  run_command  — {"tool":"run_command", "command":"<cmd>", "cwd":"<optional working dir>"}
  grep_file    — {"tool":"grep_file",   "path":"<path>", "pattern":"<regex>"}
  search_web   — {"tool":"search_web",  "query":"<search query>"}
  fetch_url    — {"tool":"fetch_url",   "url":"<full URL>"}

Workflow:
1. Understand the task.
2. Explore with list_dir / read_file / grep_file to understand the code.
3. Implement the fix using write_file.
4. Verify with run_command (run the script / compile / lint / test).
5. If run_command fails (EXIT non-zero, or STDERR has errors):
   a. Read the error carefully — identify the root cause.
   b. Missing package/module → run_command to install it (pip install / npm install).
   c. Code bug → read_file the broken file, find the line, write_file with the fix.
   d. Unfamiliar error → search_web with the exact error text, fetch_url the top result.
   e. Retry the command after fixing. Repeat until it passes.
6. Keep recovering until the command succeeds OR you have truly exhausted all options.
7. Final answer: write a clear summary WITHOUT any ACTION lines.

KEY RULES:
- NEVER give up after the first error. Always attempt at least one recovery action.
- NEVER guess file contents — always read_file first.
- NEVER put ACTION inside a code block or prose — it must be a plain line.
- If you are not sure where a file is, use list_dir first."""

BROWSER_SYSTEM = """You are browser-agent, a local browser data reader.
You can read cookies, LocalStorage, and session tabs from Chrome/Edge/Brave on this machine.

Available tools — call ONE at a time using this EXACT format on its own line:
ACTION: {"tool": "tool_name", "arg1": "value1"}

Tools:
  list_browser_profiles — {"tool":"list_browser_profiles", "browser":"chrome"}
     → List all profiles (Default, Profile 1, …) and cookie counts

  read_browser_cookies  — {"tool":"read_browser_cookies", "browser":"chrome", "profile":"Default", "domain":"<optional filter>", "show_values":true, "limit":100}
     → Read cookies. Values are decrypted (AES-GCM). Use domain filter to narrow down.

  read_browser_storage  — {"tool":"read_browser_storage", "browser":"chrome", "profile":"Default", "origin":"<optional filter>"}
     → Read LocalStorage key-value pairs (best-effort LevelDB extraction).

  read_browser_sessions — {"tool":"read_browser_sessions", "browser":"chrome", "profile":"Default"}
     → List URLs from the current or last browser session (recently open tabs).

Supported browsers: chrome, edge, brave.

Workflow:
1. If unsure which profile to use, call list_browser_profiles first.
2. Then call the relevant read tool, with a domain/origin filter if the user mentioned a specific site.
3. Present the results clearly — group cookies by domain, highlight session/auth cookies (those named token, session, auth, jwt, sid, csrftoken, etc.).
4. When done, give your final answer WITHOUT any ACTION lines.

NOTE: Cookie values for Chrome v80+ are AES-256-GCM encrypted and are automatically decrypted.
If Chrome is currently open, a temp-copy fallback is used so reading still works."""

WEB_SYSTEM = """You are web-creep, an autonomous web research agent.
You can search the web and read page content to answer questions.

Available tools — call ONE at a time using this EXACT format on its own line:
ACTION: {"tool": "tool_name", "arg1": "value1"}

Tools:
  search_web — args: {"tool":"search_web", "query":"<search query>"}
  fetch_url  — args: {"tool":"fetch_url",  "url":"<full URL>"}
  read_file  — args: {"tool":"read_file",  "path":"<local file path>"}

Workflow:
1. search_web to find relevant pages.
2. fetch_url on the most relevant result(s) to read the full content.
3. Synthesize and answer based on what you read.
4. Cite your sources (URLs) in the final answer.

Give your final answer WITHOUT any ACTION lines when done."""


# ── RAG helpers ───────────────────────────

def _rag_context(query: str) -> str:
    try:
        query_rag = _get_rag()
        resp  = query_rag(query, n_results=3)
        docs  = resp.get("documents", [[]])[0]
        metas = resp.get("metadatas", [[]])[0]
        dists = resp.get("distances", [[]])[0]
        if not docs:
            return "NO_DATA"
        lines, good = [], False
        for doc, meta, dist in zip(docs, metas, dists):
            score = 1 - dist
            if score >= SCORING_THRESHOLD:
                good = True
                lines.append(
                    f"[score={score:.3f} | {meta.get('file_name','')}]\n{doc.strip()}"
                )
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
    if not has_sys:
        out.insert(0, {"role": "system", "content": new_sys})
    return out


def _inject_system(messages: list, system: str) -> list:
    has_sys = any(m.get("role") == "system" for m in messages)
    if has_sys:
        return messages
    return [{"role": "system", "content": system}] + list(messages)


def _last_user_msg(messages: list) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            c = msg.get("content", "")
            if isinstance(c, list):
                return " ".join(p.get("text", "") for p in c if p.get("type") == "text")
            return str(c)
    return ""


# ── Ollama pass-through ───────────────────

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
        {
            "name": CAD_MODEL, "model": CAD_MODEL,
            "modified_at": "2026-01-01T00:00:00Z", "size": 0,
            "details": {"parameter_size": "7.6B", "family": "CAD/AutoCAD RAG agent"}
        },
        {
            "name": UI_MODEL, "model": UI_MODEL,
            "modified_at": "2026-01-01T00:00:00Z", "size": 0,
            "details": {"parameter_size": "7.6B", "family": "UI/Frontend agent"}
        },
        {
            "name": CODE_MODEL, "model": CODE_MODEL,
            "modified_at": "2026-01-01T00:00:00Z", "size": 0,
            "details": {"parameter_size": "7.6B", "family": "Agentic code agent"}
        },
        {
            "name": WEB_MODEL, "model": WEB_MODEL,
            "modified_at": "2026-01-01T00:00:00Z", "size": 0,
            "details": {"parameter_size": "7.6B", "family": "Web research agent"}
        },
        {
            "name": BROWSER_MODEL, "model": BROWSER_MODEL,
            "modified_at": "2026-01-01T00:00:00Z", "size": 0,
            "details": {"parameter_size": "7.6B", "family": "Browser cookies/storage reader"}
        },
    ]
    data["models"] = virtual + data.get("models", [])
    return json.dumps(data).encode()


# ── HTTP handler ──────────────────────────

class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[proxy] {fmt % args}", flush=True)

    # ── GET ──
    def do_GET(self):
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

    # ── OPTIONS ──
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    # ── POST ──
    def do_POST(self):
        length  = int(self.headers.get("Content-Length", 0))
        raw     = self.rfile.read(length)
        path    = self.path
        is_chat = path in ("/api/chat", "/v1/chat/completions")

        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}

        model = payload.get("model", "")

        # ── CAD agent ──
        if model == CAD_MODEL and is_chat:
            messages = payload.get("messages", [])
            user_q   = _last_user_msg(messages)
            print(f"[cad-rag] query: {user_q!r}", flush=True)
            payload["messages"] = _inject_rag(messages, user_q)
            payload["model"]    = REAL_MODEL
            raw = json.dumps(payload).encode()
            self._forward_and_reply(path, raw)
            return

        # ── UI agent ──
        if model == UI_MODEL and is_chat:
            payload["messages"] = _inject_system(payload.get("messages", []), UI_SYSTEM)
            payload["model"]    = REAL_MODEL
            raw = json.dumps(payload).encode()
            self._forward_and_reply(path, raw)
            return

        # ── Code agent (agentic loop) ──
        if model == CODE_MODEL and is_chat:
            FILE_TOOLS, WEB_TOOLS = _get_tools()
            all_tools = {**FILE_TOOLS, **WEB_TOOLS}   # code-agent gets web search too
            messages = _inject_system(payload.get("messages", []), CODE_SYSTEM)
            self._run_agent_response(messages, all_tools)
            return

        # ── Web-creep agent (agentic loop) ──
        if model == WEB_MODEL and is_chat:
            _, WEB_TOOLS = _get_tools()
            messages = _inject_system(payload.get("messages", []), WEB_SYSTEM)
            self._run_agent_response(messages, WEB_TOOLS)
            return

        # ── Browser agent (read local browser data) ──
        if model == BROWSER_MODEL and is_chat:
            BROWSER_TOOLS = _get_browser_tools()
            messages = _inject_system(payload.get("messages", []), BROWSER_SYSTEM)
            self._run_agent_response(messages, BROWSER_TOOLS)
            return

        # ── Pass-through ──
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
        """Run agentic loop, stream steps back as NDJSON (Ollama chat format)."""
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

        # Final done message
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


# ── Main ──────────────────────────────────

def main():
    server = HTTPServer(("localhost", PROXY_PORT), ProxyHandler)
    print(f"[rag-proxy] http://localhost:{PROXY_PORT}", flush=True)
    print(f"  cad-rag       -> RAG + {REAL_MODEL}", flush=True)
    print(f"  ui-agent      -> UI system + {REAL_MODEL}", flush=True)
    print(f"  code-agent    -> agentic file/cmd/web loop", flush=True)
    print(f"  web-creep     -> agentic web search/fetch loop", flush=True)
    print(f"  browser-agent -> local Chrome/Edge cookies & storage reader", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
