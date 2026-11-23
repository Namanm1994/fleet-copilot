"""What produced a result: the model and the settings it ran with.

Recorded at the start of every session trace, in the audit log, in each recording and in the
eval report, so any answer or eval number can be tied to the model and limits behind it.
Never includes the API key, its variable name or request headers.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fleet_copilot.config import LLMSettings, Policy

SETTING_KEYS = ("provider", "model", "temperature", "max_output_tokens", "call_timeout_s", "network_retries",
                "repair_retries", "reasoning_effort", "max_input_tokens_per_turn", "max_output_tokens_per_turn",
                "turn_deadline_s", "prompt_version")


def fingerprint(settings: dict[str, Any]) -> str:
    """Short hash of the named settings. Two runs with the same fingerprint ran under the same settings."""
    body = {k: settings.get(k) for k in SETTING_KEYS}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:12]


def run_settings(settings: LLMSettings, policy: Policy, prompt_version: str, model: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {
        "provider": settings.provider,
        "model": model or settings.model,
        "temperature": settings.temperature,
        "max_output_tokens": settings.max_output_tokens,
        "call_timeout_s": settings.call_timeout_s,
        "network_retries": settings.network_retries,
        "repair_retries": settings.repair_retries,
        "reasoning_effort": dict(settings.reasoning_effort),
        "max_input_tokens_per_turn": policy.max_input_tokens_per_turn,
        "max_output_tokens_per_turn": policy.max_output_tokens_per_turn,
        "turn_deadline_s": policy.turn_deadline_s,
        "prompt_version": prompt_version,
    }
    out["fingerprint"] = fingerprint(out)
    return out


def changed_settings(previous: dict[str, Any] | None, current: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Settings that differ between two runs, as {name: {previous, current}}. Empty when either is unknown."""
    if not previous or not current:
        return {}
    return {k: {"previous": previous.get(k), "current": current.get(k)}
            for k in SETTING_KEYS if previous.get(k) != current.get(k)}
