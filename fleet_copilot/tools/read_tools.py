"""Read tools. Every result carries evidence IDs and a query receipt; every row is tenant-scoped."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from fleet_copilot.data.sanitize import normalize_name, software_matches
from fleet_copilot.data.versions import normalize_platform, parse_version
from fleet_copilot.evidence import check_eid, software_eid
from fleet_copilot.insights.detectors import Detectors, failing_check_actions, sort_insights
from fleet_copilot.session import whole_numbers
from fleet_copilot.tools.envelope import ToolError, ToolResult
from fleet_copilot.tools.schemas import (
    FindDevicesArgs,
    GetComplianceOverviewArgs,
    GetDefinitionsArgs,
    GetDeviceArgs,
    GetDeviceHistoryArgs,
    GetSoftwareArgs,
    ListProposalsArgs,
    ResolveEntitiesArgs,
    RunInsightsArgs,
)

if TYPE_CHECKING:
    from fleet_copilot.toolkit import Toolkit

STOPWORDS = {"the", "a", "an", "my", "our", "device", "laptop", "machine", "computer", "of", "for", "s"}


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9-]+", text.lower()) if t and t not in STOPWORDS]


def _device_label(d: dict) -> str:
    return f"{d.get('hostname') or d['device_id']} ({d.get('model_name') or 'unknown model'}, {d['device_id']})"


class ReadTools:
    def __init__(self, tk: Toolkit):
        self.tk = tk

    # helpers --------------------------------------------------------------------------------
    @property
    def repo(self):
        return self.tk.repo

    def _finish(self, tool: str, args: dict, result: ToolResult, count: int) -> ToolResult:
        self.tk.check_output(tool, result.data)
        result.as_of = self.repo.as_of
        result.receipt_id = self.tk.evidence.add_receipt(tool, args, count, self.repo.as_of, result.evidence_ids,
                                                         result.definitions_used, whole_numbers(result.data))
        return result

    def _require_device(self, device_id: str) -> dict:
        device = self.repo.device(device_id)
        if device is None:
            raise ToolError("NOT_FOUND", f"No device '{device_id}' in this tenant.",
                            "Use resolve_entities or find_devices to get valid device IDs.")
        return device

    def _stale_ids(self) -> set[str]:
        cutoff = self.repo.stale_cutoff(int(self.tk.config.thresholds.get("stale_device", "days")))
        cutoff_iso = cutoff.replace("Z", "+00:00")
        return {d["device_id"] for d in self.repo.devices() if d["last_seen"] < cutoff_iso}

    def _register_snapshot(self, snap: dict) -> str:
        record = {k: snap.get(k) for k in ("day", "ram_used_pct", "disk_free_pct", "battery_condition", "os_version")}
        self.tk.evidence.add(snap["snapshot_id"], record, snap["device_id"])
        return snap["snapshot_id"]

    def _register_check(self, row: dict) -> str:
        eid = check_eid(row["snapshot_id"], row["check_id"])
        self.tk.evidence.add(eid, {"check_id": row["check_id"], "status": row["status"], "severity": row["severity"],
                                   "day": row["collected_at"][:10]}, row["device_id"])
        return eid

    # tools ----------------------------------------------------------------------------------
    def resolve_entities(self, args: ResolveEntitiesArgs) -> ToolResult:
        devices = self.repo.devices()
        out = []
        for ref in args.refs:
            raw = ref.strip()
            toks = _tokens(raw)
            exact = [d for d in devices if raw.lower() in {d["device_id"].lower(), (d["hostname"] or "").lower(),
                                                            (d["employee_id"] or "").lower()}]
            if exact:
                matches = exact
            elif toks:
                def hay(d: dict) -> str:
                    return " ".join(str(d.get(k) or "") for k in ("device_id", "hostname", "model_name", "manufacturer",
                                                                    "employee_id", "platform")).lower()
                matches = [d for d in devices if all(t in hay(d) for t in toks)]
            else:
                matches = []
            employees = sorted({d["employee_id"] for d in matches if d["employee_id"] and d["employee_id"].lower() == raw.lower()})
            status = "resolved" if len(matches) == 1 else ("ambiguous" if matches else "not_found")
            out.append({
                "ref": raw, "status": status,
                "matches": [{"type": "device", "device_id": d["device_id"], "label": _device_label(d),
                             "employee_id": d["employee_id"]} for d in matches[:10]],
                "employee_ids": employees,
            })
        return self._finish("resolve_entities", args.model_dump(), ToolResult(data=out), sum(len(o["matches"]) for o in out))

    def _device_view(self, devices: dict[str, dict], latest: dict[str, dict]) -> dict[str, dict]:
        stale = self._stale_ids()
        view = {}
        for dev_id, d in devices.items():
            s = latest.get(dev_id)
            if not s:
                continue
            view[dev_id] = {"device": d, "snap": s, "stale": dev_id in stale}
        return view

    def find_devices(self, args: FindDevicesArgs) -> ToolResult:
        c = args.criteria
        thresholds = self.tk.config.thresholds
        devices = {d["device_id"]: d for d in self.repo.devices()}
        latest = {s["device_id"]: s for s in self.repo.latest_snapshots()}
        view = self._device_view(devices, latest)
        compliance = defaultdict(list)
        for row in self.repo.compliance_latest():
            compliance[row["device_id"]].append(row)
        software = defaultdict(list)
        if c.has_software or c.has_prohibited_software is not None:
            for row in self.repo.software_latest():
                software[row["device_id"]].append(row)
        caveats, definitions = [], []
        excluded_platform = 0
        target_version = None
        if c.os_older_than:
            plat = normalize_platform(c.os_older_than.platform)
            target_version = parse_version(plat, c.os_older_than.version)
            if target_version is None:
                raise ToolError("INVALID_ARGUMENT", f"Can't parse version '{c.os_older_than.version}'.",
                                "Use '15' or '14.5' for macOS, '11 23H2' for Windows.")
        low_thr = float(thresholds.get("low_disk", "free_pct_lt"))
        if c.low_disk is not None:
            definitions.append("low_disk")
        if c.stale is not None:
            definitions.append("stale_device")
        if c.has_prohibited_software is not None:
            definitions.append("prohibited_software")

        rows, evidence = [], []
        for dev_id, v in sorted(view.items()):
            d, s = v["device"], v["snap"]
            matched: dict[str, Any] = {}
            ev = [s["snapshot_id"]]
            if c.device_ids and dev_id not in c.device_ids:
                continue
            if c.employee_id and (d["employee_id"] or "").lower() != c.employee_id.lower():
                continue
            if c.platform and d["platform"] != c.platform:
                continue
            if c.manufacturer and (d["manufacturer"] or "").lower() != c.manufacturer.lower():
                continue
            if c.model_contains and c.model_contains.lower() not in (d["model_name"] or "").lower():
                continue
            if c.is_laptop is not None and bool(d["is_laptop"]) != c.is_laptop:
                continue
            if c.stale is not None and v["stale"] != c.stale:
                continue
            if c.os_older_than:
                plat = normalize_platform(c.os_older_than.platform)
                if s["os_platform"] != plat:
                    excluded_platform += 1
                    continue
                current = (s["os_v1"], s["os_v2"], s["os_v3"])
                if None in current or not current < target_version:
                    continue
                matched["os_version"] = s["os_version"]
            if c.low_disk is not None:
                is_low = s["disk_free_pct"] is not None and s["disk_free_pct"] < low_thr
                if is_low != c.low_disk:
                    continue
            if c.disk_free_pct_lt is not None and not (s["disk_free_pct"] is not None and s["disk_free_pct"] < c.disk_free_pct_lt):
                continue
            if c.disk_free_gb_lt is not None and not (s["disk_free_gb"] is not None and s["disk_free_gb"] < c.disk_free_gb_lt):
                continue
            if any(x is not None for x in (c.low_disk, c.disk_free_pct_lt, c.disk_free_gb_lt)) or args.sort_by == "disk_free_pct":
                matched.update(disk_free_pct=s["disk_free_pct"], disk_free_gb=s["disk_free_gb"], disk_total_gb=s["disk_total_gb"])
            if c.ram_used_pct_gte is not None:
                if s["ram_used_pct"] is None or s["ram_used_pct"] < c.ram_used_pct_gte:
                    continue
            if c.ram_used_pct_gte is not None or args.sort_by == "ram_used_pct":
                matched.update(ram_used_pct=s["ram_used_pct"], ram_total_gb=s["ram_total_gb"])
            if c.failing_check or c.failing_severity:
                failing = [r for r in compliance[dev_id] if r["status"] == "fail"
                           and (not c.failing_check or r["check_id"] == c.failing_check)
                           and (not c.failing_severity or r["severity"] == c.failing_severity)]
                if not failing:
                    continue
                matched["failing_checks"] = [{"check_id": r["check_id"], "severity": r["severity"]} for r in failing]
                ev += [self._register_check(r) for r in failing]
            if c.has_software:
                hits = [r for r in software[dev_id] if software_matches(c.has_software, r["name"])]
                if not hits:
                    continue
                matched["software"] = [f"{r['name']} {r['version'] or ''}".strip() for r in hits]
                for r in hits:
                    eid = software_eid(r["snapshot_id"], r["name_norm"])
                    self.tk.evidence.add(eid, {"name": r["name"], "version": r["version"]}, dev_id)
                    ev.append(eid)
            if c.has_prohibited_software is not None:
                bl = self.tk.config.blocklist
                hits = [r for r in software[dev_id] if any(b in r["name_norm"] for b in bl)]
                if bool(hits) != c.has_prohibited_software:
                    continue
                if hits:
                    matched["prohibited_software"] = [r["name"] for r in hits]
                    for r in hits:
                        eid = software_eid(r["snapshot_id"], r["name_norm"])
                        self.tk.evidence.add(eid, {"name": r["name"], "version": r["version"], "prohibited": True}, dev_id)
                        ev.append(eid)
            if c.battery_not_normal is not None:
                bad = bool(s["battery_condition"]) and s["battery_condition"].lower() != "normal"
                if bad != c.battery_not_normal:
                    continue
                matched["battery_condition"] = s["battery_condition"]
                definitions.append("battery_eol")
            self._register_snapshot(s)
            rows.append({
                "device_id": dev_id, "hostname": d["hostname"], "model": d["model_name"], "employee_id": d["employee_id"],
                "os": f"{s['os_name']} {s['os_version']}", "last_seen": d["last_seen"][:10], "stale": v["stale"],
                "matched_values": matched,
            })
            evidence.extend(ev)

        if excluded_platform:
            caveats.append(f"{excluded_platform} device(s) on other platforms were excluded from the OS comparison.")
        stale_hits = [r["device_id"] for r in rows if r["stale"]]
        if stale_hits and c.stale is None:
            caveats.append(f"Stale (no snapshot for {thresholds.get('stale_device', 'days')}+ days): {', '.join(stale_hits)}")
        total = len(rows)
        if args.sort_by:
            key = {"disk_free_pct": lambda r: (r["matched_values"].get("disk_free_pct") is None, r["matched_values"].get("disk_free_pct") or 0),
                   "ram_used_pct": lambda r: -(r["matched_values"].get("ram_used_pct") or 0),
                   "last_seen": lambda r: r["last_seen"], "device_id": lambda r: r["device_id"]}[args.sort_by]
            rows.sort(key=key)
        data: Any
        if args.group_by:
            groups: dict[str, list[str]] = defaultdict(list)
            for r in rows:
                d = devices[r["device_id"]]
                key = {"platform": d["platform"], "manufacturer": d["manufacturer"], "model": d["model_name"],
                       "os_version": r["os"], "employee": d["employee_id"]}[args.group_by]
                groups[str(key)].append(r["device_id"])
            data = {"total": total, "groups": {k: {"count": len(v), "device_ids": v} for k, v in sorted(groups.items())}}
        else:
            data = {"total": total, "devices": rows[args.offset: args.offset + args.limit]}
        more = not args.group_by and total > args.offset + args.limit
        res = ToolResult(data=data, total_count=total, truncated=more, next_offset=(args.offset + args.limit) if more else None,
                         evidence_ids=sorted(set(evidence)), caveats=caveats, definitions_used=sorted(set(definitions)))
        if total == 0:
            res.caveats.append("No devices matched; this is a 'no matches' result, cite the receipt.")
        return self._finish("find_devices", args.model_dump(exclude_none=True), res, total)

    def get_device(self, args: GetDeviceArgs) -> ToolResult:
        d = self._require_device(args.device_id)
        s = self.repo.latest_snapshot(args.device_id)
        if s is None:
            raise ToolError("INSUFFICIENT_DATA", f"No snapshots for {args.device_id} up to as-of.")
        checks = [r for r in self.repo.compliance_latest() if r["device_id"] == args.device_id]
        evidence = [self._register_snapshot(s)] + [self._register_check(r) for r in checks]
        sw = self.repo.software_latest(args.device_id)
        stale = args.device_id in self._stale_ids()
        data = {
            "device_id": d["device_id"], "hostname": d["hostname"], "model": d["model_name"],
            "manufacturer": d["manufacturer"], "is_laptop": bool(d["is_laptop"]), "employee_id": d["employee_id"],
            "os": f"{s['os_name']} {s['os_version']}", "ram_total_gb": s["ram_total_gb"], "ram_used_pct": s["ram_used_pct"],
            "disk_total_gb": s["disk_total_gb"], "disk_free_gb": s["disk_free_gb"], "disk_free_pct": s["disk_free_pct"],
            "battery_condition": s["battery_condition"], "battery_cycle_count": s["battery_cycle_count"],
            "compliance": [{"check_id": r["check_id"], "status": r["status"], "severity": r["severity"],
                            "evidence_id": check_eid(r["snapshot_id"], r["check_id"])} for r in checks],
            "software_count": len(sw), "first_seen": d["first_seen"][:10], "last_seen": d["last_seen"][:10],
            "stale": stale, "snapshot_id": s["snapshot_id"],
            "open_proposals": [{"proposal_id": p["proposal_id"], "action": p["action"], "status": p["status"]}
                               for p in self.tk.store.list(device_id=args.device_id) if p["status"] in ("proposed", "pending", "failed")],
        }
        caveats = [f"Device is stale (last snapshot {d['last_seen'][:10]})."] if stale else []
        return self._finish("get_device", args.model_dump(), ToolResult(data=data, evidence_ids=evidence, caveats=caveats), 1)

    def get_device_history(self, args: GetDeviceHistoryArgs) -> ToolResult:
        self._require_device(args.device_id)
        lo, hi = self.repo.data_window()
        if args.start and hi and args.start > hi[:10] or args.end and lo and args.end < lo[:10]:
            raise ToolError("INSUFFICIENT_DATA", f"No telemetry in that range. Data covers {lo[:10]} to {hi[:10]}.",
                            "Ask about dates inside the telemetry window.")
        start = f"{args.start}T00:00:00Z" if args.start else None
        end = f"{args.end}T23:59:59Z" if args.end else None
        snaps = self.repo.snapshots(args.device_id, start, end)
        min_pts = int(self.tk.config.thresholds.get("min_points_for_trend", "points"))
        series: dict[str, list[dict]] = {}
        evidence: list[str] = []
        comp = None
        for metric in args.metrics:
            if metric.startswith("compliance:"):
                check = metric.split(":", 1)[1]
                comp = comp or self.repo.compliance_history(args.device_id)
                pts = [r for r in comp if r["check_id"] == check and (not start or r["collected_at"] >= start)
                       and (not end or r["collected_at"] <= end)]
                series[metric] = [{"date": r["collected_at"][:10], "value": r["status"], "evidence_id": self._register_check(r)} for r in pts]
            else:
                series[metric] = [{"date": s["day"], "value": s[metric], "evidence_id": self._register_snapshot(s)} for s in snaps]
            evidence += [p["evidence_id"] for p in series[metric]]
        caveats = []
        if len(snaps) < min_pts:
            caveats.append(f"Only {len(snaps)} snapshots in range; fewer than {min_pts} is insufficient data for a trend.")
        data = {"device_id": args.device_id, "window": [lo[:10] if lo else None, hi[:10] if hi else None], "series": series}
        return self._finish("get_device_history", args.model_dump(exclude_none=True),
                            ToolResult(data=data, evidence_ids=sorted(set(evidence)), caveats=caveats,
                                       definitions_used=["min_points_for_trend"]), len(snaps))

    def get_compliance_overview(self, args: GetComplianceOverviewArgs) -> ToolResult:
        rows = [r for r in self.repo.compliance_latest()
                if (not args.check_id or r["check_id"] == args.check_id) and (not args.severity or r["severity"] == args.severity)]
        devices = {d["device_id"]: d for d in self.repo.devices()}
        checks: dict[str, dict] = {}
        evidence = []
        for r in rows:
            c = checks.setdefault(r["check_id"], {"check_id": r["check_id"], "severity": r["severity"], "pass": 0, "fail": 0, "failing_devices": []})
            c[r["status"] if r["status"] in ("pass", "fail") else "fail"] += 1
            if r["status"] != "pass":
                eid = self._register_check(r)
                evidence.append(eid)
                c["failing_devices"].append({"device_id": r["device_id"], "hostname": devices.get(r["device_id"], {}).get("hostname"),
                                             "evidence_id": eid})
        res = ToolResult(data={"as_of_latest_snapshot_per_device": True, "checks": list(checks.values())},
                         evidence_ids=evidence, total_count=len(rows))
        if not rows:
            res.caveats.append("No compliance results matched; cite the receipt for this 'no matches' result.")
        return self._finish("get_compliance_overview", args.model_dump(exclude_none=True), res, len(rows))

    def _software_item(self, row: dict, first_seen: dict[tuple, str], with_device: bool) -> dict:
        """One software entry. Both routes of get_software build their rows here, so they carry the same facts."""
        eid = software_eid(row["snapshot_id"], row["name_norm"])
        prohibited = any(b in row["name_norm"] for b in self.tk.config.blocklist)
        seen = first_seen.get((row["device_id"], row["name_norm"]))
        self.tk.evidence.add(eid, {"name": row["name"], "version": row["version"], "prohibited": prohibited,
                                   "first_seen": seen}, row["device_id"])
        item = {"name": row["name"], "version": row["version"], "first_seen": seen, "prohibited": prohibited,
                "evidence_id": eid}
        return {"device_id": row["device_id"]} | item if with_device else item

    def get_software(self, args: GetSoftwareArgs) -> ToolResult:
        if not args.name and not args.device_id:
            raise ToolError("INVALID_ARGUMENT", "Give a software name or a device ID.")
        if args.device_id:
            self._require_device(args.device_id)
        rows = self.repo.software_latest(args.device_id)
        if args.name:
            rows = [r for r in rows if software_matches(args.name, r["name"])]
        # first day each (device, software) pair appears in the window; history is ordered by date
        first_seen: dict[tuple, str] = {}
        for r in self.repo.software_history(normalize_name(args.name) if args.name else None):
            first_seen.setdefault((r["device_id"], r["name_norm"]), r["collected_at"][:10])
        by_device = bool(args.device_id)
        ordered = rows if by_device else sorted(rows, key=lambda r: (r["device_id"], r["name_norm"]))
        items = [self._software_item(r, first_seen, with_device=not by_device) for r in ordered]
        data = {"device_id": args.device_id, "software": items} if by_device else {"query": args.name, "installed_on": items}
        return self._finish("get_software", args.model_dump(exclude_none=True),
                            ToolResult(data=data, evidence_ids=[i["evidence_id"] for i in items], total_count=len(items),
                                       definitions_used=["prohibited_software"]), len(items))

    def run_insights(self, args: RunInsightsArgs) -> ToolResult:
        if args.device_ids:
            for dev in args.device_ids:
                self._require_device(dev)
        results = Detectors(self.repo, self.tk.config).run(args.detectors, set(args.device_ids) if args.device_ids else None)
        acks = self.tk.store.acknowledgements()
        insights, insufficient, definitions = [], {}, set()
        for name, res in results.items():
            if res.insufficient_data:
                insufficient[name] = res.insufficient_data
            for ins in res.insights:
                ack = acks.get(ins.insight_id)
                if ack and ack["until"] >= self.repo.as_of[:10] and not args.include_acknowledged:
                    continue
                insights.append(ins)
        insights = sort_insights(insights)
        evidence = []
        for ins in insights:
            d = ins.to_dict()
            ack = acks.get(ins.insight_id)
            if ack:
                d["acknowledged_until"] = ack["until"]
            self.tk.evidence.add(ins.insight_id, d, ins.device_id)
            for eid in ins.evidence_ids:
                self.tk.evidence.add(eid, {"via": ins.insight_id}, ins.device_id)
            evidence.append(ins.insight_id)
            definitions.update(ins.definitions_used)
        data = {
            "detectors_run": list(results.keys()),
            "devices_checked": {n: r.devices_checked for n, r in results.items()},
            "insights": [{k: v for k, v in i.to_dict().items() if k != "eligible_actions"} | {
                "eligible_actions": [a["action"] for a in i.eligible_actions]} for i in insights],
            "insufficient_data_devices": insufficient,
        }
        res = ToolResult(data=data, evidence_ids=evidence, total_count=len(insights), definitions_used=sorted(definitions))
        if not insights:
            res.caveats.append("No findings from the detectors that ran; a valid 'no issues found' result.")
        return self._finish("run_insights", args.model_dump(exclude_none=True), res, len(insights))

    def get_definitions(self, args: GetDefinitionsArgs) -> ToolResult:
        t = self.tk.config.thresholds
        names = args.names or list(t.names())
        unknown = [n for n in names if n not in t.names()]
        if unknown:
            raise ToolError("INVALID_ARGUMENT", f"Unknown definition(s): {unknown}", f"Known: {list(t.names())}")
        data = {n: t.describe(n) for n in names}
        return self._finish("get_definitions", args.model_dump(exclude_none=True),
                            ToolResult(data=data, definitions_used=names), len(names))

    def list_proposals(self, args: ListProposalsArgs) -> ToolResult:
        rows = self.tk.store.list(args.status, args.device_id)
        data = [{"proposal_id": p["proposal_id"], "action": p["action"], "device_id": p["device_id"],
                 "employee_id": p["employee_id"], "status": p["status"], "origin": p["origin"],
                 "reason": p["reason"], "reject_reason": p["reject_reason"]} for p in rows]
        # no timestamps: wall-clock values would make recorded eval runs unrepeatable
        for p in rows:
            self.tk.evidence.add(p["proposal_id"], {"action": p["action"], "status": p["status"]}, p["device_id"])
        return self._finish("list_proposals", args.model_dump(exclude_none=True),
                            ToolResult(data=data, evidence_ids=[p["proposal_id"] for p in rows], total_count=len(rows)), len(rows))


def eligible_actions_from_evidence(tk: Toolkit, records: list) -> list[dict[str, Any]]:
    """Gap F-05: eligibility over all evidence gathered this turn, whatever tool returned it."""
    out: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for rec in records:
        if rec.kind == "insight":
            for a in rec.record.get("eligible_actions", []):
                key = (a["action"], tuple(sorted(a["params"].items())))
                if key not in seen:
                    seen.add(key)
                    out.append({**a, "severity": a.get("severity") or rec.record.get("severity", "medium"),
                                "evidence_ids": [rec.evidence_id], "stale": rec.record.get("stale", False),
                                "persistent": rec.record.get("data", {}).get("level") == "persistent"
                                or rec.record.get("data", {}).get("persistent", False)})
        elif rec.kind == "check" and rec.record.get("status") == "fail" and rec.device_id:
            latest = tk.repo.latest_snapshot(rec.device_id)
            if not latest or not rec.evidence_id.startswith(latest["snapshot_id"] + "#"):
                continue
            for a in failing_check_actions(rec.device_id, rec.record["check_id"], rec.record.get("severity")):
                key = (a["action"], tuple(sorted(a["params"].items())))
                if key not in seen:
                    seen.add(key)
                    out.append({**a, "evidence_ids": [rec.evidence_id], "stale": False, "persistent": True})
    return out
