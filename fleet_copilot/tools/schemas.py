"""Typed tool inputs. Enum-constrained where possible; no free-form query strings.

The tenant is never an argument: it comes from the session context.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ID_PATTERN = r"^[A-Za-z0-9._:-]{1,64}$"
DETECTOR = Literal["battery_eol", "ram_constrained", "low_storage", "compliance_drift", "prohibited_software"]
SEVERITY = Literal["high", "medium", "low"]
TEMPLATE = Literal["low_disk", "os_update_required", "prohibited_software", "screen_lock_required",
                   "compliance_issue", "battery_replacement", "upgrade_scheduled"]
HISTORY_METRICS = ("disk_free_pct", "disk_free_gb", "ram_used_pct", "battery_condition", "battery_cycle_count",
                   "battery_full_charge_capacity", "os_version")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_id(value: str) -> str:
    if not re.match(ID_PATTERN, value):
        raise ValueError("invalid identifier")
    return value


def _check_date(value: str | None) -> str | None:
    if value is None:
        return value
    date.fromisoformat(value[:10])
    return value[:10]


# ---- read tools -------------------------------------------------------------------------------
class ResolveEntitiesArgs(Strict):
    refs: list[str] = Field(..., min_length=1, max_length=10,
                            description="Words the admin used for devices or people, e.g. 'the Dell', 'acme-macbook-2', 'emp-acme-1001'.")


class OsOlderThan(Strict):
    platform: Literal["darwin", "win32", "macos", "windows"]
    version: str = Field(..., description="e.g. '15' or '14.5' for macOS, '11 23H2' for Windows")


class DeviceCriteria(Strict):
    device_ids: list[str] | None = None
    employee_id: str | None = None
    platform: Literal["darwin", "win32"] | None = None
    manufacturer: str | None = Field(None, description="e.g. Apple, Dell, Lenovo")
    model_contains: str | None = None
    is_laptop: bool | None = None
    os_older_than: OsOlderThan | None = Field(None, description="Only devices of that platform are compared; others are excluded and reported.")
    low_disk: bool | None = Field(None, description="Named threshold: system volume below the configured free %.")
    disk_free_pct_lt: float | None = Field(None, ge=0, le=100)
    disk_free_gb_lt: float | None = Field(None, ge=0)
    ram_used_pct_gte: float | None = Field(None, ge=0, le=100)
    failing_check: str | None = Field(None, description="Compliance check ID failing on the latest snapshot")
    failing_severity: SEVERITY | None = Field(None, description="Any check of this severity failing on the latest snapshot")
    has_software: str | None = None
    has_prohibited_software: bool | None = None
    battery_not_normal: bool | None = None
    stale: bool | None = None


class FindDevicesArgs(Strict):
    criteria: DeviceCriteria = Field(default_factory=DeviceCriteria)
    group_by: Literal["platform", "manufacturer", "model", "os_version", "employee"] | None = None
    sort_by: Literal["disk_free_pct", "ram_used_pct", "last_seen", "device_id"] | None = None
    limit: int = Field(25, ge=1, le=25)
    offset: int = Field(0, ge=0, description="Skip this many matches; use the next_offset of the previous result to page.")


class GetDeviceArgs(Strict):
    device_id: str

    _v = field_validator("device_id")(_check_id)


class GetDeviceHistoryArgs(Strict):
    device_id: str
    metrics: list[str] = Field(..., min_length=1, max_length=4,
                               description=f"Any of {list(HISTORY_METRICS)} or 'compliance:<check_id>'")
    start: str | None = Field(None, description="YYYY-MM-DD")
    end: str | None = Field(None, description="YYYY-MM-DD")

    _v = field_validator("device_id")(_check_id)
    _d = field_validator("start", "end")(_check_date)

    @field_validator("metrics")
    @classmethod
    def _metrics(cls, values: list[str]) -> list[str]:
        for m in values:
            if m not in HISTORY_METRICS and not (m.startswith("compliance:") and re.match(ID_PATTERN, m[11:])):
                raise ValueError(f"unknown metric '{m}'")
        return values


class GetComplianceOverviewArgs(Strict):
    check_id: str | None = None
    severity: SEVERITY | None = None


class GetSoftwareArgs(Strict):
    name: str | None = Field(None, description="Software name to search for (case-insensitive)")
    device_id: str | None = Field(None, description="List the software on this device")


class RunInsightsArgs(Strict):
    detectors: list[DETECTOR] | None = Field(None, description="Omit to run all detectors")
    device_ids: list[str] | None = None
    include_acknowledged: bool = False


class GetDefinitionsArgs(Strict):
    names: list[str] | None = None


class ListProposalsArgs(Strict):
    status: Literal["proposed", "pending", "executed", "failed", "rejected", "stale_precondition"] | None = None
    device_id: str | None = None


# ---- action tools -----------------------------------------------------------------------------
class EvidenceArgs(Strict):
    evidence_ids: list[str] = Field(..., min_length=1, max_length=20,
                                    description="Evidence IDs returned by read tools earlier in this session")


class OpenRemediationTicketArgs(EvidenceArgs):
    device_id: str
    check_id: str
    note: str = Field("", max_length=300, description="Short context for IT; must not name other devices or people")
    _v = field_validator("device_id", "check_id")(_check_id)


class FlagDeviceForReplacementArgs(EvidenceArgs):
    device_id: str
    reason: Literal["battery_eol"] = "battery_eol"
    _v = field_validator("device_id")(_check_id)


class CreateUpgradeOrderArgs(EvidenceArgs):
    device_id: str
    component: Literal["ram", "storage"]
    spec_gb: int = Field(..., description="Target capacity in GB, from the allowed upgrade list")
    _v = field_validator("device_id")(_check_id)


class NotifyEmployeeArgs(EvidenceArgs):
    employee_id: str
    device_id: str
    template: TEMPLATE
    check_id: str | None = Field(None, description="Required for the compliance_issue template")
    _v = field_validator("employee_id", "device_id")(_check_id)


class RequestSoftwareRemovalArgs(EvidenceArgs):
    device_id: str
    software_name: str = Field(..., max_length=100)
    _v = field_validator("device_id")(_check_id)


class AcknowledgeFindingArgs(Strict):
    insight_id: str
    reason: str = Field(..., min_length=3, max_length=200)
    until: str = Field(..., description="YYYY-MM-DD, at most 90 days after as-of")
    _d = field_validator("until")(_check_date)


READ_ARGS = {
    "resolve_entities": ResolveEntitiesArgs,
    "find_devices": FindDevicesArgs,
    "get_device": GetDeviceArgs,
    "get_device_history": GetDeviceHistoryArgs,
    "get_compliance_overview": GetComplianceOverviewArgs,
    "get_software": GetSoftwareArgs,
    "run_insights": RunInsightsArgs,
    "get_definitions": GetDefinitionsArgs,
    "list_proposals": ListProposalsArgs,
}
ACTION_ARGS = {
    "open_remediation_ticket": OpenRemediationTicketArgs,
    "flag_device_for_replacement": FlagDeviceForReplacementArgs,
    "create_upgrade_order": CreateUpgradeOrderArgs,
    "notify_employee": NotifyEmployeeArgs,
    "request_software_removal": RequestSoftwareRemovalArgs,
    "acknowledge_finding": AcknowledgeFindingArgs,
}

DESCRIPTIONS = {
    "resolve_entities": "Map the admin's words for devices or employees ('the Dell', a hostname, a serial, an employee ID) to IDs. Returns 'ambiguous' with candidates when several match.",
    "find_devices": "Main query tool. Filter devices on their latest snapshot (platform, OS older than, low disk, RAM, failing checks, software, battery, stale) and optionally group or count. Returns matched values and evidence IDs. Lists come 25 at a time: when the result has next_offset, call again with that offset for the next page.",
    "get_device": "Latest-snapshot summary of one device: OS, RAM, disk, battery condition, compliance, last seen, open proposals.",
    "get_device_history": "Time series of one device's metrics over the telemetry window (disk, RAM, battery, OS version, or compliance:<check_id>). Each series comes with a summary (number of points, pass and fail counts, first and last fail, min and max, when the current value began): state those figures, do not count rows yourself.",
    "get_compliance_overview": "Pass/fail counts per compliance check on latest snapshots, with the failing devices.",
    "get_software": "Find which devices have a piece of software (by name), or list the software on one device.",
    "run_insights": "Run pattern detectors across the telemetry window: battery_eol, ram_constrained, low_storage, compliance_drift, prohibited_software. Omit device_ids to check the whole fleet; pass device_ids for a question about particular devices, which also names any of them with too little history for a trend.",
    "get_definitions": "Return the threshold definitions in use, so answers can state them.",
    "list_proposals": "List action proposals and their status for this tenant.",
    "open_remediation_ticket": "PROPOSE an IT ticket for a compliance check failing on the device's latest snapshot. Creates a proposal only; a human must approve.",
    "flag_device_for_replacement": "PROPOSE flagging a device for replacement because its battery condition is not Normal. Proposal only.",
    "create_upgrade_order": "PROPOSE an upgrade request (ram or storage) for a device with a persistent RAM or storage finding. Nothing is purchased. Proposal only.",
    "notify_employee": "PROPOSE notifying the device's employee with a fixed template matching a current finding. No free text. Proposal only.",
    "request_software_removal": "PROPOSE an IT task to remove prohibited software present on the device's latest snapshot. Proposal only.",
    "acknowledge_finding": "PROPOSE suppressing a known finding (insight ID from this session) until a date at most 90 days after as-of. Proposal only.",
}
