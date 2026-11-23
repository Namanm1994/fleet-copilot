"""Session context (read-only, set before the agent runs) and per-session evidence."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from fleet_copilot.evidence import device_of, kind_of


@dataclass(frozen=True)
class SessionContext:
    """Fixed for the whole session. The tenant is never a tool argument and never editable by the LLM."""

    tenant_id: str
    tenant_name: str
    operator: str
    as_of: str
    session_id: str

    @staticmethod
    def new(tenant_id: str, tenant_name: str, operator: str, as_of: str) -> SessionContext:
        return SessionContext(tenant_id, tenant_name, operator, as_of, uuid.uuid4().hex[:12])


@dataclass
class EvidenceRecord:
    evidence_id: str
    kind: str
    device_id: str | None
    record: dict[str, Any]
    turn: int


def whole_numbers(obj: Any) -> set[int]:
    """Every whole number in a tool result, at any depth. Booleans are not numbers here."""
    if isinstance(obj, bool):
        return set()
    if isinstance(obj, int):
        return {obj}
    if isinstance(obj, float) and obj.is_integer():
        return {int(obj)}
    if isinstance(obj, dict):
        return set().union(*(whole_numbers(v) for v in obj.values())) if obj else set()
    if isinstance(obj, (list, tuple)):
        return set().union(*(whole_numbers(v) for v in obj)) if obj else set()
    return set()


@dataclass
class SessionEvidence:
    """Everything read tools returned in this session. Actions may only cite these IDs."""

    records: dict[str, EvidenceRecord] = field(default_factory=dict)
    receipts: dict[str, dict[str, Any]] = field(default_factory=dict)
    rejected_dedup_keys: set[str] = field(default_factory=set)
    turn: int = 0
    _receipt_seq: int = 0

    def add(self, evidence_id: str, record: dict[str, Any] | None = None, device_id: str | None = None) -> None:
        if evidence_id not in self.records:
            self.records[evidence_id] = EvidenceRecord(
                evidence_id, kind_of(evidence_id), device_id or device_of(evidence_id), record or {}, self.turn
            )

    def has(self, evidence_id: str) -> bool:
        return evidence_id in self.records or evidence_id in self.receipts

    def get(self, evidence_id: str) -> EvidenceRecord | None:
        return self.records.get(evidence_id)

    def for_device(self, device_id: str, kinds: tuple[str, ...] | None = None) -> list[EvidenceRecord]:
        return [r for r in self.records.values() if r.device_id == device_id and (kinds is None or r.kind in kinds)]

    def this_turn(self) -> list[EvidenceRecord]:
        return [r for r in self.records.values() if r.turn == self.turn]

    def add_receipt(self, tool: str, args: dict[str, Any], result_count: int, as_of: str,
                    evidence_ids: list[str], definitions_used: list[str] | None = None,
                    numbers: set[int] | None = None) -> str:
        self._receipt_seq += 1
        rid = f"Q-{self.turn}-{self._receipt_seq}"
        self.receipts[rid] = {"receipt_id": rid, "tool": tool, "args": args, "result_count": result_count,
                              "as_of": as_of, "evidence_ids": evidence_ids, "turn": self.turn,
                              "definitions_used": list(definitions_used or []),
                              # whole numbers the result itself reported (for example devices_checked), so a
                              # count in an answer can be checked against the result it cites
                              "numbers": sorted(numbers or ())}
        return rid

    def new_turn(self) -> int:
        self.turn += 1
        return self.turn
