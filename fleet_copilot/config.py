"""Configuration loading. Policy is frozen after load and fails closed on any error."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

KNOWN_ACTIONS = (
    "open_remediation_ticket",
    "flag_device_for_replacement",
    "create_upgrade_order",
    "notify_employee",
    "request_software_removal",
    "acknowledge_finding",
)
KNOWN_READ_TOOLS = (
    "resolve_entities",
    "find_devices",
    "get_device",
    "get_device_history",
    "get_compliance_overview",
    "get_software",
    "run_insights",
    "get_definitions",
    "list_proposals",
)


class ConfigError(RuntimeError):
    """Raised when a config file is missing or invalid. The CLI exits on this."""


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def _load_yaml(path: Path) -> dict:
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Malformed YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Config file {path} must contain a mapping")
    return data


def _load_with_extends(path: Path) -> dict:
    data = _load_yaml(path)
    parent = data.pop("extends", None)
    if parent:
        base = _load_with_extends(path.parent / parent)
        for key, value in data.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                merged = dict(base[key])
                merged.update(value)
                base[key] = merged
            else:
                base[key] = value
        return base
    return data


@dataclass(frozen=True)
class Tenant:
    tenant_id: str
    name: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class Policy:
    tenants: Mapping[str, Tenant]
    read_tools: tuple[str, ...]
    action_tools: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    approval_required: bool
    max_devices_per_batch: int
    max_tool_calls_per_turn: int
    max_verifier_revisions: int
    input_max_chars: int
    auto_propose: bool
    max_auto_proposals_per_turn: int
    auto_propose_rules: Mapping[str, Mapping[str, Any]]
    max_input_tokens_per_turn: int
    max_output_tokens_per_turn: int
    turn_deadline_s: float
    max_input_tokens_per_session: int
    max_input_tokens_per_day: int
    context: Mapping[str, int]
    upgrade_specs: Mapping[str, tuple[int, ...]]
    notify_templates: Mapping[str, str]
    source: str = ""

    def resolve_tenant(self, ref: str) -> Tenant | None:
        """Map a tenant ID or alias (case-insensitive) to a registered tenant."""
        key = (ref or "").strip().lower()
        for tenant in self.tenants.values():
            if key == tenant.tenant_id.lower() or key in tenant.aliases or key == tenant.name.lower():
                return tenant
        return None


def _require(data: dict, key: str, typ: type | tuple[type, ...], path: Path) -> Any:
    if key not in data:
        raise ConfigError(f"{path.name}: missing required key '{key}'")
    value = data[key]
    if not isinstance(value, typ):
        raise ConfigError(f"{path.name}: key '{key}' has the wrong type")
    return value


CONTEXT_DEFAULTS = {
    "history_turns": 3, "digest_max_chars": 1500, "max_chars_per_turn": 48000, "keep_recent_tool_results": 2,
    "max_tool_output_chars": 12000, "focus_evidence_ids": 12, "focus_queries": 4,
}


def _context(raw: dict[str, Any], path: Path) -> Mapping[str, int]:
    unknown = set(raw) - set(CONTEXT_DEFAULTS)
    if unknown:
        raise ConfigError(f"{path.name}: unknown context setting(s) {sorted(unknown)}")
    merged = {**CONTEXT_DEFAULTS, **raw}
    for key, value in merged.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ConfigError(f"{path.name}: context.{key} must be a whole number of 0 or more")
    if merged["history_turns"] < 1 or merged["max_tool_output_chars"] < 500:
        raise ConfigError(f"{path.name}: context.history_turns must be at least 1 and max_tool_output_chars at least 500")
    return MappingProxyType(merged)


def load_policy(path: Path) -> Policy:
    data = _load_with_extends(path)
    tenants_raw = _require(data, "tenants", dict, path)
    if not tenants_raw:
        raise ConfigError(f"{path.name}: no tenants registered")
    tenants: dict[str, Tenant] = {}
    for tid, info in tenants_raw.items():
        if not isinstance(info, dict) or "name" not in info:
            raise ConfigError(f"{path.name}: tenant '{tid}' needs a name")
        aliases = tuple(str(a).lower() for a in info.get("aliases", []))
        tenants[tid] = Tenant(tid, str(info["name"]), aliases)

    read_tools = tuple(_require(data, "read_tools", list, path))
    for tool in read_tools:
        if tool not in KNOWN_READ_TOOLS:
            raise ConfigError(f"{path.name}: unknown read tool '{tool}'")
    actions_raw = _require(data, "action_tools", dict, path)
    for name in actions_raw:
        if name not in KNOWN_ACTIONS:
            raise ConfigError(f"{path.name}: unknown action '{name}'")
    forbidden = tuple(data.get("forbidden_actions", []))
    enabled = tuple(n for n, cfg in actions_raw.items() if (cfg or {}).get("enabled", False) and n not in forbidden)

    if data.get("approval_required") is not True:
        raise ConfigError(f"{path.name}: approval_required must be true")

    budgets = _require(data, "budgets", dict, path)
    specs = _require(data, "upgrade_specs", dict, path)
    templates = _require(data, "notify_templates", dict, path)
    rules = data.get("auto_propose_rules", {}) or {}
    for name in rules:
        if name not in enabled:
            raise ConfigError(f"{path.name}: auto_propose_rules lists disabled or unknown action '{name}'")
        if name in ("notify_employee", "acknowledge_finding"):
            raise ConfigError(f"{path.name}: '{name}' may never be proposed without a request")

    try:
        policy = Policy(
            tenants=MappingProxyType(tenants),
            read_tools=read_tools,
            action_tools=enabled,
            forbidden_actions=forbidden,
            approval_required=True,
            max_devices_per_batch=int(_require(data, "max_devices_per_batch", int, path)),
            max_tool_calls_per_turn=int(_require(data, "max_tool_calls_per_turn", int, path)),
            max_verifier_revisions=int(data.get("max_verifier_revisions", 2)),
            input_max_chars=int(_require(data, "input_max_chars", int, path)),
            auto_propose=bool(data.get("auto_propose", False)),
            max_auto_proposals_per_turn=int(data.get("max_auto_proposals_per_turn", 5)),
            auto_propose_rules=_freeze(rules),
            max_input_tokens_per_turn=int(budgets["max_input_tokens_per_turn"]),
            max_output_tokens_per_turn=int(budgets["max_output_tokens_per_turn"]),
            turn_deadline_s=float(budgets["turn_deadline_s"]),
            max_input_tokens_per_session=int(budgets.get("max_input_tokens_per_session", 0)),
            max_input_tokens_per_day=int(budgets.get("max_input_tokens_per_day", 0)),
            context=_context(data.get("context") or {}, path),
            upgrade_specs=MappingProxyType({k: tuple(int(x) for x in v) for k, v in specs.items()}),
            notify_templates=_freeze(templates),
            source=str(path),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"{path.name}: invalid value ({exc})") from exc
    return policy


@dataclass(frozen=True)
class Thresholds:
    values: Mapping[str, Mapping[str, Any]]

    def get(self, name: str, key: str) -> Any:
        try:
            return self.values[name][key]
        except KeyError as exc:
            raise ConfigError(f"thresholds.yaml: missing {name}.{key}") from exc

    def describe(self, name: str) -> str:
        entry = self.values.get(name)
        if entry is None:
            raise ConfigError(f"thresholds.yaml: unknown definition '{name}'")
        params = {k: v for k, v in entry.items() if k != "description"}
        return str(entry["description"]).format(**params)

    def names(self) -> tuple[str, ...]:
        return tuple(self.values.keys())


def load_thresholds(path: Path) -> Thresholds:
    data = _load_yaml(path)
    for name, entry in data.items():
        if not isinstance(entry, dict) or "description" not in entry:
            raise ConfigError(f"{path.name}: '{name}' needs a description")
    thresholds = Thresholds(_freeze(data))
    for name in thresholds.names():
        thresholds.describe(name)  # validates format placeholders
    return thresholds


def load_blocklist(path: Path) -> tuple[str, ...]:
    data = _load_yaml(path)
    items = data.get("prohibited_software")
    if not isinstance(items, list) or not items:
        raise ConfigError(f"{path.name}: prohibited_software must be a non-empty list")
    return tuple(str(i).lower() for i in items)


@dataclass(frozen=True)
class LLMSettings:
    provider: str
    model: str
    base_url: str | None
    api_key_env: str
    temperature: float
    max_output_tokens: int
    call_timeout_s: float
    network_retries: int
    backoff_base_s: float
    repair_retries: int
    reasoning_effort: Mapping[str, str] = field(default_factory=dict)
    save_prompts: bool = True
    pricing: Mapping[str, Mapping[str, float]] = field(default_factory=dict)


SUPPORTED_PROVIDERS = ("anthropic", "openai", "openrouter")
REASONING_EFFORTS = ("", "minimal", "low", "medium", "high")
REASONING_STEPS = ("planner",)


def _reasoning_effort(data: Any) -> dict[str, str]:
    data = data or {}
    if not isinstance(data, dict):
        raise ConfigError("llm.yaml: reasoning_effort must be a mapping of step to effort")
    out: dict[str, str] = {}
    for step, effort in data.items():
        effort = str(effort or "").lower()
        if step not in REASONING_STEPS:
            raise ConfigError(f"llm.yaml: reasoning_effort has unknown step '{step}'. Use one of {REASONING_STEPS}.")
        if effort not in REASONING_EFFORTS:
            raise ConfigError(f"llm.yaml: reasoning_effort.{step} is '{effort}'. Use one of {REASONING_EFFORTS}.")
        out[step] = effort
    return out


def load_llm_settings(config_dir: Path) -> LLMSettings:
    data = _load_yaml(config_dir / "llm.yaml")
    pricing_path = config_dir / "pricing.yaml"
    pricing = (_load_yaml(pricing_path).get("models") or {}) if pricing_path.exists() else {}
    provider = os.environ.get("FLEET_LLM_PROVIDER") or data.get("provider", "openrouter")
    if provider not in SUPPORTED_PROVIDERS:
        raise ConfigError(f"Unsupported LLM provider '{provider}'. Use one of {SUPPORTED_PROVIDERS}.")
    model = os.environ.get("FLEET_LLM_MODEL") or (data.get("models") or {}).get(provider, "")
    return LLMSettings(
        provider=provider,
        model=model or "",
        base_url=(data.get("base_urls") or {}).get(provider),
        api_key_env=(data.get("api_key_env") or {}).get(provider, ""),
        temperature=float(data.get("temperature", 0)),
        max_output_tokens=int(data.get("max_output_tokens", 1500)),
        call_timeout_s=float(data.get("call_timeout_s", 30)),
        network_retries=int(data.get("network_retries", 3)),
        backoff_base_s=float(data.get("backoff_base_s", 1.0)),
        repair_retries=int(data.get("repair_retries", 2)),
        reasoning_effort=_freeze(_reasoning_effort(data.get("reasoning_effort"))),
        save_prompts=bool(data.get("save_prompts", True)),
        pricing=_freeze(pricing),
    )


@dataclass(frozen=True)
class AppConfig:
    policy: Policy
    thresholds: Thresholds
    blocklist: tuple[str, ...]


def load_app_config(config_dir: Path, policy_file: str = "policy.yaml") -> AppConfig:
    return AppConfig(
        policy=load_policy(config_dir / policy_file),
        thresholds=load_thresholds(config_dir / "thresholds.yaml"),
        blocklist=load_blocklist(config_dir / "blocklist.yaml"),
    )


def load_dotenv(path: Path) -> None:
    """Minimal .env reader: KEY=VALUE lines; existing environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value
