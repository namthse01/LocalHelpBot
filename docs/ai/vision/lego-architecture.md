---
phase: vision
title: TheAgent0 — Lego Architecture (north star)
description: The long-term architectural direction for TheAgent0 — a modular, hot-swappable, self-learning AI where every code block is an individual brick that can be upgraded or replaced while the system is running.
---

# TheAgent0 — Lego Architecture

> **Status: vision document, not implementation.** This file captures *where we are going*, not what is built. Concrete work tracked in [planning/README.md](../planning/README.md) under M6 / M7 and any successor milestones.

---

## 1. North Star

**One AI. Easy to use. Can do everything. Self-learning. Self-evolving.**

That is the mission. Every architectural decision should reduce friction along one of those four axes. If a change makes the system harder to use, less capable, less able to learn, or less able to evolve itself — it is wrong, even if it looks elegant on paper.

This is what makes TheAgent0 different from a chatbot wrapper, an automation framework, or a static agent: the codebase itself is designed to grow. Pieces come off, get rebuilt, snap back in — while the rest keeps running.

---

## 2. Lego Principles

The metaphor is literal. Treat every module like a Lego brick.

1. **Every module is a brick with a well-defined surface.** A brick has studs on top and sockets below — nothing else. A module exposes a *contract* (input schema, output schema, side-effect declaration) and nothing else. Internals are private.
2. **Bricks can be added, removed, or upgraded while the tower is standing.** Hot-swap is the default expectation, not a special case. Restarting the proxy to install a tool is a regression.
3. **A failing brick must not topple the tower.** Isolation + graceful degradation. If `download_file` crashes, `read_file` keeps working. If `vision_tools` fails its health probe, it is quarantined; the rest of the system carries on.
4. **Adding a new brick must not require touching existing bricks.** Drop a `.py` file into `core/plugins/`, declare its manifest, and the system picks it up. No edits to `orchestrator.py`, `agent.py`, or `config.py` required.
5. **Bricks know what they need.** A manifest declares dependencies (other bricks, Python packages, model availability, OS, etc.). The loader respects them; the marketplace enforces them.

---

## 3. Existing foundation (what we already have)

The current codebase already embodies parts of this. New work should *build on these*, not replace them.

- **Plugin contract** — every `core/plugins/*.py` exposes `register(registry: ToolRegistry) -> None`. Discovered via `pkgutil.iter_modules()` in [core/tool_schema.py](../../../core/tool_schema.py) (`load_plugins()`).
- **Tool registry** — `ToolRegistry` + filtered sub-registries per specialist. Built once in [core/orchestrator.py](../../../core/orchestrator.py).
- **Self-learning kernel** — `LessonsStore` ([core/lessons.py](../../../core/lessons.py)) persists JSONL lessons, injects them into T1 of every system prompt via [core/context.py](../../../core/context.py).
- **Correction auto-capture** — `_maybe_capture_correction()` / `_maybe_capture_preference()` in [core/orchestrator.py](../../../core/orchestrator.py) detect user corrections and persist them. Opt-in via `LESSONS_AUTO_CAPTURE` in [config.py](../../../config.py).
- **Stop-the-Line** — repeated identical tool failures are refused after 2 attempts. Prevents retry storms. See [core/agent.py](../../../core/agent.py).
- **Permission queue** — every write/exec tool routes through user approval. The substrate for safe self-modification.
- **`update_self` tool** — `learning_tools.py` already exposes a permission-gated `git pull --ff-only` for the agent to upgrade its own code. Disabled by default (`UPDATE_SELF_ENABLED`).

These are the *studs* on the existing tower. Future bricks should snap onto them.

---

## 4. Gaps to close (the Lego work)

Each item below is a separable workstream. The order in §5 matters; the items themselves do not need to ship together.

### 4.1 Plugin manifests

Every plugin module declares a structured manifest. Today the plugin only exposes `register()` — the system has no way to know its version, dependencies, or health check.

```python
PLUGIN_META = {
    "name": "system_tools",
    "version": "1.2.0",
    "depends_on": [],              # other plugin names
    "python_requires": ["Pillow>=10", "psutil>=5.9"],
    "model_requires": [],          # e.g. ["llava:latest"] for vision_tools
    "platform": ["windows", "linux", "darwin"],
}

def health_probe() -> tuple[bool, str]:
    """Return (ok, message). Called periodically. Cheap; no side effects."""
    ...

def register(registry: ToolRegistry) -> None:
    ...
```

The manifest is the *contract*. Everything downstream — the marketplace, the hot-reloader, the dependency graph, the health monitor — reads from it.

### 4.2 Versioned `ToolRegistry`

The current registry is mutable and unversioned: once a tool is registered, you cannot atomically swap it. We need:

- An immutable snapshot per registration cycle.
- A pointer (`current`) that the orchestrator reads under a read lock.
- A `swap(new_snapshot)` operation that publishes a new snapshot atomically.
- A bounded ring of past snapshots for rollback.

This is the safety net for hot-reload — if the swap fails health, the pointer rolls back to the previous snapshot.

### 4.3 Plugin health monitor

A background thread (or async task) that walks every registered plugin's `health_probe()` on a slow cadence (e.g. every 60s). Reports results into:

- `/api/healthcheck` for the UI banner.
- A new `tool_health.jsonl` log so the agent can read its own history.
- A *quarantine* policy: a plugin failing N consecutive probes is removed from the active registry (with the user notified).

The monitor is the *eyes* of the system — without it, hot-reload is flying blind.

### 4.4 Hot-reload mechanism

A new tool, `reload_plugin <name>`, that:

1. Acquires the registry write lock.
2. Calls `importlib.reload(core.plugins.<name>)`.
3. Builds a new candidate `ToolRegistry` from the reloaded module's `register()`.
4. Runs `health_probe()` on the candidate.
5. If healthy → `swap(candidate)` publishes it; old snapshot drops to the rollback ring.
6. If unhealthy → discard the candidate; the pointer never moves. Surface the error.

In-flight tool calls hold the read lock; they finish against the *old* snapshot. New calls after the swap hit the *new* snapshot. No request sees a half-swapped state.

### 4.5 Feedback loop — what worked, what didn't

The agent today records *corrections* but not *outcomes*. We need:

- Per-tool success/failure counters keyed by tool name + error code.
- Per-tool average retry count and time-to-success.
- Common-error-code surface: "this tool failed with `MISSING_ARG` 80% of the time today — its schema may be misleading."

Feed these signals into the lesson system so the agent learns *which tools work for which task shapes*, and the user can see *which tools are getting worse over time* and target them for improvement.

### 4.6 Self-reflection cycle

End-of-conversation summarizer that asks itself "what would I do differently?" and writes a structured lesson — not just user corrections, but the agent's own retrospective. Combined with §4.5, this gives the agent a way to evolve its own playbook without waiting for the user to teach it.

### 4.7 Plugin marketplace (M7)

A curated registry of installable plugins. The agent (with user permission) can:

- List available plugins (`marketplace_list`).
- Install one by name (`marketplace_install <name>` — pulls source, verifies manifest, runs probe, registers).
- Uninstall (`marketplace_uninstall <name>` — deregisters, removes source).

All of this rides on top of §4.1–§4.4. The marketplace is *just* a curated source of bricks; the snap-in mechanism is the same one used for local plugins.

---

## 5. Suggested sequencing

The order matters. Each step is non-breaking by itself and enables the next.

1. **Plugin manifests** (§4.1) — purely additive. Existing plugins keep working; manifests are opt-in. Low risk, high information value.
2. **Health probes + monitor** (§4.3) — visibility before mutation. We need to *see* plugin state before we start changing it.
3. **Versioned registry + rollback** (§4.2) — the safety net. Build it before anything that mutates the registry at runtime.
4. **Hot-reload tool** (§4.4) — the payoff. Now `reload_plugin` is a transaction with a safe rollback path.
5. **Feedback loop + self-reflection** (§4.5, §4.6) — closes M6. With hot-reload working, the agent's lessons can drive *what to reload and why*.
6. **Marketplace** (§4.7) — closes M7. By this point the foundation is mature enough that a marketplace is a thin layer on top.

---

## 6. Risks & open questions

These are real and unresolved. Anyone touching the Lego work should engage with them before writing code.

- **Concurrency during reload.** The proposal in §4.4 uses a read/write lock on the registry pointer. But what about a long-running tool call (e.g. `run_command` waiting on a 30s shell)? Do we wait for it to finish, or fence it onto the old snapshot and let it complete there? Pick one and document it.
- **Stateful plugins.** Today plugins are pure: register → call → return. A stateful plugin (caches, open file handles, connected sessions) needs an `export_state() / import_state()` contract on the manifest so state survives a reload. We don't have that yet.
- **Dependency graph.** Plugin B depends on Plugin A. If A is reloaded, must B reload too? Or only if A's interface changed? A *semver-respecting* answer is: reload A; reload B only if A's major version bumped. Requires manifest discipline.
- **Rollback policy.** How many past snapshots do we keep? Memory cost grows with `N × registry_size`. Probably bounded at 3 — enough to survive a bad swap and roll back to the last-known-good.
- **Versioning.** SemVer per plugin is the obvious answer. But what's the version of *the registry itself*? When does the core's contract change in a way that invalidates old manifests? Need a `MIN_CORE_VERSION` declaration on each manifest.
- **User trust during self-modification.** `update_self` already routes through the permission queue. So should `reload_plugin` and especially `marketplace_install`. The principle: *the user must be able to see and approve every brick swap*, at least until they explicitly grant standing approval.

---

## 7. Cross-references

- **Existing milestones**: [planning/README.md](../planning/README.md) — M6 (Agent self-improve), M7 (Plugin marketplace). This document is the design backdrop for both.
- **Plugin contract**: [core/plugins/__init__.py](../../../core/plugins/__init__.py), `register(registry)`. Manifest extension is strictly additive.
- **Self-learning store**: [core/lessons.py](../../../core/lessons.py), [core/context.py](../../../core/context.py). Feedback-loop signals (§4.5) feed into the same store.
- **Update self**: existing tool in [core/plugins/learning_tools.py](../../../core/plugins/learning_tools.py). Same permission model that `reload_plugin` should reuse.

---

*This document is alive — edit it as the architecture sharpens. The goal is not to fix the design today; it is to make sure every code change knows which direction the tower is growing.*
