"""Reasoning effort: set per step in llm.yaml, sent only on the planning call."""

from __future__ import annotations

import dataclasses
import shutil

import pytest

from fleet_copilot.agent.session import CopilotSession
from fleet_copilot.config import ConfigError, load_app_config, load_llm_settings
from fleet_copilot.llm.base import FakeClock
from fleet_copilot.llm.providers import reasoning_kwargs
from fleet_copilot.llm.scripted import ScriptedModel, call
from fleet_copilot.paths import PROJECT_ROOT
from fleet_copilot.session import SessionContext
from fleet_copilot.toolkit import Toolkit
from tests.test_agent import LOW_DISK_ANSWER, SETTINGS, plan, submit
from tests.test_tools import AS_OF


def run(home, settings):
    cfg = load_app_config(home.config, "policy.eval.yaml")
    t = cfg.policy.resolve_tenant("fixture-999")
    tk = Toolkit(SessionContext.new(t.tenant_id, t.name, "tester", AS_OF), cfg, home)
    model = ScriptedModel([plan(steps=[{"tool": "find_devices", "purpose": "low disk"}]),
                           call("find_devices", {"criteria": {"low_disk": True}}), submit(LOW_DISK_ANSWER)])
    sess = CopilotSession(tk, model, settings, PROJECT_ROOT / "prompts", clock=FakeClock())
    res = sess.ask("Which devices are low on disk space?")
    assert res.status == "answered", res.text
    return model.efforts


def test_only_the_planning_call_gets_the_effort(home):
    low = dataclasses.replace(SETTINGS, reasoning_effort={"planner": "low"})
    assert run(home, low) == ["low", None, None]


def test_no_effort_sent_when_unset_or_blank(home):
    assert run(home, SETTINGS) == [None, None, None]
    assert run(home, dataclasses.replace(SETTINGS, reasoning_effort={"planner": ""})) == [None, None, None]


def test_request_settings_per_provider():
    assert reasoning_kwargs("openrouter", "low") == {"extra_body": {"reasoning": {"effort": "low"}}}
    assert reasoning_kwargs("openai", "low") == {"reasoning_effort": "low"}
    assert reasoning_kwargs("anthropic", "low") == {}
    for provider in ("openrouter", "openai", "anthropic"):
        assert reasoning_kwargs(provider, None) == {} and reasoning_kwargs(provider, "") == {}


def test_shipped_config_asks_for_low_effort_when_planning():
    assert load_llm_settings(PROJECT_ROOT / "config").reasoning_effort == {"planner": "low"}


@pytest.mark.parametrize("bad", ["reasoning_effort:\n  planner: huge\n", "reasoning_effort:\n  agent: low\n",
                                 "reasoning_effort: low\n"])
def test_bad_values_stop_at_startup(tmp_path, bad):
    shutil.copytree(PROJECT_ROOT / "config", tmp_path / "config")
    path = tmp_path / "config" / "llm.yaml"
    text = path.read_text(encoding="utf-8")
    start = text.index("reasoning_effort:")
    path.write_text(text[:text.rindex("\n", 0, start) + 1] + bad, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_llm_settings(tmp_path / "config")


def test_per_turn_output_budget_is_8000():
    assert load_app_config(PROJECT_ROOT / "config").policy.max_output_tokens_per_turn == 8000
