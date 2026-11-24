"""The default provider and model are the ones the recorded eval results were run on."""
from __future__ import annotations

from fleet_copilot.config import load_llm_settings
from fleet_copilot.paths import PROJECT_ROOT

EVAL_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"


def test_default_is_openrouter_with_the_eval_model(monkeypatch):
    monkeypatch.delenv("FLEET_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("FLEET_LLM_MODEL", raising=False)
    s = load_llm_settings(PROJECT_ROOT / "config")
    assert s.provider == "openrouter" and s.model == EVAL_MODEL
    assert s.api_key_env == "OPENROUTER_API_KEY" and s.base_url == "https://openrouter.ai/api/v1"


def test_environment_still_overrides_the_default(monkeypatch):
    monkeypatch.setenv("FLEET_LLM_PROVIDER", "anthropic")
    monkeypatch.delenv("FLEET_LLM_MODEL", raising=False)
    s = load_llm_settings(PROJECT_ROOT / "config")
    assert s.provider == "anthropic" and s.model.startswith("claude-")
