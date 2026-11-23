"""Build the derived SQLite database from the raw NDJSON (the immutable source of truth).

Fields that are never loaded (security.md): IP and MAC addresses, network interfaces,
hardware UUID, serial number (duplicate of device_id), page size, total_memory string.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fleet_copilot.data.sanitize import clean_text, normalize_name
from fleet_copilot.data.schema import SCHEMA
from fleet_copilot.data.versions import normalize_platform, parse_version

GB = 1024**3
LAPTOP_HINTS = ("book", "laptop", "xps", "thinkpad", "latitude", "elitebook", "zenbook", "surface laptop", "spectre")
MANUFACTURER_HINTS = {
    "macbook": "Apple", "imac": "Apple", "mac ": "Apple", "mac mini": "Apple", "mac studio": "Apple",
    "thinkpad": "Lenovo", "ideapad": "Lenovo", "xps": "Dell", "latitude": "Dell", "inspiron": "Dell",
    "elitebook": "HP", "probook": "HP", "spectre": "HP", "surface": "Microsoft", "zenbook": "ASUS",
}


class LoadError(RuntimeError):
    pass


def derive_manufacturer(model_name: str | None) -> str | None:
    if not model_name:
        return None
    lower = model_name.lower() + " "
    for hint, maker in MANUFACTURER_HINTS.items():
        if lower.startswith(hint) or f" {hint}" in f" {lower}":
            return maker
    return model_name.split()[0]


def derive_is_laptop(model_name: str | None, battery_present: bool | None) -> bool:
    if battery_present:
        return True
    lower = (model_name or "").lower()
    return any(h in lower for h in LAPTOP_HINTS)


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _system_volume(volumes: list[dict]) -> dict | None:
    for vol in volumes:
        mount = (vol.get("mount_point") or "").rstrip("\\")
        if mount in ("/", "C:", "c:"):
            return vol
    return max(volumes, key=lambda v: v.get("size_bytes") or 0) if volumes else None


def _pct(part: float | None, whole: float | None) -> float | None:
    if part is None or not whole:
        return None
    return round(100.0 * part / whole, 2)


def iter_records(path: Path) -> Iterable[tuple[int, dict]]:
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield lineno, json.loads(line)
            except json.JSONDecodeError as exc:
                raise LoadError(f"{path}:{lineno}: invalid JSON ({exc})") from exc


def load(raw_paths: list[Path], db_path: Path, registered_tenants: Iterable[str] = (), rebuild: bool = False) -> dict[str, Any]:
    """Load one or more NDJSON files. Returns the data-quality report.

    By default the load appends: snapshots already in the database are skipped and only new ones
    are added, in one transaction, so a running session keeps its view (it reads up to its own
    as-of time). `rebuild=True`, or a missing database, builds a fresh one from the files."""
    for p in raw_paths:
        if not p.is_file():
            raise LoadError(f"Raw data not found: {p}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    fresh = rebuild or not db_path.exists()
    tmp = db_path.with_suffix(".tmp")
    seen_ids: dict[str, str] = {}
    existing_devices: dict[str, dict] = {}
    if fresh:
        if tmp.exists():
            tmp.unlink()
        conn = sqlite3.connect(tmp)
        conn.executescript(SCHEMA)
    else:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        seen_ids = {r["snapshot_id"]: r["collected_at"] for r in conn.execute("SELECT snapshot_id, collected_at FROM snapshots")}
        existing_devices = {r["device_id"]: dict(r) for r in conn.execute("SELECT * FROM devices")}
        conn.row_factory = None
    preloaded = set(seen_ids)
    already_loaded = 0
    exact_dupes = 0
    report: dict[str, Any] = {"sources": [str(p) for p in raw_paths]}
    per_device: dict[str, list[dict]] = defaultdict(list)
    device_info: dict[str, dict] = {}
    check_catalog: Counter = Counter()
    severities: dict[str, set] = defaultdict(set)
    conditions: Counter = Counter()
    platforms: Counter = Counter()
    encrypted_mismatch: list[str] = []
    rows = 0

    for p in raw_paths:
        for lineno, rec in iter_records(p):
            rows += 1
            try:
                device_id = clean_text(rec["device_id"])
                tenant_id = clean_text(rec["company_id"])
                collected_at = rec["collected_at"]
                ts = _parse_ts(collected_at)
            except (KeyError, ValueError) as exc:
                raise LoadError(f"{p}:{lineno}: missing or invalid key field ({exc})") from exc
            day = ts.date().isoformat()
            snapshot_id = f"{device_id}@{day}"
            if snapshot_id in seen_ids and seen_ids[snapshot_id] != collected_at:
                snapshot_id = f"{device_id}@{ts.strftime('%Y-%m-%dT%H%M')}"  # second snapshot on the same day
            if seen_ids.get(snapshot_id) == collected_at:
                if snapshot_id in preloaded:
                    already_loaded += 1  # loaded by an earlier run
                else:
                    exact_dupes += 1
                continue
            seen_ids[snapshot_id] = collected_at
            if device_id in existing_devices and existing_devices[device_id]["tenant_id"] != tenant_id:
                raise LoadError(f"{p}:{lineno}: device {device_id} appears under two tenants")

            os_ = rec.get("os") or {}
            ident = rec.get("device_identity") or {}
            mem = rec.get("memory") or {}
            bat = rec.get("battery") or {}
            volumes = rec.get("disk_volumes") or []
            platform = normalize_platform(os_.get("platform")) or clean_text(os_.get("platform"))
            os_version = clean_text(os_.get("product_version"))
            parsed = parse_version(platform, os_version) or (None, None, None)
            model_name = clean_text(ident.get("model_name"))
            hostname = clean_text(os_.get("hostname"))
            employee_id = clean_text(rec.get("employee_id"))

            ram_total = mem.get("total_memory_bytes") or mem.get("ram_bytes")
            ram_used = mem.get("used_memory_bytes")
            sysvol = _system_volume(volumes)
            disk_total = sysvol.get("size_bytes") if sysvol else None
            disk_free = sysvol.get("available_bytes") if sysvol else None
            disk_enc = sysvol.get("encrypted") if sysvol else None

            battery_present = bat.get("battery_present")
            condition = clean_text(bat.get("condition")) if battery_present else None
            if battery_present:
                conditions[condition] += 1
            platforms[platform] += 1

            conn.execute(
                "INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    snapshot_id, device_id, tenant_id, employee_id, collected_at, day, lineno,
                    clean_text(rec.get("agent_version")), hostname, platform, clean_text(os_.get("product_name")),
                    os_version, parsed[0], parsed[1], parsed[2],
                    round(ram_total / GB, 1) if ram_total else None, _pct(ram_used, ram_total),
                    round(disk_total / GB, 1) if disk_total else None,
                    round(disk_free / GB, 1) if disk_free is not None else None,
                    _pct(disk_free, disk_total), None if disk_enc is None else int(bool(disk_enc)),
                    None if battery_present is None else int(bool(battery_present)), condition,
                    bat.get("cycle_count") if battery_present else None,
                    bat.get("full_charge_capacity") if battery_present else None,
                    bat.get("percentage") if battery_present else None,
                    clean_text(bat.get("charging_status")) if battery_present else None,
                ),
            )
            for vol in volumes:
                conn.execute(
                    "INSERT INTO disk_volumes VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        snapshot_id, tenant_id, clean_text(vol.get("volume_name")), clean_text(vol.get("mount_point")),
                        clean_text(vol.get("file_system")),
                        round((vol.get("size_bytes") or 0) / GB, 1), round((vol.get("available_bytes") or 0) / GB, 1),
                        None if vol.get("encrypted") is None else int(bool(vol.get("encrypted"))), int(vol is sysvol),
                    ),
                )
            for sw in rec.get("installed_software") or []:
                name = clean_text(sw.get("name"))
                if not name:
                    continue
                conn.execute(
                    "INSERT INTO software VALUES (?,?,?,?,?,?,?,?)",
                    (snapshot_id, device_id, tenant_id, collected_at, name, normalize_name(name),
                     clean_text(sw.get("version")), clean_text(sw.get("publisher"))),
                )
            enc_check = None
            for cr in rec.get("compliance_results") or []:
                check_id = clean_text(cr.get("check_id"))
                status = (clean_text(cr.get("status")) or "").lower()
                severity = (clean_text(cr.get("severity")) or "").lower() or None
                if not check_id:
                    continue
                check_catalog[check_id] += 1
                severities[check_id].add(severity)
                if check_id == "disk_encryption":
                    enc_check = status
                conn.execute(
                    "INSERT INTO compliance_results VALUES (?,?,?,?,?,?,?)",
                    (snapshot_id, device_id, tenant_id, collected_at, check_id, status, severity),
                )
            if enc_check is not None and disk_enc is not None and (enc_check == "pass") != bool(disk_enc):
                encrypted_mismatch.append(snapshot_id)

            per_device[device_id].append({"ts": ts, "cycles": bat.get("cycle_count") if battery_present else None})
            info = device_info.setdefault(device_id, {"tenant_id": tenant_id})
            if info["tenant_id"] != tenant_id:
                raise LoadError(f"{p}:{lineno}: device {device_id} appears under two tenants")
            info.update({
                "employee_id": employee_id, "hostname": hostname, "model_name": model_name,
                "model_identifier": clean_text(ident.get("model_identifier")), "platform": platform,
                "battery_present": battery_present,
            })

    gaps: dict[str, list[str]] = {}
    cycle_decreases: dict[str, int] = {}
    for device_id, snaps in per_device.items():
        snaps.sort(key=lambda s: s["ts"])
        info = device_info[device_id]
        days = sorted({s["ts"].date() for s in snaps})
        missing = []
        for a, b in zip(days, days[1:], strict=False):
            for k in range(1, (b - a).days):
                missing.append(date.fromordinal(a.toordinal() + k).isoformat())
        if missing:
            gaps[device_id] = missing
        dec = sum(1 for a, b in zip(snaps, snaps[1:], strict=False) if a["cycles"] is not None and b["cycles"] is not None and b["cycles"] < a["cycles"])
        if dec:
            cycle_decreases[device_id] = dec
        first, last, count = snaps[0]["ts"].isoformat(), snaps[-1]["ts"].isoformat(), len(snaps)
        prev = existing_devices.get(device_id)
        if prev:
            first, count = min(first, prev["first_seen"]), count + prev["snapshot_count"]
            if prev["last_seen"] >= last:  # older data arriving late: keep the newer identity fields
                conn.execute("UPDATE devices SET first_seen = ?, snapshot_count = ? WHERE device_id = ?", (first, count, device_id))
                continue
        conn.execute(
            "INSERT OR REPLACE INTO devices VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                device_id, info["tenant_id"], info["employee_id"], info["hostname"], info["model_name"],
                info["model_identifier"], derive_manufacturer(info["model_name"]), info["platform"],
                int(derive_is_laptop(info["model_name"], info["battery_present"])), first, last, count,
            ),
        )

    as_of = conn.execute("SELECT max(collected_at) FROM snapshots").fetchone()[0]
    if as_of is None:
        raise LoadError("No snapshots found in the raw data")
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('as_of', ?)", (as_of,))
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('sources', ?)", (json.dumps(report["sources"]),))
    tenants_in_data = sorted(r[0] for r in conn.execute("SELECT DISTINCT tenant_id FROM devices"))
    total_devices = conn.execute("SELECT count(*) FROM devices").fetchone()[0]
    conn.commit()
    conn.close()
    if fresh:
        if db_path.exists():
            db_path.unlink()
        tmp.rename(db_path)

    registered = set(registered_tenants)
    report.update({
        "mode": "rebuild" if fresh else "append",
        "rows": rows,
        "new_snapshots": rows - exact_dupes - already_loaded,
        "already_loaded": already_loaded,
        "exact_duplicates_dropped": exact_dupes,
        "devices": total_devices,
        "tenants_in_data": tenants_in_data,
        "unregistered_tenants": [t for t in tenants_in_data if registered and t not in registered],
        "as_of": as_of,
        "platforms": dict(platforms),
        "battery_conditions": {str(k): v for k, v in conditions.items()},
        "compliance_checks": {k: {"rows": v, "severities": sorted(s for s in severities[k] if s)} for k, v in check_catalog.items()},
        "missing_days": gaps,
        "cycle_count_decreases": cycle_decreases,
        "encrypted_flag_mismatches": encrypted_mismatch,
    })
    return report
