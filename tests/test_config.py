"""Policy loading: fail closed (G5, TC-08) and frozen at runtime (G6, TC-09)."""

import dataclasses
import shutil
from pathlib import Path

import pytest

from fleet_copilot.config import ConfigError, load_app_config, load_policy
from fleet_copilot.paths import PROJECT_ROOT

CONFIG = PROJECT_ROOT / "config"


def test_policy_loads_and_resolves_aliases():
    policy = load_policy(CONFIG / "policy.yaml")
    assert policy.resolve_tenant("ACME").tenant_id == "acme-001"
    assert policy.resolve_tenant("acme-001").name == "Acme"
    assert policy.resolve_tenant("unknown") is None
    assert "lock_device" not in policy.action_tools
    assert "fixture-999" not in policy.tenants


def test_eval_policy_extends_base():
    policy = load_policy(CONFIG / "policy.eval.yaml")
    assert "fixture-999" in policy.tenants and "acme-001" in policy.tenants
    assert policy.max_devices_per_batch == 10


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.unlink(),
        lambda p: p.write_text("tenants: [unclosed", encoding="utf-8"),
        lambda p: p.write_text(p.read_text(encoding="utf-8").replace("acknowledge_finding: {enabled: true}", "launch_rockets: {enabled: true}"), encoding="utf-8"),
        lambda p: p.write_text(p.read_text(encoding="utf-8").replace("approval_required: true", "approval_required: false"), encoding="utf-8"),
    ],
    ids=["missing", "malformed", "unknown-action", "approval-disabled"],
)
def test_policy_fails_closed(tmp_path: Path, mutate):
    shutil.copytree(CONFIG, tmp_path / "config")
    mutate(tmp_path / "config" / "policy.yaml")
    with pytest.raises(ConfigError):
        load_app_config(tmp_path / "config")


def test_policy_is_frozen():
    policy = load_policy(CONFIG / "policy.yaml")
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        policy.max_devices_per_batch = 1000
    with pytest.raises(TypeError):
        policy.tenants["evil-1"] = None  # type: ignore[index]
    with pytest.raises(TypeError):
        policy.auto_propose_rules["notify_employee"] = {}  # type: ignore[index]


def test_thresholds_describe():
    cfg = load_app_config(CONFIG)
    assert "15%" in cfg.thresholds.describe("low_disk")
    assert cfg.thresholds.get("stale_device", "days") == 10
