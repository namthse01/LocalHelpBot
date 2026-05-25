"""Slice 0.4 — pydantic schema invariants for config.py."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.config_schema import RootConfig


def _minimal_valid_config() -> dict:
    return {
        "chat_model": "qwen3.5:latest",
        "providers": {
            "primary":  {"type": "local", "provider": "ollama", "model": "qwen3.5:latest"},
            "fallback": {"type": "local", "provider": "ollama", "model": "qwen3.5:latest"},
        },
        "agents": {
            "main": {
                "system_prompt": "you are main",
                "model": "qwen3.5:latest",
                "tools": [],
            },
        },
    }


def test_happy_path_validates():
    cfg = RootConfig.model_validate(_minimal_valid_config())
    assert cfg.chat_model == "qwen3.5:latest"
    assert "main" in cfg.agents


def test_missing_main_agent_fails():
    bad = _minimal_valid_config()
    bad["agents"] = {"researcher": bad["agents"]["main"]}
    with pytest.raises(ValidationError) as exc:
        RootConfig.model_validate(bad)
    assert any("main" in e["msg"] for e in exc.value.errors())


def test_typo_in_profile_key_fails_with_path():
    bad = _minimal_valid_config()
    bad["agents"]["main"]["toolss"] = []   # typo
    with pytest.raises(ValidationError) as exc:
        RootConfig.model_validate(bad)
    locs = [".".join(str(p) for p in e["loc"]) for e in exc.value.errors()]
    assert any("toolss" in loc for loc in locs)


def test_bad_verify_value_fails():
    bad = _minimal_valid_config()
    bad["agents"]["main"]["verify"] = "yes"   # must be "off" | "high"
    with pytest.raises(ValidationError):
        RootConfig.model_validate(bad)


def test_bad_provider_type_fails():
    bad = _minimal_valid_config()
    bad["providers"]["primary"]["type"] = "remote"  # must be "api" | "local"
    with pytest.raises(ValidationError):
        RootConfig.model_validate(bad)


def test_default_verify_is_off():
    cfg = RootConfig.model_validate(_minimal_valid_config())
    assert cfg.agents["main"].verify == "off"


def test_default_provider_type_when_omitted():
    """ModelProviderSlot.type defaults to 'local'."""
    cfg_dict = _minimal_valid_config()
    cfg_dict["providers"]["primary"] = {"model": "x"}
    cfg = RootConfig.model_validate(cfg_dict)
    assert cfg.providers.primary.type == "local"
    assert cfg.providers.primary.provider == "ollama"


def test_real_config_module_imports_and_validates():
    """The actual config.py at project root should parse cleanly."""
    import config
    assert hasattr(config, "AGENT_PROFILES")
    assert "main" in config.AGENT_PROFILES
    assert hasattr(config, "CONFIG")
    # CONFIG may be None if validation fell back, but a properly maintained
    # config.py should populate it.
    assert config.CONFIG is not None, "config.py validation failed at import"


def test_cad_rag_specialist_profile_is_present():
    """Slice 0.2 added this profile; the cad-rag virtual model depends on it."""
    import config
    assert "cad-rag-specialist" in config.AGENT_PROFILES
    p = config.AGENT_PROFILES["cad-rag-specialist"]
    assert "query_rag" in p["tools"]
