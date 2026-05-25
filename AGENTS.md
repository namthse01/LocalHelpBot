# Agents catalog

LocalHelpBot exposes **virtual models** through the proxy (`core/proxy.py`). They look like normal Ollama models to any client (the Web UI dropdown, VS Code Continue, the Discord bridge, MCP servers) but each routes the request through different middleware: orchestrator, RAG injection, browser-data reader, etc.

Pick by what the task needs, not by what sounds cool. The router (`auto-agent`) is usually the right default.

---

## Routing matrix

| Virtual model     | Picks model           | Routes through               | Best for                                                                 | Avoid for                          |
|-------------------|-----------------------|------------------------------|--------------------------------------------------------------------------|------------------------------------|
| `auto-agent`      | `CHAT_MODEL`          | Orchestrator (`main` profile)| Default for everything. Will delegate to sub-agents via `task` tool.     | When you need a specific specialist forced |
| `code-agent`      | `CHAT_MODEL`          | Orchestrator (`main` profile)| Same as `auto-agent` — full tool registry (incl. PDF/DOCX/install_package). Kept for back-compat. | n/a |
| `cad-rag`         | `CHAT_MODEL`          | Forward + RAG injection      | Single-shot questions against the indexed CAD/AutoCAD corpus.            | Multi-step tasks; use `auto-agent` |
| `ui-agent`        | `CHAT_MODEL`          | Forward + UI system prompt   | Frontend / UI advice with a tuned system prompt.                         | File-writing tasks (no tools)      |
| `web-creep`       | `CHAT_MODEL`          | Agent loop + WEB_TOOLS       | Multi-source web research without filesystem access.                     | Anything that needs to write files |
| `browser-agent`   | `CHAT_MODEL`          | Agent loop + BROWSER_TOOLS   | Reading local Chrome/Edge cookies, history, storage.                     | General chat                       |
| `deep-agent`      | `LARGE_MODEL`         | Forward (no tools)           | Heavy reasoning when you have a bigger model installed.                  | Anything tool-driven (no agent loop) |

---

## When to use which

### `auto-agent` *(default — start here)*
The smart router. Routes through the orchestrator's `main` profile, which has the full tool registry (file IO, web, exec, PDF/DOCX, install_package, `task` delegation). It plans, explores, acts, verifies, reports. Use it for almost anything.

**Example prompt:**
> *"Summarize each Markdown file in `docs/ai/` into a one-paragraph entry, save the combined list to `out/overview.md`."*

### `code-agent`
Identical to `auto-agent` under the hood (also routes to the `main` specialist). Kept as a named alias because Continue and other clients hardcoded it. No functional difference today.

### `cad-rag`
Pre-injects the top RAG chunks into the system prompt before forwarding to Ollama. Useful for one-shot Q&A against your `cad_db/` corpus. **Note**: in v3 the agent can also call `query_rag` as a tool from `auto-agent`/`main`, which is generally smarter — `cad-rag` exists for the simple ask-and-answer case.

**Example prompt (against indexed corpus):**
> *"What's the keyboard shortcut for trimming geometry in AutoCAD 2024?"*

**Gotcha:** the score threshold is `0.3` (set in `core/proxy.py:SCORING_THRESHOLD`). If nothing matches above that, you get `NO_DATA` and the model is told to admit it.

### `ui-agent`
Replaces the system prompt with `UI_SYSTEM` and forwards to Ollama. No tools — purely a system-prompt swap for UI/frontend questions. Replace the placeholder string in `core/proxy.py:UI_SYSTEM` with your own canonical UI guidance.

### `web-creep`
Agentic loop with `WEB_TOOLS` (= `search_web`, `fetch_url`, plus `read_file` for local context). No write access — research-only.

**Example prompt:**
> *"Compare the licence terms of the top 5 Python LLM-evaluation libraries. Cite each source URL."*

### `browser-agent`
Reads local Chrome/Edge cookies and storage via `BROWSER_TOOLS` (defined in `core/browser.py`). Use for "what's in my Chrome bookmarks?" style questions. Permission-gated where appropriate.

### `deep-agent`
Forwards directly to `LARGE_MODEL` (e.g. `glm-4.7-flash:latest`) — no agent loop, no tools. Use when you want raw reasoning power on a question and don't need tool execution. If `LARGE_MODEL` is empty in `config.py`, this model falls back to `CHAT_MODEL`.

---

## Profiles (the underlying configuration)

Virtual models pick from these per-specialist profiles in `config.py:AGENT_PROFILES`:

| Profile      | Tools available                       | `verify` mode | Notes |
|--------------|---------------------------------------|---------------|-------|
| `main`       | Full registry (24 tools incl. plugins) | `off`         | Default. Set `"verify": "high"` to enable CoVe on write-heavy tasks. |
| `researcher` | `read_file`, `list_dir`, `search_web`, `fetch_url`, `query_rag` | `off` | Synthesises across multiple sources. |
| `coder`      | `read_file`, `list_dir`, `search_web` | `off`         | Read-only code analysis. Won't edit files. |
| `summarizer` | none                                  | `off`         | Pure prose compression. Used by Discord output. |

### Enabling Chain-of-Verification

Edit `config.py` → set `"verify": "high"` on the profile you want. Next time the agent finishes a response, its message will contain three sections:

```
## Draft
…initial answer…
## Verification
1. Q: … → A: …
2. Q: … → A: …
3. Q: … → A: …
## Final
…corrected answer…
```

The final block is the answer; the rest is the audit trail. Enable selectively — it adds tokens and latency, so it's worth it for write tasks but overkill for chitchat.

---

## Cross-references

- `core/orchestrator.py` — how profiles are filtered to per-call tool registries; `task` sub-agent delegation; CoVe addendum injection.
- `core/proxy.py` — virtual-model routing and the legacy forward paths (`cad-rag`, `ui-agent`, `deep-agent`).
- `core/tool_schema.py` — `Tool` / `ToolResult` / `ErrorCode` definitions; `build_default_registry()` for the canonical tool list.
- `core/agent.py` — the loop (`run_agent`), `STOP_THE_LINE_MAX_RETRIES`, `MAX_TURNS`, parse-retry self-heal.
- `docs/ai/testing/troubleshooting.md` — failure modes per profile.
