"""Insight detectors: pure Python over repository data. The LLM only explains their output.

Each insight carries the finding, its evidence IDs, a short explanation, caveats, a
severity, and the actions its evidence would justify (`eligible_actions`).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable

from fleet_copilot.config import AppConfig
from fleet_copilot.data.repository import Repository
from fleet_copilot.data.sanitize import normalize_name
from fleet_copilot.evidence import check_eid, insight_eid, software_eid

SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1}
DETECTORS = ("battery_eol", "ram_constrained", "low_storage", "compliance_drift", "prohibited_software")


@dataclass
class Insight:
    insight_id: str
    detector: str
    device_id: str
    hostname: str | None
    finding: str
    explanation: str
    severity: str
    evidence_ids: list[str]
    caveats: list[str] = field(default_factory=list)
    definitions_used: list[str] = field(default_factory=list)
    eligible_actions: list[dict[str, Any]] = field(default_factory=list)
    stale: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DetectorResult:
    insights: list[Insight]
    insufficient_data: list[str]          # device IDs skipped for too few snapshots
    devices_checked: int


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _by_device(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        out[r["device_id"]].append(r)
    for v in out.values():
        v.sort(key=lambda r: r["collected_at"])
    return out


def _linear_slope_per_day(points: list[tuple[datetime, float]]) -> float | None:
    if len(points) < 2:
        return None
    t0 = points[0][0]
    xs = [(t - t0).total_seconds() / 86400 for t, _ in points]
    ys = [v for _, v in points]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def _median(values: list[float]) -> float:
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


class Detectors:
    def __init__(self, repo: Repository, config: AppConfig):
        self.repo = repo
        self.cfg = config
        self.t = config.thresholds
        devices = repo.devices()
        self.devices = {d["device_id"]: d for d in devices}
        cutoff = repo.stale_cutoff(int(self.t.get("stale_device", "days")))
        self.stale = {d["device_id"] for d in devices if d["last_seen"] < _parse(cutoff).isoformat()}
        self._snaps: dict[str, list[dict]] | None = None

    # ---- helpers --------------------------------------------------------------------------
    @property
    def snaps(self) -> dict[str, list[dict]]:
        if self._snaps is None:
            self._snaps = _by_device(self.repo.all_snapshots())
        return self._snaps

    def _make(self, detector: str, device_id: str, **kw: Any) -> Insight:
        evidence = kw.pop("evidence_ids")
        insight = Insight(
            insight_id=insight_eid(detector, device_id, evidence),
            detector=detector,
            device_id=device_id,
            hostname=self.devices.get(device_id, {}).get("hostname"),
            evidence_ids=evidence,
            stale=device_id in self.stale,
            **kw,
        )
        if insight.stale:
            last = self.devices[device_id]["last_seen"][:10]
            insight.caveats.append(f"Device is stale (last snapshot {last}); findings may be out of date.")
        return insight

    def _next_spec(self, kind: str, current_gb: float | None) -> int | None:
        specs = self.cfg.policy.upgrade_specs.get(kind, ())
        if current_gb is None:
            return None
        larger = [s for s in specs if s > current_gb + 0.5]
        return min(larger) if larger else None

    # ---- detectors ------------------------------------------------------------------------
    def battery_eol(self, device_ids: set[str] | None = None) -> DetectorResult:
        out, insufficient = [], []
        checked = 0
        min_pts = int(self.t.get("battery_degrading", "min_points"))
        decline_thr = float(self.t.get("battery_degrading", "decline_pct"))
        for dev, snaps in self.snaps.items():
            if device_ids and dev not in device_ids:
                continue
            bsnaps = [s for s in snaps if s["battery_present"]]
            if not bsnaps:
                continue
            checked += 1
            latest = bsnaps[-1]
            cond = latest["battery_condition"]
            if cond and cond.lower() != "normal":
                first_bad = latest
                for s in reversed(bsnaps):
                    if (s["battery_condition"] or "").lower() == "normal":
                        break
                    first_bad = s
                had_normal = any((s["battery_condition"] or "").lower() == "normal" for s in bsnaps)
                since = f" since {first_bad['day']}" if had_normal else ""
                evidence = sorted({latest["snapshot_id"], first_bad["snapshot_id"]})
                out.append(self._make(
                    "battery_eol", dev,
                    finding=f"Battery condition is \"{cond}\"{since}",
                    explanation=f"The device itself reports the battery as \"{cond}\" (cycle count {latest['battery_cycle_count']}).",
                    severity="high", evidence_ids=evidence, definitions_used=["battery_eol"],
                    eligible_actions=[{"action": "flag_device_for_replacement",
                                       "params": {"device_id": dev, "reason": "battery_eol"}},
                                      {"action": "notify_employee",
                                       "params": {"template": "battery_replacement", "device_id": dev}}],
                    data={"condition": cond, "since": first_bad["day"] if had_normal else None,
                          "cycle_count": latest["battery_cycle_count"]},
                ))
            caps = [(_parse(s["collected_at"]), s["battery_full_charge_capacity"]) for s in bsnaps
                    if s["battery_full_charge_capacity"]]
            if len(caps) < min_pts:
                insufficient.append(dev)
                continue
            k = min(3, len(caps) // 3) or 1
            start, end = _median([c for _, c in caps[:k]]), _median([c for _, c in caps[-k:]])
            if start > 0:
                decline = 100.0 * (start - end) / start
                if decline >= decline_thr:
                    evidence = [bsnaps[0]["snapshot_id"], bsnaps[-1]["snapshot_id"]]
                    out.append(self._make(
                        "battery_eol", dev,
                        finding=f"Full-charge capacity fell {decline:.1f}% over the window ({start:.0f} → {end:.0f})",
                        explanation="Capacity is trending down across snapshots, an early sign of battery wear.",
                        severity="medium", evidence_ids=evidence, definitions_used=["battery_degrading"],
                        caveats=["Design capacity is not in the data, so this is a relative decline, not battery health %."],
                        data={"decline_pct": round(decline, 1), "start": start, "end": end, "kind": "degrading"},
                    ))
        return DetectorResult(out, insufficient, checked)

    def ram_constrained(self, device_ids: set[str] | None = None) -> DetectorResult:
        out, insufficient = [], []
        thr = float(self.t.get("ram_flagged", "used_pct_gte"))
        consecutive = int(self.t.get("ram_flagged", "consecutive"))
        share_thr = float(self.t.get("ram_persistent", "share"))
        min_pts = int(self.t.get("ram_persistent", "min_points"))
        checked = 0
        for dev, snaps in self.snaps.items():
            if device_ids and dev not in device_ids:
                continue
            vals = [s for s in snaps if s["ram_used_pct"] is not None]
            if not vals:
                continue
            checked += 1
            high = [s for s in vals if s["ram_used_pct"] >= thr]
            best, run, best_run = 0, [], []
            for s in vals:
                if s["ram_used_pct"] >= thr:
                    run.append(s)
                    if len(run) > best:
                        best, best_run = len(run), list(run)
                else:
                    run = []
            share = len(high) / len(vals)
            persistent = len(vals) >= min_pts and share >= share_thr
            if len(vals) < min_pts:
                insufficient.append(dev)
            if not persistent and best < consecutive:
                continue
            latest = vals[-1]
            evidence = sorted({s["snapshot_id"] for s in (high if persistent else best_run)})
            caveats = ["One RAM reading per day: high usage at that moment, not proof of sustained memory pressure."]
            actions = []
            if persistent:
                spec = self._next_spec("ram_gb", latest["ram_total_gb"])
                if spec:
                    actions.append({"action": "create_upgrade_order",
                                    "params": {"device_id": dev, "component": "ram", "spec_gb": spec}})
                else:
                    caveats.append("No larger RAM option in the allowed upgrade list.")
            out.append(self._make(
                "ram_constrained", dev,
                finding=(f"RAM at or above {thr:.0f}% in {len(high)} of {len(vals)} snapshots" if persistent
                         else f"RAM at or above {thr:.0f}% on {best} consecutive snapshots"),
                explanation=(f"Memory is consistently near full on a {latest['ram_total_gb']:.0f} GB device."
                             if persistent else "A short run of very high memory use; not yet persistent."),
                severity="medium" if persistent else "low",
                evidence_ids=evidence,
                definitions_used=["ram_persistent" if persistent else "ram_flagged"],
                caveats=caveats, eligible_actions=actions,
                data={"share": round(share, 2), "max_consecutive": best, "ram_total_gb": latest["ram_total_gb"],
                      "level": "persistent" if persistent else "flagged"},
            ))
        return DetectorResult(out, insufficient, checked)

    def low_storage(self, device_ids: set[str] | None = None) -> DetectorResult:
        out, insufficient = [], []
        thr = float(self.t.get("low_disk", "free_pct_lt"))
        share_thr = float(self.t.get("low_disk_persistent", "share"))
        min_pts = int(self.t.get("low_disk_persistent", "min_points"))
        checked = 0
        for dev, snaps in self.snaps.items():
            if device_ids and dev not in device_ids:
                continue
            vals = [s for s in snaps if s["disk_free_pct"] is not None]
            if not vals:
                continue
            checked += 1
            latest = vals[-1]
            low = [s for s in vals if s["disk_free_pct"] < thr]
            persistent = len(vals) >= min_pts and len(low) / len(vals) >= share_thr
            slope = None
            if len(vals) >= min_pts:
                slope = _linear_slope_per_day([(_parse(s["collected_at"]), s["disk_free_pct"]) for s in vals])
            else:
                insufficient.append(dev)
            is_low = latest["disk_free_pct"] < thr
            days_to_low = None
            if not is_low and slope is not None and slope < 0:
                days_to_low = (latest["disk_free_pct"] - thr) / -slope
            if not is_low and (days_to_low is None or days_to_low > 14):
                continue
            caveats, actions = [], []
            if slope is not None:
                caveats.append(f"Trend estimate: {slope:+.2f} percentage points of free space per day (linear fit).")
            if is_low:
                actions.append({"action": "notify_employee", "params": {"template": "low_disk", "device_id": dev}})
                if persistent:
                    spec = self._next_spec("storage_gb", latest["disk_total_gb"])
                    if spec:
                        actions.insert(0, {"action": "create_upgrade_order",
                                           "params": {"device_id": dev, "component": "storage", "spec_gb": spec}})
            evidence = sorted({latest["snapshot_id"], *(s["snapshot_id"] for s in low[-5:])})
            if is_low:
                finding = f"{latest['disk_free_pct']:.1f}% free on the system volume ({latest['disk_free_gb']:.0f} GB of {latest['disk_total_gb']:.0f} GB)"
                if persistent:
                    finding += f"; low in {len(low)} of {len(vals)} snapshots"
            else:
                finding = f"Projected to fall below {thr:.0f}% free in about {days_to_low:.0f} days (now {latest['disk_free_pct']:.1f}%)"
            out.append(self._make(
                "low_storage", dev, finding=finding,
                explanation=("Free space is consistently below the threshold." if persistent
                             else "Free space is below the threshold." if is_low
                             else "Free space is falling steadily toward the threshold."),
                severity="high" if latest["disk_free_pct"] < 5 else ("medium" if is_low else "low"),
                evidence_ids=evidence,
                definitions_used=["low_disk"] + (["low_disk_persistent"] if persistent else []),
                caveats=caveats, eligible_actions=actions,
                data={"free_pct": latest["disk_free_pct"], "free_gb": latest["disk_free_gb"],
                      "total_gb": latest["disk_total_gb"], "persistent": persistent,
                      "slope_pct_per_day": None if slope is None else round(slope, 3),
                      "days_to_threshold": None if days_to_low is None else round(days_to_low, 1)},
            ))
        return DetectorResult(out, insufficient, checked)

    def compliance_drift(self, device_ids: set[str] | None = None) -> DetectorResult:
        out = []
        need = int(self.t.get("compliance_drift", "consecutive_fails"))
        flap_n = int(self.t.get("compliance_flapping", "changes"))
        rows = self.repo.compliance_history()
        series: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for r in rows:
            if device_ids and r["device_id"] not in device_ids:
                continue
            series[(r["device_id"], r["check_id"])].append(r)
        for (dev, check), seq in sorted(series.items()):
            seq.sort(key=lambda r: r["collected_at"])
            statuses = [r["status"] for r in seq]
            changes = sum(1 for a, b in zip(statuses, statuses[1:]) if a != b)
            trailing = 0
            for s in reversed(statuses):
                if s != "fail":
                    break
                trailing += 1
            severity = seq[-1]["severity"] or "medium"
            passed_before = "pass" in statuses[: len(statuses) - trailing]
            if changes >= flap_n:
                evidence = [check_eid(r["snapshot_id"], check) for r in seq if r["status"] == "fail"][-5:]
                out.append(self._make(
                    "compliance_drift", dev,
                    finding=f"{check} changed status {changes} times in the window (flapping); latest: {statuses[-1]}",
                    explanation="The check keeps switching between pass and fail; reported separately from drift.",
                    severity="low", evidence_ids=evidence, definitions_used=["compliance_flapping"],
                    data={"check_id": check, "kind": "flapping", "changes": changes, "latest": statuses[-1]},
                ))
                continue
            if passed_before and trailing >= need:
                last_pass = seq[len(seq) - trailing - 1]
                first_fail = seq[len(seq) - trailing]
                latest = seq[-1]
                evidence = [check_eid(last_pass["snapshot_id"], check), check_eid(first_fail["snapshot_id"], check),
                            check_eid(latest["snapshot_id"], check)]
                actions = [{"action": "open_remediation_ticket",
                            "params": {"device_id": dev, "check_id": check}, "severity": severity}]
                template = {"screen_lock": "screen_lock_required", "os_up_to_date": "os_update_required"}.get(check, "compliance_issue")
                actions.append({"action": "notify_employee", "params": {"template": template, "device_id": dev, "check_id": check}})
                out.append(self._make(
                    "compliance_drift", dev,
                    finding=f"{check} passed until {last_pass['collected_at'][:10]} and has failed since {first_fail['collected_at'][:10]} ({trailing} snapshots)",
                    explanation=f"A {severity}-severity check that used to pass is now failing consistently.",
                    severity=severity, evidence_ids=sorted(set(evidence)), definitions_used=["compliance_drift"],
                    eligible_actions=actions,
                    data={"check_id": check, "kind": "drift", "last_pass": last_pass["collected_at"][:10],
                          "first_fail": first_fail["collected_at"][:10], "consecutive_fails": trailing},
                ))
        return DetectorResult(out, [], len({d for d, _ in series}))

    def prohibited_software(self, device_ids: set[str] | None = None) -> DetectorResult:
        out = []
        blocklist = self.cfg.blocklist
        history = self.repo.software_history()
        latest = {(r["device_id"], r["name_norm"]): r for r in self.repo.software_latest()}
        by_dev: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
        for r in history:
            if any(b in r["name_norm"] for b in blocklist):
                by_dev[r["device_id"]][r["name_norm"]].append(r)
        for dev, names in sorted(by_dev.items()):
            if device_ids and dev not in device_ids:
                continue
            for norm, seen in names.items():
                if (dev, norm) not in latest:
                    continue
                seen.sort(key=lambda r: r["collected_at"])
                first, last = seen[0], latest[(dev, norm)]
                evidence = sorted({software_eid(first["snapshot_id"], norm), software_eid(last["snapshot_id"], norm)})
                out.append(self._make(
                    "prohibited_software", dev,
                    finding=f"Prohibited software \"{last['name']}\" installed (first seen {first['collected_at'][:10]})",
                    explanation="Matches the prohibited-software blocklist; no compliance check covers it.",
                    severity="high", evidence_ids=evidence, definitions_used=["prohibited_software"],
                    eligible_actions=[{"action": "request_software_removal",
                                       "params": {"device_id": dev, "software_name": last["name"]}},
                                      {"action": "notify_employee",
                                       "params": {"template": "prohibited_software", "device_id": dev}}],
                    data={"software": last["name"], "version": last["version"], "first_seen": first["collected_at"][:10]},
                ))
        return DetectorResult(out, [], len(self.devices))

    def run(self, names: list[str] | None = None, device_ids: set[str] | None = None) -> dict[str, DetectorResult]:
        registry: dict[str, Callable[..., DetectorResult]] = {
            "battery_eol": self.battery_eol,
            "ram_constrained": self.ram_constrained,
            "low_storage": self.low_storage,
            "compliance_drift": self.compliance_drift,
            "prohibited_software": self.prohibited_software,
        }
        selected = names or list(DETECTORS)
        unknown = [n for n in selected if n not in registry]
        if unknown:
            raise ValueError(f"Unknown detector(s): {unknown}")
        return {n: registry[n](device_ids) for n in selected}


def failing_check_actions(device_id: str, check_id: str, severity: str | None) -> list[dict[str, Any]]:
    """Actions justified by a check that fails on the latest snapshot (gap F-05)."""
    template = {"screen_lock": "screen_lock_required", "os_up_to_date": "os_update_required"}.get(check_id, "compliance_issue")
    return [
        {"action": "open_remediation_ticket", "params": {"device_id": device_id, "check_id": check_id},
         "severity": severity or "medium"},
        {"action": "notify_employee", "params": {"template": template, "device_id": device_id, "check_id": check_id}},
    ]


def sort_insights(insights: list[Insight]) -> list[Insight]:
    return sorted(insights, key=lambda i: (-SEVERITY_RANK.get(i.severity, 0), i.detector, i.device_id))


def normalize_software_query(name: str) -> str:
    return normalize_name(name)
