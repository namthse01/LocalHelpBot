"""Typed config schema (pydantic v2).

Inspired by `openclaw/src/config/types.openclaw.ts`. The job of this
module is to take whatever the user wrote in `config.py` plus any
runtime overrides from `runtime_overrides.json`, validate the whole
thing as a single object, and produce a `RootConfig` instance — with a
big colored error message if anything is wrong.

Why we bother: before this, a typo like `"toolss": [...]` in a profile
only blew up the FIRST time a specialist was invoked, often deep in
the agent loop. Now `import config` fails at startup with a pointer
to the bad key.

Public surface:
    RootConfig.load(module, overrides_path) -> RootConfig
    pretty_print_validation_error(err) -> None
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

# Stable error codes for verify flag — must match what core/orchestrator.py uses.
VerifyMode = Literal["off", "high"]


# ───────────────────────────────────────────────────────────────────────
# Models
# ───────────────────────────────────────────────────────────────────────


class AgentProfile(BaseModel):
    """One specialist profile.

    `tools` is the allowlist passed to ToolRegistry.filter(). Unknown
    tool names don't fail validation here (the registry exists in a
    different module) — they just silently miss out at runtime. That's
    intentional: a profile may reference a tool that isn't loaded yet
    if its plugin is optional.
    """

    model_config = ConfigDict(extra="forbid")

    description: str = ""
    system_prompt: str
    model: str
    tools: List[str] = Field(default_factory=list)
    verify: VerifyMode = "off"


class ModelProviderSlot(BaseModel):
    """A primary or fallback model provider entry."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["api", "local"] = "local"
    provider: str = "ollama"
    api_key: Optional[str] = None
    model: str


class ModelProviders(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary: ModelProviderSlot
    fallback: ModelProviderSlot


class DiscordGuild(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_channels: List[int] = Field(default_factory=list)
    admin_role_id: Optional[int] = None


class DiscordSettings(BaseModel):
    """Discord wiring. Accepts the existing `{guilds: {id: {...}}}`
    shape from `config.py:DISCORD_SETTINGS`."""

    model_config = ConfigDict(extra="allow")  # allow legacy aux fields

    guilds: Dict[int, DiscordGuild] = Field(default_factory=dict)
    default_guild_id: Optional[int] = None
    allow_all_channels: bool = False


class AutomationTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    schedule: str
    prompt: str
    recipient: int


class RootConfig(BaseModel):
    """The whole config tree.

    Built either from `config.py` constants (load_from_module) or from
    a dict (eg. tests). Returns a frozen-ish view; downstream code
    still mutates the legacy module-level globals for backward compat.
    """

    model_config = ConfigDict(extra="forbid")

    # Connectivity & models
    ollama_base: str = "http://localhost:11434"
    chat_model: str
    large_model: str = ""
    embed_model: str = "mxbai-embed-large:latest"
    proxy_port: int = 11435

    # Provider routing
    providers: ModelProviders

    # Discord wiring
    discord_token: str = ""
    discord: DiscordSettings = Field(default_factory=DiscordSettings)

    # Agent profiles
    agents: Dict[str, AgentProfile]

    # Scheduled tasks
    tasks: List[AutomationTask] = Field(default_factory=list)

    # ── validators ──────────────────────────────────────────────────
    @model_validator(mode="after")
    def _require_main_agent(self) -> "RootConfig":
        if "main" not in self.agents:
            raise ValueError("AGENT_PROFILES must include a 'main' profile — it's the default specialist.")
        return self


# ───────────────────────────────────────────────────────────────────────
# Loader
# ───────────────────────────────────────────────────────────────────────


def _decrypt_providers_inplace(providers: Dict[str, Any]) -> Dict[str, Any]:
    """Decrypt api_key fields stored encrypted in runtime_overrides.json.

    Best-effort: if `core.secrets.decrypt_secret` raises we leave the
    field as-is so the user still gets a sensible error from the model.
    """
    try:
        from core.secrets import decrypt_secret
    except Exception:
        return providers
    out = dict(providers)
    for slot in ("primary", "fallback"):
        p = out.get(slot) or {}
        key = p.get("api_key")
        if key:
            try:
                p = dict(p)
                p["api_key"] = decrypt_secret(key)
                out[slot] = p
            except Exception:
                pass
    return out


def load_root_config(
    *,
    base: Dict[str, Any],
    overrides_path: Optional[Path] = None,
) -> RootConfig:
    """Validate the merged config (base + overrides).

    `base` is the dict view of `config.py` constants. `overrides_path`
    is `runtime_overrides.json` next to config.py (created by the UI's
    Apply Mode Changes button).
    """
    merged = dict(base)

    if overrides_path and overrides_path.exists():
        try:
            import json
            with overrides_path.open("r", encoding="utf-8") as fh:
                overrides = json.load(fh)
            if "providers" in overrides:
                merged["providers"] = _decrypt_providers_inplace(overrides["providers"])
            for k in ("agents", "discord", "tasks"):
                if k in overrides:
                    merged[k] = overrides[k]
        except Exception as e:
            # We don't fail here — the user can fix the override file
            # without nuking their whole config.
            from core.logs import get_logger
            get_logger("proxy").warning(f"Could not load runtime_overrides.json: {e}")

    try:
        return RootConfig.model_validate(merged)
    except ValidationError as e:
        pretty_print_validation_error(e)
        raise SystemExit(1)


def pretty_print_validation_error(err: ValidationError) -> None:
    """Print a rich-formatted validation error and exit hint.

    Falls back to plain stderr if rich is unavailable.
    """
    try:
        from rich.console import Console
        from rich.table import Table
        from rich.text import Text
        console = Console(stderr=True, force_terminal=True)
        console.rule("[bold red]Config validation failed[/bold red]")
        console.print(
            "[red]LocalHelpBot refused to start because [bold]config.py[/bold] "
            "or [bold]runtime_overrides.json[/bold] has invalid entries.[/red]\n"
        )
        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("Where", style="cyan", overflow="fold")
        table.add_column("Problem", overflow="fold")
        for e in err.errors():
            loc = ".".join(str(p) for p in e["loc"]) or "(root)"
            table.add_row(loc, e["msg"])
        console.print(table)
        console.print()
        console.print(
            "[yellow]How to fix:[/yellow] open the file and correct the listed keys. "
            "Common gotchas: misspelled profile keys (e.g. [bold]toolss[/bold] vs [bold]tools[/bold]), "
            "missing [bold]main[/bold] agent profile, wrong types in [bold]MODEL_PROVIDERS[/bold]."
        )
    except Exception:
        # Rich not available — fall back to plain stderr.
        print("Config validation failed:", file=sys.stderr)
        for e in err.errors():
            loc = ".".join(str(p) for p in e["loc"]) or "(root)"
            print(f"  {loc}: {e['msg']}", file=sys.stderr)


__all__ = [
    "AgentProfile",
    "ModelProviderSlot",
    "ModelProviders",
    "DiscordGuild",
    "DiscordSettings",
    "AutomationTask",
    "RootConfig",
    "load_root_config",
    "pretty_print_validation_error",
    "VerifyMode",
]
