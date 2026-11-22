"""Executes approved proposals. Only reachable from the CLI after a human decision.

Re-checks the precondition and tenant on current data, then appends one record to the
outbox (atomic, locked). A delivery failure leaves the proposal `failed` and retryable;
retries are idempotent by proposal ID.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

from fleet_copilot.audit import file_lock
from fleet_copilot.paths import touch_private
from fleet_copilot.store import now_iso

if TYPE_CHECKING:
    from fleet_copilot.toolkit import Toolkit

_FAIL_NEXT = {"armed": os.environ.get("FLEET_OUTBOX_FAIL_NEXT") == "1"}


class DeliveryError(RuntimeError):
    pass


def arm_failure() -> None:
    """Test hook: make the next outbox delivery fail once."""
    _FAIL_NEXT["armed"] = True


class Executor:
    def __init__(self, tk: Toolkit):
        self.tk = tk

    def _deliver(self, record: dict[str, Any]) -> None:
        if _FAIL_NEXT["armed"]:
            _FAIL_NEXT["armed"] = False
            raise DeliveryError("Simulated delivery failure (FLEET_OUTBOX_FAIL_NEXT)")
        path = self.tk.paths.outbox
        touch_private(path)
        with file_lock(path):
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())

    def _outbox_has(self, proposal_id: str) -> bool:
        path = self.tk.paths.outbox
        if not path.exists():
            return False
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and json.loads(line).get("proposal_id") == proposal_id:
                return True
        return False

    def approve(self, proposal_id: str, operator: str) -> dict[str, Any]:
        p = self.tk.store.get(proposal_id)
        if p is None:
            raise KeyError(proposal_id)  # other tenants' proposals are invisible
        if p["status"] == "executed":
            return p
        if p["status"] not in ("proposed", "pending", "failed"):
            raise ValueError(f"{proposal_id} is {p['status']}; it can't be approved.")
        if p["status"] != "failed":
            p = self.tk.store.update(proposal_id, status="approved", decided_at=now_iso(), decided_by=operator)
            self.tk.audit_event("approved", proposal_id=proposal_id, action=p["action"], origin=p["origin"],
                                evidence_ids=p["evidence_ids"])
        return self._execute(p, operator)

    def retry(self, proposal_id: str, operator: str) -> dict[str, Any]:
        p = self.tk.store.get(proposal_id)
        if p is None:
            raise KeyError(proposal_id)
        if p["status"] == "executed":
            return p  # idempotent no-op
        if p["status"] != "failed":
            raise ValueError(f"Only failed proposals can be retried; {proposal_id} is {p['status']}.")
        return self._execute(p, operator)

    def _execute(self, p: dict[str, Any], operator: str) -> dict[str, Any]:
        pid = p["proposal_id"]
        if self._outbox_has(pid):
            return self.tk.store.update(pid, status="executed", executed_at=now_iso(), error=None)
        pre = self.tk.policy_engine.precondition(p["action"], p["params"])
        if not pre.ok:
            updated = self.tk.store.update(pid, status="stale_precondition", error=pre.reason)
            self.tk.audit_event("stale_precondition", proposal_id=pid, action=p["action"], reason=pre.reason)
            return updated
        record = {
            "proposal_id": pid, "action": p["action"], "tenant": self.tk.ctx.tenant_id,
            "device_id": p["device_id"], "employee_id": p["employee_id"], "params": p["params"],
            "evidence_ids": p["evidence_ids"], "origin": p["origin"], "approved_by": operator,
            "executed_at": now_iso(), "status": "executed",
        }
        if p["action"] == "notify_employee":
            record["message"] = p["preview"]
        try:
            if p["action"] == "acknowledge_finding":
                self.tk.store.add_acknowledgement(p["params"]["insight_id"], p["params"]["reason"], p["params"]["until"], pid)
            self._deliver(record)
        except (DeliveryError, OSError, TimeoutError) as exc:
            updated = self.tk.store.update(pid, status="failed", error=str(exc))
            self.tk.audit_event("failed", proposal_id=pid, action=p["action"], reason=str(exc))
            return updated
        updated = self.tk.store.update(pid, status="executed", executed_at=record["executed_at"], error=None)
        self.tk.audit_event("executed", proposal_id=pid, action=p["action"], origin=p["origin"],
                            evidence_ids=p["evidence_ids"], detail={"approved_by": operator})
        return updated

    def reject(self, proposal_id: str, operator: str, reason: str = "") -> dict[str, Any]:
        p = self.tk.store.get(proposal_id)
        if p is None:
            raise KeyError(proposal_id)
        if p["status"] not in ("proposed", "pending"):
            raise ValueError(f"{proposal_id} is {p['status']}; it can't be rejected.")
        updated = self.tk.store.update(proposal_id, status="rejected", decided_at=now_iso(), decided_by=operator,
                                       reject_reason=reason[:200])
        self.tk.evidence.rejected_dedup_keys.add(p["dedup_key"])
        self.tk.audit_event("rejected", proposal_id=proposal_id, action=p["action"], reason=reason[:200])
        return updated

    def later(self, proposal_id: str, operator: str) -> dict[str, Any]:
        p = self.tk.store.get(proposal_id)
        if p is None:
            raise KeyError(proposal_id)
        if p["status"] != "proposed":
            return p
        self.tk.audit_event("deferred", proposal_id=proposal_id, action=p["action"])
        return self.tk.store.update(proposal_id, status="pending")
