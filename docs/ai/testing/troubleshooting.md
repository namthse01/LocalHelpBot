---
phase: testing
title: Troubleshooting — top failure modes
description: Red-flag signs, root causes, and fixes for the most common LocalHelpBot issues. Borrows the "Stop-the-Line" diagnostic pattern from the How_AI_Learn skills collection.
---

# Troubleshooting

This catalogues the failure modes the v3 upgrade was designed to fix (and prevent). Each entry lists **red-flag signs** so you can recognise the problem fast, the **root cause**, and the **fix**.

Before opening an issue, run the healthcheck:

```powershell
.\venv\Scripts\python -m core.healthcheck
```

The UI shows the same banner at the top of the **Change Mode** tab.

---

## 1. Bot forgets the file it just wrote

**Red flags:**
- You ask the bot to write a file, then "now add a section to it" → bot says "which file?" or asks for the path again.
- Bot says *"I don't have context"* despite having written the file in the previous turn.
- Works for the first 2 turns then loses track.

**Root cause (pre-v3):** the proxy rebuilt the system prompt from scratch on every turn, dropping the carried summary alongside it. Every request was effectively turn-1 from the model's point of view.

**Fix (v3):** the context engine ([core/context.py](../../../core/context.py)) keeps a 4-tier system prompt; T2 (Session) carries the user's goal, files touched, and sticky decisions across turns. The proxy derives a stable `session_id` from a hash of the first user message + today's date (or from the `X-Session-Id` HTTP header if the client passes one).

**Verify it's working:**
1. Open `http://localhost:11435`.
2. Open DevTools → Network. Send a chat message.
3. The response should include header `X-Session-Id: <hex>`. Subsequent requests in the same conversation should hit the same id.
4. Files touched in turn N appear in T2 of turn N+1's system prompt — visible in the Logs tab under `subsystem=context`.

**If it still happens:** check `data/sessions/<sid>.jsonl` exists for your session. If not, the persistence layer is disabled — look for `[session-store] persistence disabled: …` in the proxy logs.

---

## 2. Tool loops on the same error

**Red flags:**
- Activity panel shows the same tool (`edit_file`, `write_file`, …) being retried 5+ times with identical args.
- Error code stays the same (e.g. `STALE_OLD_STRING`) across retries.
- Eventually hits `MAX_ERROR_STREAK` and gives up.

**Root cause (pre-v3):** errors were free-form strings; the agent loop couldn't tell a recoverable failure from a doomed one, so it kept trying.

**Fix (v3):** Stop-the-Line in [core/agent.py](../../../core/agent.py). Per-call retry counter keyed on `_args_hash(tool_name, args)`. After `STOP_THE_LINE_MAX_RETRIES = 2` consecutive failures of the SAME call, the third invocation is short-circuited with a synthetic `LOOP_BUDGET` ToolResult. The agent is forced to revise the plan or pick a different tool.

**Verify it's working:** in the Activity panel, after 2 failures of the same call you should see one `[LOOP_BUDGET]` tool_result, then the agent's revised approach.

**If it still happens:** make sure the tool returns a `ToolResult` (not a raw string). Legacy string returns are tolerated but they may not classify cleanly into an `ErrorCode`. Check the relevant tool function and convert it to `ToolResult.error(ErrorCode.X, …)`.

---

## 3. RAG returns irrelevant chunks or `NO_DATA`

**Red flags:**
- `query_rag` results have scores ≤ 0.4 across the board.
- The bot says *"NO_DATA — all matches below score 0.40"* for questions the corpus definitely covers.
- Healthcheck warns *"vector dim mismatch — got X, expected Y for `<model>`"*.

**Root cause:** the embedding model used to index `cad_db/` is different from `EMBED_MODEL` in `config.py`. Embeddings from different models can't be compared. Common culprit: an older `cad_db/` built with `all-mpnet-base-v2` (768-dim) being queried by `mxbai-embed-large` (1024-dim).

**Fix:**

```powershell
# 1. Delete the stale DB
Remove-Item -Recurse -Force cad_db

# 2. Re-embed with the current EMBED_MODEL
.\venv\Scripts\python -m scripts.update_rag
```

The healthcheck `ChromaDB / RAG store` check will turn green once dims match.

**If chunks are still irrelevant after re-embedding:** the `top_k` / `min_score` tool args are too greedy. The default `min_score = 0.4` is a floor; raise to `0.55` for tighter matches by editing the prompt or the tool's default in [core/tools.py](../../../core/tools.py).

---

## 4. Config typo refuses to start the proxy

**Red flags:**
- Proxy exits at startup with a colored *"Config validation failed"* table.
- Error message points to e.g. `agents.main.toolss: Extra inputs are not permitted`.

**Root cause:** pydantic v2 schema in [core/config_schema.py](../../../core/config_schema.py) caught a misspelled key. This is intentional — v3 wants typos to fail at import, not deep in the agent loop.

**Fix:** open the file the error points at (`config.py` or `runtime_overrides.json`), fix the listed key, restart.

Common gotchas:
- `toolss` vs `tools` in an agent profile
- `verify` value must be `"off"` or `"high"` (not `"yes"` / `"true"`)
- `MODEL_PROVIDERS` slot type must be `"api"` or `"local"`
- A missing `main` profile in `AGENT_PROFILES` — that one is required by the model validator.

---

## 5. "Ollama refused connection"

**Red flags:**
- `OllamaError: ... refused`, `ConnectionRefusedError`, or timeouts on every chat.
- Healthcheck shows `Ollama reachable: FAIL`.

**Root cause:** the Ollama daemon isn't running, or it's bound to a different host/port than `OLLAMA_BASE` in `config.py`.

**Fix:**

```powershell
# Start the daemon (Windows: it usually autostarts after install)
ollama serve

# In a separate terminal, confirm it answers:
curl http://localhost:11434/api/tags
```

If you've moved it to a non-default port, edit `config.py:OLLAMA_BASE`.

---

## 6. Discord bot drops messages

**Red flags:**
- Messages in an allowed channel get no reply.
- Bot replies to some users but not others.

**Root cause:** the guild or channel id isn't in `DISCORD_SETTINGS["guilds"]` allow-list. The Discord adapter is intentionally strict — it ignores anything not explicitly listed.

**Fix:**
1. Right-click the channel in Discord → *Copy Channel ID*.
2. Add it to `DISCORD_SETTINGS["guilds"][<guild_id>]["allowed_channels"]`.
3. Restart the proxy.

To temporarily allow all channels in a guild during dev, set `DISCORD_SETTINGS["allow_all_channels"] = True` — but never ship with that on.

---

## 7. Permission modal doesn't appear

**Red flags:**
- A `write_file` / `run_command` is in-flight but no modal shows.
- After 120s the agent reports `PERMISSION_DENIED: timeout — user did not respond`.

**Root cause:** the UI's polling loop isn't reaching `/api/permissions/pending`. Common causes:
- The browser tab is backgrounded (Chrome throttles `setInterval` to ~1/min in background tabs — see [core/proxy.py:HEARTBEAT_TIMEOUT](../../../core/proxy.py)).
- A previous modal was dismissed via DOM manipulation rather than the Allow/Deny buttons.

**Fix:**
- Bring the LocalHelpBot tab to the foreground.
- Hard-refresh the UI (Ctrl+F5).
- Confirm `pollPermissions` is firing in DevTools console.

If the modal renders but the diff/command preview is empty, the tool may have sent malformed `details` to `request_permission(...)`. Check the relevant `_gated_*` wrapper in [core/tools.py](../../../core/tools.py) or [core/plugins/](../../../core/plugins/).

---

## 8. Logs tab is empty

**Red flags:**
- Open Logs tab → "Waiting for events…" never goes away.
- `proxy.log.jsonl` exists and has entries but the UI shows nothing.

**Root cause:** the in-memory ring buffer (`core/logs.py:_ring`) only holds events emitted AFTER `core.logs.configure()` has run. If the proxy crashed before configuring, the buffer is empty. Also: the UI only polls while the Logs tab is the active tab.

**Fix:**
- Make sure the Logs tab is open and active.
- Trigger any chat — that emits ≥ 1 event per subsystem.
- If the buffer is genuinely empty (proxy just started), use `Get-Content proxy.log.jsonl -Tail 50` from PowerShell as a fallback.

---

## 9. CoVe makes responses worse

**Red flags:**
- You enabled `"verify": "high"` on a profile and answers got *less* coherent or noticeably longer with no clear improvement.

**Root cause:** Chain-of-Verification on a small local model (≤ 7B) is hit-and-miss. The model may invent verification questions whose answers don't actually correct the draft.

**Fix:**
- Use `verify: high` only on `main` and `researcher` profiles for write-heavy tasks (PDF/DOCX generation, multi-file refactors).
- Leave it `off` for `coder` (read-only) and `summarizer` (compression).
- If you have a larger model installed, switch the profile's `model` to that — CoVe shines on ≥ 13B models.

---

## Common diagnostic commands

```powershell
# Full health pass
.\venv\Scripts\python -m core.healthcheck

# Tail the structured JSONL log
Get-Content proxy.log.jsonl -Tail 50 | ConvertFrom-Json | Format-Table ts, subsystem, level, msg

# Filter to a single subsystem (e.g. tools)
Get-Content proxy.log.jsonl -Tail 200 | ForEach-Object { ConvertFrom-Json $_ } | Where-Object subsystem -eq tools

# Inspect a session's persisted state
Get-Content data\sessions\<sid>.jsonl | ConvertFrom-Json | Format-List

# Confirm Ollama models
ollama list
```

## Red flags that mean something else is wrong

These almost never have a config explanation — investigate the root cause directly:

- **Memory grows unboundedly across long sessions** → the compaction safety net in `core/proxy.py:_ensure_budget` is failing silently. Check the proxy logs for `compact safety-net failed` warnings.
- **Tool returns ok=True but the file was not actually written** → look at the tool's `meta` field (it should include `files_touched`); if missing, the tool isn't reporting state correctly.
- **Streaming response stalls partway** → almost always a network buffer issue with Ollama. Try `OLLAMA_BASE` over `127.0.0.1` instead of `localhost`.
- **Permission modal flashes then disappears** → another browser tab on the same proxy approved/denied the request before you saw it. Close the other tab.
