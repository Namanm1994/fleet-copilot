"""Toolkit: binds the session context to repository, store, policy, audit and tools.

The only way the agent touches data or creates proposals. Tools not listed in
policy.yaml are never exposed.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from fleet_copilot.audit import AuditLog
from fleet_copilot.config import AppConfig
from fleet_copilot.data.repository import Repository, open_readonly
from fleet_copilot.executor import Executor
from fleet_copilot.paths import Paths
from fleet_copilot.policy import PolicyEngine, dedup_key
from fleet_copilot.session import SessionContext, SessionEvidence
from fleet_copilot.store import AppStore
from fleet_copilot.tools.envelope import ToolError, ToolResult
from fleet_copilot.tools.read_tools import ReadTools
from fleet_copilot.tools.schemas import ACTION_ARGS, DESCRIPTIONS, READ_ARGS

ORIGINS = ("requested", "unprompted", "scan")


class Toolkit:
    def __init__(self, ctx: SessionContext, config: AppConfig, paths: Paths, repo: Repository | None = None):
        self.ctx = ctx
        self.config = config
        self.paths = paths
        self.repo = repo or Repository(open_readonly(paths.telemetry_db), ctx.tenant_id, ctx.as_of)
        if self.repo.tenant_id != ctx.tenant_id:
            raise RuntimeError("Repository tenant does not match the session")
        self.store = AppStore(paths.app_db, ctx.tenant_id)
        self.audit = AuditLog(paths.audit_log)
        self.evidence = SessionEvidence()
        self.read = ReadTools(self)
        self.policy_engine = PolicyEngine(self)
        self.executor = Executor(self)
        self.turn_counters: dict[str, int] = {"auto": 0}

    # ---- bookkeeping ---------------------------------------------------------------------------
    def new_turn(self) -> int:
        self.turn_counters = {"auto": 0}
        return self.evidence.new_turn()

    def audit_event(self, event: str, **fields: Any) -> None:
        self.audit.append(event, tenant=self.ctx.tenant_id, session_id=self.ctx.session_id,
                          operator=self.ctx.operator, **fields)

    # ---- exposure ------------------------------------------------------------------------------
    def tool_names(self) -> list[str]:
        """Only policy-listed tools. Forbidden actions are never included."""
        return [*self.config.policy.read_tools, *self.config.policy.action_tools]

    def tool_specs(self) -> list[dict[str, Any]]:
        specs = []
        for name in self.tool_names():
            model = READ_ARGS.get(name) or ACTION_ARGS[name]
            schema = model.model_json_schema()
            specs.append({"name": name, "description": DESCRIPTIONS[name], "parameters": schema})
        return specs

    # ---- dispatch ------------------------------------------------------------------------------
    def call(self, name: str, args: dict[str, Any] | None, origin: str = "requested") -> ToolResult:
        args = args or {}
        try:
            if name not in self.tool_names():
                raise ToolError("FORBIDDEN_ACTION", f"Tool '{name}' is not available.",
                                f"Available tools: {self.tool_names()}")
            if name in READ_ARGS:
                try:
                    parsed = READ_ARGS[name].model_validate(args)
                except ValidationError as exc:
                    err = exc.errors()[0]
                    raise ToolError("INVALID_ARGUMENT", f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}") from None
                return getattr(self.read, name)(parsed)
            return self.propose(name, args, origin)
        except ToolError as exc:
            return exc.result(self.repo.as_of)

    def propose(self, action: str, args: dict[str, Any], origin: str) -> ToolResult:
        if origin not in ORIGINS:
            raise ValueError(origin)
        parsed, pre, evidence_ids = self.policy_engine.check(action, args, origin)
        a = parsed.model_dump()
        device_id = a.get("device_id")
        if action == "acknowledge_finding":
            rec = self.evidence.get(a["insight_id"])
            device_id = rec.device_id if rec else None
        preview = self._preview(action, a)
        proposal = self.store.create(
            session_id=self.ctx.session_id, action=action, device_id=device_id, employee_id=a.get("employee_id"),
            params={k: v for k, v in a.items() if k != "evidence_ids"}, evidence_ids=evidence_ids, origin=origin,
            dedup_key=dedup_key(action, parsed), reason=pre.reason, preview=preview, warnings=pre.warnings,
        )
        self.turn_counters[action] = self.turn_counters.get(action, 0) + 1
        if origin != "requested":
            self.turn_counters["auto"] += 1
        self.evidence.add(proposal["proposal_id"], {"action": action, "status": "proposed"}, device_id)
        self.audit_event("proposed", proposal_id=proposal["proposal_id"], action=action, origin=origin,
                         evidence_ids=evidence_ids, reason=pre.reason)
        data = {"proposal_id": proposal["proposal_id"], "status": "proposed", "origin": origin, "action": action,
                "preview": preview, "reason": pre.reason, "warnings": pre.warnings,
                "note": "Awaiting human approval. Nothing has been executed."}
        return ToolResult(data=data, evidence_ids=[proposal["proposal_id"]], as_of=self.repo.as_of)

    def _preview(self, action: str, a: dict[str, Any]) -> str:
        dev = self.repo.device(a["device_id"]) if a.get("device_id") else None
        host = (dev or {}).get("hostname") or a.get("device_id")
        if action == "notify_employee":
            template = self.config.policy.notify_templates[a["template"]]
            kind = "laptop" if dev and dev["is_laptop"] else "device"
            return template.format(device_kind=kind, hostname=host, as_of=self.repo.as_of[:10],
                                   threshold=self.config.thresholds.get("low_disk", "free_pct_lt"),
                                   check_id=a.get("check_id") or "")
        return {
            "open_remediation_ticket": f"Open a remediation ticket for {host}: {a.get('check_id')} failing." + (f" Note: {a['note']}" if a.get("note") else ""),
            "flag_device_for_replacement": f"Flag {host} for replacement (battery end-of-life).",
            "create_upgrade_order": f"Request a {a.get('component')} upgrade to {a.get('spec_gb')} GB for {host}. Nothing is purchased automatically.",
            "request_software_removal": f"Ask IT to remove \"{a.get('software_name')}\" from {host}.",
            "acknowledge_finding": f"Suppress finding {a.get('insight_id')} until {a.get('until')}: {a.get('reason')}",
        }[action]
