"""Policy checks for action proposals, in a fixed order, enforced in code.

1  tool enabled in policy.yaml
2  arguments valid (schema)
3  target belongs to the session tenant (else NOT_FOUND, existence never confirmed)
4  evidence was returned in this session, is about the target, and is the right kind
5  precondition true on the latest data (and freshness: stale devices)
6  no duplicate open proposal
7  not rejected earlier this session
8  unprompted proposals: per-turn cap
9  batch cap per action per turn
Action-specific checks (upgrade allowlist, notify template, ticket note, 90-day limit) run in 5.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from fleet_copilot.data.sanitize import clean_text, normalize_name
from fleet_copilot.insights.detectors import Detectors
from fleet_copilot.tools.envelope import ToolError
from fleet_copilot.tools.schemas import ACTION_ARGS

if TYPE_CHECKING:
    from fleet_copilot.toolkit import Toolkit

SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1}
TEMPLATE_CHECK = {"screen_lock_required": "screen_lock", "os_update_required": "os_up_to_date"}


@dataclass
class Precondition:
    ok: bool
    reason: str
    warnings: list[str] = field(default_factory=list)


def dedup_key(action: str, args: BaseModel) -> str:
    a = args.model_dump()
    detail = {
        "open_remediation_ticket": a.get("check_id"),
        "flag_device_for_replacement": a.get("reason"),
        "create_upgrade_order": a.get("component"),
        "notify_employee": f"{a.get('template')}:{a.get('check_id') or ''}",
        "request_software_removal": normalize_name(a.get("software_name")),
        "acknowledge_finding": a.get("insight_id"),
    }[action]
    target = a.get("device_id") or a.get("insight_id")
    if action == "notify_employee":
        target = f"{a.get('employee_id')}/{a.get('device_id')}"
    return f"{action}|{target}|{detail}"


class PolicyEngine:
    def __init__(self, tk: Toolkit):
        self.tk = tk

    @property
    def policy(self):
        return self.tk.config.policy

    # ---- preconditions (also used by the executor's re-check) ------------------------------
    def precondition(self, action: str, a: dict[str, Any], repo=None) -> Precondition:
        repo = repo or self.tk.repo
        dev = a.get("device_id")
        if action == "acknowledge_finding":
            until = date.fromisoformat(a["until"])
            as_of = date.fromisoformat(repo.as_of[:10])
            max_days = int(self.tk.config.thresholds.get("acknowledge_max_days", "days"))
            if until <= as_of:
                return Precondition(False, "The 'until' date must be after the as-of date.")
            if until > as_of + timedelta(days=max_days):
                return Precondition(False, f"Acknowledgements last at most {max_days} days (until {as_of + timedelta(days=max_days)}).")
            return Precondition(True, f"Suppress {a['insight_id']} until {a['until']}.")

        snap = repo.latest_snapshot(dev)
        if snap is None:
            return Precondition(False, f"No telemetry for {dev} up to as-of.")
        warnings = []
        stale_days = int(self.tk.config.thresholds.get("stale_device", "days"))
        cutoff = repo.stale_cutoff(stale_days)
        if snap["collected_at"] < cutoff:
            warnings.append(f"Device is stale: last snapshot {snap['day']}; the finding may be out of date.")

        if action == "open_remediation_ticket":
            row = next((r for r in repo.compliance_latest() if r["device_id"] == dev and r["check_id"] == a["check_id"]), None)
            if row is None:
                return Precondition(False, f"No '{a['check_id']}' result on the latest snapshot of {dev}.")
            if row["status"] == "pass":
                return Precondition(False, f"'{a['check_id']}' passes on the latest snapshot ({snap['day']}); no ticket needed.")
            return Precondition(True, f"{a['check_id']} ({row['severity']}) failing on {snap['day']}", warnings)

        if action == "flag_device_for_replacement":
            cond = snap["battery_condition"]
            if not cond or cond.lower() == "normal":
                return Precondition(False, f"Battery condition is '{cond or 'no battery'}' on {snap['day']}; replacement needs a condition other than Normal.")
            return Precondition(True, f"Battery condition \"{cond}\" on {snap['day']}", warnings)

        if action == "create_upgrade_order":
            kind = "ram_gb" if a["component"] == "ram" else "storage_gb"
            allowed = self.policy.upgrade_specs.get(kind, ())
            current = snap["ram_total_gb"] if a["component"] == "ram" else snap["disk_total_gb"]
            if a["spec_gb"] not in allowed:
                return Precondition(False, f"{a['spec_gb']} GB is not an allowed {a['component']} spec. Allowed: {list(allowed)} GB.")
            if current is not None and a["spec_gb"] <= current + 0.5:
                return Precondition(False, f"{a['spec_gb']} GB is not larger than the current {current:.0f} GB.")
            det = Detectors(repo, self.tk.config)
            if a["component"] == "ram":
                hits = [i for i in det.ram_constrained({dev}).insights if i.data.get("level") == "persistent"]
                need = "a persistent RAM finding (ram_persistent)"
            else:
                hits = [i for i in det.low_storage({dev}).insights if i.data.get("persistent")]
                need = "a persistent low-storage finding (low_disk_persistent)"
            if not hits:
                return Precondition(False, f"An upgrade needs {need}; {dev} doesn't have one.")
            return Precondition(True, f"{hits[0].finding}", warnings)

        if action == "request_software_removal":
            q = normalize_name(a["software_name"])
            if not any(b in q for b in self.tk.config.blocklist):
                return Precondition(False, f"'{a['software_name']}' is not on the prohibited-software list.")
            present = [r for r in repo.software_latest(dev) if r["name_norm"] == q or q in r["name_norm"]]
            if not present:
                return Precondition(False, f"'{a['software_name']}' is not installed on the latest snapshot of {dev}.")
            return Precondition(True, f"\"{present[0]['name']}\" installed on {snap['day']}", warnings)

        if action == "notify_employee":
            device = repo.device(dev)
            if device is None or (device["employee_id"] or "") != a["employee_id"]:
                return Precondition(False, f"{a['employee_id']} is not the assigned user of {dev}.")
            t = a["template"]
            thr = float(self.tk.config.thresholds.get("low_disk", "free_pct_lt"))
            checks = {r["check_id"]: r for r in repo.compliance_latest() if r["device_id"] == dev}
            if t == "low_disk":
                ok = snap["disk_free_pct"] is not None and snap["disk_free_pct"] < thr
                return Precondition(ok, f"{snap['disk_free_pct']}% free" if ok else "Device is not low on disk.", warnings)
            if t in TEMPLATE_CHECK:
                row = checks.get(TEMPLATE_CHECK[t])
                ok = bool(row) and row["status"] == "fail"
                return Precondition(ok, f"{TEMPLATE_CHECK[t]} failing" if ok else f"{TEMPLATE_CHECK[t]} is not failing.", warnings)
            if t == "compliance_issue":
                row = checks.get(a.get("check_id") or "")
                ok = bool(row) and row["status"] == "fail"
                return Precondition(ok, f"{a.get('check_id')} failing" if ok else "That check is not failing (template needs check_id).", warnings)
            if t == "prohibited_software":
                bl = self.tk.config.blocklist
                ok = any(any(b in r["name_norm"] for b in bl) for r in repo.software_latest(dev))
                return Precondition(ok, "Prohibited software present" if ok else "No prohibited software on the device.", warnings)
            if t == "battery_replacement":
                cond = snap["battery_condition"]
                ok = bool(cond) and cond.lower() != "normal"
                return Precondition(ok, f"Battery \"{cond}\"" if ok else "Battery condition is Normal.", warnings)
            if t == "upgrade_scheduled":
                ok = any(p["action"] == "create_upgrade_order" and p["status"] in ("approved", "executed")
                         for p in self.tk.store.list(device_id=dev))
                return Precondition(ok, "Upgrade request exists" if ok else "No approved upgrade request for this device.", warnings)
            return Precondition(False, f"Unknown template '{t}'.")
        return Precondition(False, f"Unknown action '{action}'.")

    # ---- full check sequence ---------------------------------------------------------------
    def check(self, action: str, raw_args: dict[str, Any], origin: str) -> tuple[BaseModel, Precondition, list[str]]:
        tk = self.tk
        # 1 enabled
        if action in self.policy.forbidden_actions or action not in self.policy.action_tools:
            raise ToolError("FORBIDDEN_ACTION", f"'{action}' is not an allowed action.",
                            f"Allowed actions: {list(self.policy.action_tools)}")
        # 2 schema
        try:
            args = ACTION_ARGS[action].model_validate(raw_args)
        except ValidationError as exc:
            raise ToolError("INVALID_ARGUMENT", f"Invalid arguments for {action}: {exc.errors()[0]['msg']}",
                            "Fix the arguments and try again.") from None
        a = args.model_dump()
        # 3 target in tenant
        dev = a.get("device_id")
        if action == "acknowledge_finding":
            rec = tk.evidence.get(a["insight_id"])
            if rec is None or rec.kind != "insight":
                raise ToolError("PRECONDITION_FAILED", f"Insight {a['insight_id']} was not returned in this session.",
                                "Run run_insights first and use an insight_id from its result.")
            dev = rec.device_id
        if dev and tk.repo.device(dev) is None:
            tk.audit_event("target_not_found", action=action, origin=origin, detail={"device_id": dev})
            raise ToolError("NOT_FOUND", f"No device '{dev}' in this tenant.", "Use resolve_entities or find_devices.")
        if action == "notify_employee" and not tk.repo.employee_devices(a["employee_id"]):
            tk.audit_event("target_not_found", action=action, origin=origin, detail={"employee_id": a["employee_id"]})
            raise ToolError("NOT_FOUND", f"No employee '{a['employee_id']}' in this tenant.")
        # 4 evidence: from this session, about this target, of a supporting kind
        evidence_ids = list(a.get("evidence_ids") or [a.get("insight_id")])
        missing = [e for e in evidence_ids if not tk.evidence.has(e)]
        if missing:
            raise ToolError("PRECONDITION_FAILED", f"Evidence not retrieved in this session: {missing}.",
                            "Look the device up first; cite only evidence IDs that tools returned.")
        about_target = [e for e in evidence_ids if tk.evidence.get(e) and tk.evidence.get(e).device_id == dev]
        if not about_target:
            raise ToolError("PRECONDITION_FAILED", f"None of the cited evidence is about {dev}.",
                            "Cite evidence for the same device the action targets.")
        if action == "open_remediation_ticket":
            ok = any((r := tk.evidence.get(e)) and ((r.kind == "check" and r.record.get("check_id") == a["check_id"])
                                                   or (r.kind == "insight" and r.record.get("data", {}).get("check_id") == a["check_id"]))
                     for e in about_target)
            if not ok:
                raise ToolError("PRECONDITION_FAILED", f"Cite the {a['check_id']} check result (or drift insight) for {dev}.")
        # note hygiene for tickets (F-21)
        if action == "open_remediation_ticket" and a.get("note"):
            note = clean_text(a["note"], 300) or ""
            foreign = [m for m in re.findall(r"\b[A-Z0-9]{8,}\b|emp-[a-z0-9-]+", note)
                       if m != dev and m != (tk.repo.device(dev) or {}).get("employee_id")]
            if foreign:
                raise ToolError("INVALID_ARGUMENT", "The ticket note may not name other devices or people.",
                                "Describe only this device's issue.")
            raw_args = {**raw_args, "note": note}
            args = ACTION_ARGS[action].model_validate(raw_args)
            a = args.model_dump()
        if action == "notify_employee" and a["template"] not in self.policy.notify_templates:
            raise ToolError("INVALID_ARGUMENT", f"Unknown template '{a['template']}'.",
                            f"Templates: {list(self.policy.notify_templates)}")
        # 5 precondition and freshness
        pre = self.precondition(action, a)
        if not pre.ok:
            tk.audit_event("refused", action=action, origin=origin, detail={"code": "PRECONDITION_FAILED", "reason": pre.reason, "args": a})
            raise ToolError("PRECONDITION_FAILED", pre.reason, "Explain to the admin what the data shows instead.")
        if pre.warnings and origin != "requested":
            raise ToolError("PRECONDITION_FAILED", f"Not proposed automatically: {pre.warnings[0]}")
        # 6 duplicate
        key = dedup_key(action, args)
        existing = tk.store.find_open(key)
        if existing:
            raise ToolError("DUPLICATE", f"An open proposal already exists: {existing['proposal_id']} ({existing['status']}).",
                            "Point the admin to the existing proposal.")
        # 7 rejected this session
        if key in tk.evidence.rejected_dedup_keys:
            raise ToolError("REJECTED_THIS_SESSION", "The admin rejected this proposal earlier in this session.",
                            "Don't propose it again; mention the rejection if relevant.")
        # 8 auto-proposal cap
        if origin in ("unprompted", "scan") and tk.turn_counters["auto"] >= self.policy.max_auto_proposals_per_turn:
            raise ToolError("AUTO_PROPOSAL_CAP", "Per-turn cap for unprompted proposals reached.")
        # 9 batch cap
        if tk.turn_counters.get(action, 0) >= self.policy.max_devices_per_batch:
            raise ToolError("BATCH_LIMIT", f"At most {self.policy.max_devices_per_batch} {action} proposals per turn.",
                            "Propose a scoped, prioritized subset and say so.")
        return args, pre, evidence_ids
