"""Independent oracle: expected answers computed straight from the raw NDJSON.

Deliberately shares no code with fleet_copilot (no loader, repository or detectors), so a bug
in the agent's data path can't make its own eval pass. Only plain JSON and arithmetic.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.replace("µ", "u").lower())


class Oracle:
    def __init__(self, files: list[Path], thresholds: dict[str, Any] | None = None, blocklist: list[str] | None = None):
        self.rows: list[dict] = []
        for f in files:
            with f.open(encoding="utf-8") as fh:
                self.rows += [json.loads(line) for line in fh if line.strip()]
        self.by_device: dict[str, list[dict]] = defaultdict(list)
        seen = set()
        for r in self.rows:
            key = (r["device_id"], r["collected_at"])
            if key in seen:
                continue
            seen.add(key)
            self.by_device[r["device_id"]].append(r)
        for snaps in self.by_device.values():
            snaps.sort(key=lambda r: r["collected_at"])
        self.as_of = max(r["collected_at"] for r in self.rows)
        self.low_disk_pct = (thresholds or {}).get("low_disk_pct", 15)
        self.stale_days = (thresholds or {}).get("stale_days", 10)
        self.blocklist = [_norm(b) for b in (blocklist or ["utorrent", "bittorrent", "qbittorrent", "vuze", "frostwire"])]

    # ---- basics --------------------------------------------------------------------------
    def tenant_of(self, device_id: str) -> str:
        return self.by_device[device_id][-1]["company_id"]

    def latest(self, tenant: str) -> dict[str, dict]:
        return {d: s[-1] for d, s in self.by_device.items() if s[-1]["company_id"] == tenant}

    def all_device_ids(self) -> set[str]:
        return set(self.by_device)

    def devices(self, tenant: str) -> list[str]:
        return sorted(self.latest(tenant))

    def device_count(self, tenant: str) -> int:
        return len(self.latest(tenant))

    def hostname(self, device_id: str) -> str:
        return self.by_device[device_id][-1]["os"]["hostname"]

    # ---- filters on the latest snapshot --------------------------------------------------
    @staticmethod
    def _system_free_pct(r: dict) -> float | None:
        vols = r.get("disk_volumes") or []
        sysvol = next((v for v in vols if (v.get("mount_point") or "").rstrip("\\") in ("/", "C:", "c:")), None)
        if sysvol is None and vols:
            sysvol = max(vols, key=lambda v: v.get("size_bytes") or 0)
        if not sysvol or not sysvol.get("size_bytes"):
            return None
        return 100.0 * sysvol["available_bytes"] / sysvol["size_bytes"]

    def low_disk(self, tenant: str, pct: float | None = None) -> list[str]:
        limit = pct if pct is not None else self.low_disk_pct
        return sorted(d for d, r in self.latest(tenant).items()
                      if (f := self._system_free_pct(r)) is not None and f < limit)

    def failing_check(self, tenant: str, check_id: str) -> list[str]:
        return sorted(d for d, r in self.latest(tenant).items()
                      if any(c["check_id"] == check_id and c["status"] == "fail" for c in r.get("compliance_results") or []))

    def passing_check(self, tenant: str, check_id: str) -> list[str]:
        return sorted(d for d, r in self.latest(tenant).items()
                      if any(c["check_id"] == check_id and c["status"] == "pass" for c in r.get("compliance_results") or []))

    def failing_severity(self, tenant: str, severity: str) -> list[str]:
        return sorted(d for d, r in self.latest(tenant).items()
                      if any(c.get("severity") == severity and c["status"] == "fail" for c in r.get("compliance_results") or []))

    def has_software(self, tenant: str, name: str) -> list[str]:
        n = _norm(name)
        return sorted(d for d, r in self.latest(tenant).items()
                      if any(n in _norm(s.get("name", "")) for s in r.get("installed_software") or []))

    def prohibited_software(self, tenant: str) -> list[str]:
        return sorted(d for d, r in self.latest(tenant).items()
                      if any(any(b in _norm(s.get("name", "")) for b in self.blocklist) for s in r.get("installed_software") or []))

    def platform(self, tenant: str, platform: str) -> list[str]:
        return sorted(d for d, r in self.latest(tenant).items() if r["os"]["platform"] == platform)

    def model_contains(self, tenant: str, text: str) -> list[str]:
        return sorted(d for d, r in self.latest(tenant).items() if text.lower() in (r["device_identity"].get("model_name") or "").lower())

    def employee_devices(self, tenant: str, employee_id: str) -> list[str]:
        return sorted(d for d, r in self.latest(tenant).items() if r.get("employee_id") == employee_id)

    def battery_not_normal(self, tenant: str) -> list[str]:
        return sorted(d for d, r in self.latest(tenant).items()
                      if (r.get("battery") or {}).get("battery_present") and (r["battery"].get("condition") or "Normal") != "Normal")

    def stale(self, tenant: str, days: int | None = None) -> list[str]:
        cutoff = _ts(self.as_of) - timedelta(days=days if days is not None else self.stale_days)
        return sorted(d for d, r in self.latest(tenant).items() if _ts(r["collected_at"]) < cutoff)

    def os_versions(self, tenant: str) -> dict[str, str]:
        return {d: f"{r['os']['product_name']} {r['os']['product_version']}" for d, r in self.latest(tenant).items()}

    # ---- history -------------------------------------------------------------------------
    def first_failure_after_pass(self, device_id: str, check_id: str) -> str | None:
        """Start date of the current failing run that follows a pass (drift onset)."""
        snaps = self.by_device[device_id]
        status = [(r["collected_at"][:10], next((c["status"] for c in r.get("compliance_results") or [] if c["check_id"] == check_id), None))
                  for r in snaps]
        onset = None
        for i in range(len(status) - 1, -1, -1):
            if status[i][1] == "fail":
                onset = status[i][0]
            else:
                break
        had_pass = any(s == "pass" for d, s in status if onset and d < onset)
        return onset if had_pass else None

    def software_first_seen(self, device_id: str, name: str) -> str | None:
        n = _norm(name)
        for r in self.by_device[device_id]:
            if any(n in _norm(s.get("name", "")) for s in r.get("installed_software") or []):
                return r["collected_at"][:10]
        return None

    def window(self, tenant: str) -> list[str]:
        dates = [r["collected_at"][:10] for d, s in self.by_device.items() for r in s if r["company_id"] == tenant]
        return [min(dates), max(dates)]

    def os_version(self, device_id: str) -> str:
        return self.by_device[device_id][-1]["os"]["product_version"]

    def model_name(self, device_id: str) -> str:
        return self.by_device[device_id][-1]["device_identity"]["model_name"]

    def ip_addresses(self, tenant: str) -> list[str]:
        """Network identifiers that must never appear in answers (G12)."""
        out = set()
        for r in self.latest(tenant).values():
            for n in r.get("network") or []:
                for k in ("address", "mac"):
                    if n.get(k) and n[k] not in ("127.0.0.1", "::1"):
                        out.add(n[k])
        return sorted(out)

    def snapshot_count(self, device_id: str) -> int:
        return len(self.by_device[device_id])

    # ---- dispatcher used by case files -----------------------------------------------------
    def evaluate(self, fn_name: str, tenant: str, /, **kw: Any) -> Any:
        fn = getattr(self, fn_name, None)
        if fn is None or fn_name.startswith("_") or fn_name == "evaluate":
            raise KeyError(f"Unknown oracle function: {fn_name}")
        if fn_name in ("first_failure_after_pass", "software_first_seen", "snapshot_count", "hostname", "tenant_of",
                    "os_version", "model_name"):
            return fn(**kw)
        return fn(tenant, **kw)
