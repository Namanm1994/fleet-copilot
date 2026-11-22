"""`fleet profile`: summarize the loaded data so thresholds can be calibrated (gap F-02)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


def _quantiles(values: list[float]) -> dict[str, float] | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None

    def q(p: float) -> float:
        idx = min(len(vals) - 1, max(0, round(p * (len(vals) - 1))))
        return round(vals[idx], 2)

    return {"min": vals[0], "p05": q(0.05), "p50": q(0.5), "p95": q(0.95), "max": vals[-1], "n": len(vals)}


def profile(db_path: Path) -> dict[str, Any]:
    """Cross-tenant summary for the operator running the CLI. It never reaches the LLM."""
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        out: dict[str, Any] = {}
        out["as_of"] = cur.execute("SELECT value FROM meta WHERE key='as_of'").fetchone()[0]
        out["tenants"] = {
            t: n for t, n in cur.execute("SELECT tenant_id, count(*) FROM devices GROUP BY 1 ORDER BY 1")
        }
        out["snapshots"] = cur.execute("SELECT count(*) FROM snapshots").fetchone()[0]
        out["window"] = list(cur.execute("SELECT min(collected_at), max(collected_at) FROM snapshots").fetchone())
        out["models"] = {m: n for m, n in cur.execute("SELECT model_name, count(*) FROM devices GROUP BY 1 ORDER BY 2 DESC")}
        out["manufacturers"] = {m: n for m, n in cur.execute("SELECT manufacturer, count(*) FROM devices GROUP BY 1")}
        out["laptops"] = cur.execute("SELECT sum(is_laptop), count(*) FROM devices").fetchone()
        out["os_versions"] = {
            f"{p} {v}": n for p, v, n in cur.execute(
                "SELECT os_platform, os_version, count(DISTINCT device_id) FROM snapshots GROUP BY 1, 2 ORDER BY 1, 2")
        }
        out["ram_used_pct"] = _quantiles([r[0] for r in cur.execute("SELECT ram_used_pct FROM snapshots")])
        out["disk_free_pct"] = _quantiles([r[0] for r in cur.execute("SELECT disk_free_pct FROM snapshots")])
        out["battery_conditions"] = {
            str(c): n for c, n in cur.execute(
                "SELECT battery_condition, count(*) FROM snapshots WHERE battery_present = 1 GROUP BY 1")
        }
        out["battery_cycles"] = _quantiles([r[0] for r in cur.execute("SELECT battery_cycle_count FROM snapshots")])
        out["compliance"] = {
            f"{c} ({s})": {"pass": p, "fail": f}
            for c, s, p, f in cur.execute(
                "SELECT check_id, severity, sum(status='pass'), sum(status='fail') FROM compliance_results GROUP BY 1, 2")
        }
        out["snapshots_per_device"] = _quantiles([r[0] for r in cur.execute("SELECT snapshot_count FROM devices")])
        out["ram_ge_95_devices"] = cur.execute(
            "SELECT count(DISTINCT device_id) FROM snapshots WHERE ram_used_pct >= 95").fetchone()[0]
        out["low_disk_latest_devices"] = cur.execute(
            """SELECT count(*) FROM snapshots s JOIN (SELECT device_id, max(collected_at) m FROM snapshots GROUP BY 1) l
               ON s.device_id = l.device_id AND s.collected_at = l.m WHERE s.disk_free_pct < 15""").fetchone()[0]
        return out
    finally:
        conn.close()
