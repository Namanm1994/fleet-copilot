"""Typed tool outputs: the `data` each tool returns.

Inputs were always validated (schemas.py). These models do the same for outputs, so the shape
the prompts and evals rely on is written down in one place. `tests/test_tool_contract.py` pins
the field names of inputs, outputs and the envelope; a rename fails that test until
TOOL_CONTRACT_VERSION is bumped and the snapshot is regenerated on purpose.

At runtime a mismatch is recorded (audit event `contract_violation`) and the result is still
returned, so a contract bug never takes the agent down. Tests run in strict mode and fail.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel

TOOL_CONTRACT_VERSION = "1"


class Out(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ---- resolve_entities ---------------------------------------------------------------------------
class ResolveMatch(Out):
    type: str
    device_id: str | None = None
    label: str | None = None
    employee_id: str | None = None


class ResolvedRef(Out):
    ref: str
    status: str
    matches: list[ResolveMatch]
    employee_ids: list[str] = Field(default_factory=list)


class ResolveEntitiesOut(RootModel[list[ResolvedRef]]):
    pass


# ---- find_devices -------------------------------------------------------------------------------
class DeviceRow(Out):
    device_id: str
    hostname: str | None
    model: str | None
    employee_id: str | None
    os: str
    last_seen: str
    stale: bool
    matched_values: dict[str, Any]  # the values the filters matched on; keys depend on the criteria


class DeviceGroup(Out):
    count: int
    device_ids: list[str]


class FindDevicesOut(Out):
    total: int
    devices: list[DeviceRow] | None = None
    groups: dict[str, DeviceGroup] | None = None


# ---- get_device ---------------------------------------------------------------------------------
class DeviceCheck(Out):
    check_id: str
    status: str
    severity: str | None
    evidence_id: str


class OpenProposal(Out):
    proposal_id: str
    action: str
    status: str


class GetDeviceOut(Out):
    device_id: str
    hostname: str | None
    model: str | None
    manufacturer: str | None
    is_laptop: bool
    employee_id: str | None
    os: str
    ram_total_gb: float | None
    ram_used_pct: float | None
    disk_total_gb: float | None
    disk_free_gb: float | None
    disk_free_pct: float | None
    battery_condition: str | None
    battery_cycle_count: int | None
    compliance: list[DeviceCheck]
    software_count: int
    first_seen: str
    last_seen: str
    stale: bool
    snapshot_id: str
    open_proposals: list[OpenProposal]


# ---- get_device_history -------------------------------------------------------------------------
class HistoryPoint(Out):
    date: str
    value: Any
    evidence_id: str


class GetDeviceHistoryOut(Out):
    device_id: str
    window: list[str | None]
    series: dict[str, list[HistoryPoint]]


# ---- get_compliance_overview --------------------------------------------------------------------
class FailingDevice(Out):
    device_id: str
    hostname: str | None
    evidence_id: str


class CheckOverview(Out):
    check_id: str
    severity: str | None
    passing: int = Field(alias="pass")
    failing: int = Field(alias="fail")
    failing_devices: list[FailingDevice]


class GetComplianceOverviewOut(Out):
    as_of_latest_snapshot_per_device: bool
    checks: list[CheckOverview]


# ---- get_software -------------------------------------------------------------------------------
class SoftwareItem(Out):
    name: str
    version: str | None
    prohibited: bool
    evidence_id: str
    device_id: str | None = None      # present when searching by name
    first_seen: str | None = None     # present when searching by name


class GetSoftwareOut(Out):
    device_id: str | None = None
    software: list[SoftwareItem] | None = None
    query: str | None = None
    installed_on: list[SoftwareItem] | None = None


# ---- run_insights -------------------------------------------------------------------------------
class InsightRow(Out):
    insight_id: str
    detector: str
    device_id: str
    hostname: str | None
    finding: str
    explanation: str
    severity: str
    evidence_ids: list[str]
    caveats: list[str]
    definitions_used: list[str]
    eligible_actions: list[str]
    stale: bool
    data: dict[str, Any]              # detector-specific details
    acknowledged_until: str | None = None


class RunInsightsOut(Out):
    detectors_run: list[str]
    devices_checked: dict[str, int]
    insights: list[InsightRow]
    insufficient_data_devices: Any


# ---- get_definitions, list_proposals ------------------------------------------------------------
class GetDefinitionsOut(RootModel[dict[str, str]]):
    pass


class ProposalRow(Out):
    proposal_id: str
    action: str
    device_id: str | None
    employee_id: str | None
    status: str
    origin: str
    reason: str | None
    reject_reason: str | None


class ListProposalsOut(RootModel[list[ProposalRow]]):
    pass


# ---- action tools (all return the same shape) -----------------------------------------------------
class ProposalOut(Out):
    proposal_id: str
    status: str
    origin: str
    action: str
    preview: str
    reason: str | None
    warnings: list[str]
    note: str


READ_OUTPUTS: dict[str, type[BaseModel]] = {
    "resolve_entities": ResolveEntitiesOut, "find_devices": FindDevicesOut, "get_device": GetDeviceOut,
    "get_device_history": GetDeviceHistoryOut, "get_compliance_overview": GetComplianceOverviewOut,
    "get_software": GetSoftwareOut, "run_insights": RunInsightsOut, "get_definitions": GetDefinitionsOut,
    "list_proposals": ListProposalsOut,
}
ACTION_OUTPUT = ProposalOut


def output_model(tool: str) -> type[BaseModel]:
    return READ_OUTPUTS.get(tool, ACTION_OUTPUT)
