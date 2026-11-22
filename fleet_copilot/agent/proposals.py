"""Unprompted proposals, created in code (gap F-04), not by the LLM.

Eligibility comes from all evidence gathered in the turn (F-05). Only actions listed in
policy.auto_propose_rules qualify, never notify_employee or acknowledge_finding; tickets
only for high-severity checks (F-06); stale devices never (F-20). Highest severity first,
capped per turn by the policy check sequence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fleet_copilot.insights.detectors import SEVERITY_RANK
from fleet_copilot.tools.read_tools import eligible_actions_from_evidence

if TYPE_CHECKING:
    from fleet_copilot.toolkit import Toolkit


def candidates(tk: Toolkit, records: list | None = None) -> list[dict[str, Any]]:
    policy = tk.config.policy
    if not policy.auto_propose:
        return []
    rules = policy.auto_propose_rules
    out = []
    for e in eligible_actions_from_evidence(tk, records if records is not None else tk.evidence.this_turn()):
        rule = rules.get(e["action"])
        if rule is None or e.get("stale"):
            continue
        min_sev = rule.get("min_severity")
        if min_sev and SEVERITY_RANK.get(e.get("severity", "low"), 0) < SEVERITY_RANK[min_sev]:
            continue
        out.append(e)
    out.sort(key=lambda e: -SEVERITY_RANK.get(e.get("severity", "low"), 0))
    return out


def auto_propose(tk: Toolkit, origin: str = "unprompted", records: list | None = None) -> tuple[list[dict], list[dict]]:
    """Returns (created proposals, skipped candidates with the reason)."""
    created, skipped = [], []
    for e in candidates(tk, records):
        args = {**e["params"], "evidence_ids": e["evidence_ids"]}
        res = tk.call(e["action"], args, origin=origin)
        if res.ok:
            created.append(res.data | {"device_id": e["params"].get("device_id")})
        else:
            skipped.append({"action": e["action"], "device_id": e["params"].get("device_id"),
                            "code": res.error.code, "message": res.error.message})
    return created, skipped
