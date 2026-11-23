"""`fleet scan`: a fixed workflow in code (no planner, no ReAct).

run all detectors → policy checks on eligible actions (same caps as chat) → optional LLM
summary, verified against the findings → briefing → approval prompt (CLI) → exit.
Without --summary the scan makes no LLM call and is fully reproducible.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from fleet_copilot.agent.proposals import auto_propose
from fleet_copilot.agent.verifier import DEVICE_RE, EMP_RE, _matches, _numbers
from fleet_copilot.insights.detectors import DETECTORS
from fleet_copilot.toolkit import Toolkit

DETECTOR_LABELS = {
    "battery_eol": "Battery end-of-life",
    "ram_constrained": "Memory pressure",
    "low_storage": "Low storage",
    "compliance_drift": "Compliance drift",
    "prohibited_software": "Prohibited software",
}


class ScanSummary(BaseModel):
    summary: str
    device_ids: list[str] = []


@dataclass
class ScanResult:
    tenant_name: str
    as_of: str
    insights: list[dict[str, Any]]
    clean_detectors: list[str]
    insufficient: dict[str, Any]
    definitions_used: list[str]
    proposals: list[dict[str, Any]]
    skipped: list[dict[str, Any]]
    receipt_id: str | None
    summary: str | None = None
    summary_note: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)

    def findings_for_llm(self) -> list[dict[str, Any]]:
        return [{k: i[k] for k in ("device_id", "detector", "severity", "finding")} for i in self.insights]


def run_scan(tk: Toolkit) -> ScanResult:
    tk.new_turn()
    tk.audit_event("scan_started")
    res = tk.call("run_insights", {"detectors": list(DETECTORS)}, origin="scan")
    if not res.ok:
        raise RuntimeError(res.error.message if res.error else "scan failed")
    data = res.data
    found = {i["detector"] for i in data["insights"]}
    clean = [d for d in data["detectors_run"] if d not in found]
    proposals, skipped = auto_propose(tk, origin="scan")
    proposals = [tk.store.get(p["proposal_id"]) for p in proposals]
    tk.audit_event("scan_finished", detail={"insights": len(data["insights"]), "proposals": len(proposals)})
    return ScanResult(tenant_name=tk.ctx.tenant_name, as_of=tk.repo.as_of, insights=data["insights"],
                      clean_detectors=clean, insufficient=data["trend_not_assessed"],
                      definitions_used=res.definitions_used, proposals=proposals, skipped=skipped,
                      receipt_id=res.receipt_id)


def verify_summary(s: ScanSummary, result: ScanResult) -> list[str]:
    """Same spirit as the chat verifier: every ID and number in the summary must come from the findings."""
    allowed_devices = {i["device_id"] for i in result.insights}
    corpus = json.dumps(result.insights, ensure_ascii=False)
    errors = []
    for token in set(DEVICE_RE.findall(s.summary)) | set(s.device_ids) | set(EMP_RE.findall(s.summary)):
        if token not in allowed_devices and token not in corpus:
            errors.append(f"'{token}' is not in the findings")
    allowed_nums = set(_numbers(corpus)) | {float(len(result.insights)), float(len(result.proposals))}
    allowed_nums |= {float(sum(1 for i in result.insights if i["severity"] == sev)) for sev in ("low", "medium", "high")}
    for x in _numbers(s.summary):
        if not _matches(x, allowed_nums):
            errors.append(f"number {x:g} is not in the findings")
    return errors


def summarize(result: ScanResult, sess_llm, prompt_template: str, repair_retries: int, on_event) -> None:
    """Fill result.summary from the LLM, or leave it empty with a note. Never blocks the briefing."""
    from fleet_copilot.llm.base import BudgetExceeded
    from fleet_copilot.llm.retry import LLMCallError
    from fleet_copilot.llm.structured import RepairFailed, parse_with_repair

    if not result.insights:
        result.summary = "No issues found by any detector."
        return
    prompt = prompt_template.format(findings=json.dumps(result.findings_for_llm(), ensure_ascii=False))
    messages = [{"role": "user", "content": prompt}]
    try:
        s, _ = parse_with_repair(lambda m: sess_llm(m, None), messages, ScanSummary, repair_retries, on_event)
    except (RepairFailed, BudgetExceeded, LLMCallError) as exc:
        result.summary_note = f"Summary skipped ({exc.__class__.__name__}); findings below are complete."
        return
    errors = verify_summary(s, result)
    on_event("verifier_result", {"ok": not errors, "errors": errors})
    if errors:
        result.summary_note = "Summary skipped: it did not match the findings. Findings below are complete."
        return
    result.summary = s.summary


def render_scan(result: ScanResult, thresholds) -> str:
    lines = [f"Fleet scan · {result.tenant_name} · as of {result.as_of[:10]}", ""]
    if result.summary:
        lines += [result.summary, ""]
    if result.summary_note:
        lines += [result.summary_note, ""]
    for sev in ("high", "medium", "low"):
        items = [i for i in result.insights if i["severity"] == sev]
        if not items:
            continue
        lines.append(f"{sev.upper()} ({len(items)})")
        for i in items:
            host = f" ({i['hostname']})" if i.get("hostname") else ""
            lines.append(f"  • {i['device_id']}{host}: {i['finding']}")
            lines.append(f"      {i['insight_id']} · evidence: {', '.join(i['evidence_ids'][:3])}"
                         + (f" +{len(i['evidence_ids']) - 3} more" if len(i["evidence_ids"]) > 3 else ""))
            for c in i.get("caveats") or []:
                lines.append(f"      caveat: {c}")
        lines.append("")
    for d in result.clean_detectors:
        lines.append(f"✓ {DETECTOR_LABELS.get(d, d)}: no issues found")
    if result.insufficient:
        n = max(v["count"] for v in result.insufficient.values())
        lines.append(f"Too few snapshots for a trend on {n} device(s) (a trend needs "
                     f"{thresholds.get('min_points_for_trend', 'points')}). Their latest snapshot was still checked.")
    if result.definitions_used:
        lines += ["", "Definitions:"] + [f"  {d}: {thresholds.describe(d)}" for d in result.definitions_used
                                         if d in thresholds.names()]
    if result.proposals:
        lines += ["", "Proposed actions (awaiting your approval; nothing has been executed):"]
        for p in result.proposals:
            lines.append(f"  {p['proposal_id']}: {p['preview']}")
    capped = [s for s in result.skipped if s["code"] == "AUTO_PROPOSAL_CAP"]
    dupes = [s for s in result.skipped if s["code"] == "DUPLICATE"]
    if capped:
        lines.append(f"  {len(capped)} more eligible action(s) not proposed (cap per run); run the scan again after deciding.")
    if dupes:
        lines.append(f"  {len(dupes)} eligible action(s) already have open proposals (see `fleet proposals list`).")
    return "\n".join(lines).rstrip() + "\n"
