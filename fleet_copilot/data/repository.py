"""Tenant-scoped, read-only access to telemetry.

Every query is filtered by the session tenant here, and every returned row is checked
again before it leaves this module (defense in depth). Tools never get a raw connection.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


class TenantViolation(RuntimeError):
    """A row from another tenant reached the repository boundary. Fail closed."""


class DataUnavailable(RuntimeError):
    pass


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def open_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise DataUnavailable(f"Telemetry database not found at {db_path}. Run `fleet load` first.")
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def dataset_as_of(db_path: Path) -> str:
    conn = open_readonly(db_path)
    try:
        return conn.execute("SELECT value FROM meta WHERE key='as_of'").fetchone()[0]
    finally:
        conn.close()


def known_tenants(db_path: Path) -> list[str]:
    conn = open_readonly(db_path)
    try:
        return [r[0] for r in conn.execute("SELECT DISTINCT tenant_id FROM devices ORDER BY 1")]
    finally:
        conn.close()


class Repository:
    def __init__(self, conn: sqlite3.Connection, tenant_id: str, as_of: str):
        self._conn = conn
        self.tenant_id = tenant_id
        self.as_of = as_of

    # ---- internals -------------------------------------------------------------------------
    def close(self) -> None:
        self._conn.close()

    def _query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        if "tenant_id = ?" not in sql:
            raise TenantViolation("Repository query without tenant filter")
        rows = [_row_to_dict(r) for r in self._conn.execute(sql, params)]
        for row in rows:
            if "tenant_id" in row and row["tenant_id"] != self.tenant_id:
                raise TenantViolation(f"Row from tenant {row['tenant_id']!r} in a {self.tenant_id!r} session")
        return rows

    @property
    def as_of_dt(self) -> datetime:
        return datetime.fromisoformat(self.as_of.replace("Z", "+00:00"))

    def stale_cutoff(self, days: int) -> str:
        cutoff = self.as_of_dt - timedelta(days=days)
        return cutoff.isoformat().replace("+00:00", "Z")

    # ---- devices ---------------------------------------------------------------------------
    def devices(self) -> list[dict[str, Any]]:
        return self._query(
            "SELECT * FROM devices WHERE tenant_id = ? AND first_seen <= ? ORDER BY device_id",
            (self.tenant_id, self._as_of_cmp()),
        )

    def device(self, device_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM devices WHERE tenant_id = ? AND device_id = ?", (self.tenant_id, device_id))
        return rows[0] if rows else None

    def employee_devices(self, employee_id: str) -> list[dict[str, Any]]:
        return self._query(
            "SELECT * FROM devices WHERE tenant_id = ? AND employee_id = ?", (self.tenant_id, employee_id)
        )

    def _as_of_cmp(self) -> str:
        # Stored timestamps are ISO with "Z"; compare on the same representation.
        return self.as_of

    # ---- snapshots -------------------------------------------------------------------------
    def latest_snapshots(self) -> list[dict[str, Any]]:
        return self._query(
            """
            SELECT s.* FROM snapshots s
            JOIN (SELECT device_id, max(collected_at) AS m FROM snapshots
                  WHERE tenant_id = ? AND collected_at <= ? GROUP BY device_id) l
              ON s.device_id = l.device_id AND s.collected_at = l.m
            WHERE s.tenant_id = ?
            ORDER BY s.device_id
            """,
            (self.tenant_id, self._as_of_cmp(), self.tenant_id),
        )

    def latest_snapshot(self, device_id: str) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT * FROM snapshots WHERE tenant_id = ? AND device_id = ? AND collected_at <= ? "
            "ORDER BY collected_at DESC LIMIT 1",
            (self.tenant_id, device_id, self._as_of_cmp()),
        )
        return rows[0] if rows else None

    def snapshots(self, device_id: str, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        end_cmp = min(end, self._as_of_cmp()) if end else self._as_of_cmp()
        return self._query(
            "SELECT * FROM snapshots WHERE tenant_id = ? AND device_id = ? AND collected_at >= ? AND collected_at <= ? "
            "ORDER BY collected_at",
            (self.tenant_id, device_id, start or "", end_cmp),
        )

    def all_snapshots(self) -> list[dict[str, Any]]:
        return self._query(
            "SELECT * FROM snapshots WHERE tenant_id = ? AND collected_at <= ? ORDER BY device_id, collected_at",
            (self.tenant_id, self._as_of_cmp()),
        )

    def snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM snapshots WHERE tenant_id = ? AND snapshot_id = ?", (self.tenant_id, snapshot_id))
        return rows[0] if rows else None

    def data_window(self) -> tuple[str | None, str | None]:
        rows = self._query(
            "SELECT tenant_id, min(collected_at) AS lo, max(collected_at) AS hi FROM snapshots WHERE tenant_id = ? "
            "AND collected_at <= ? GROUP BY tenant_id",
            (self.tenant_id, self._as_of_cmp()),
        )
        return (rows[0]["lo"], rows[0]["hi"]) if rows else (None, None)

    # ---- compliance ------------------------------------------------------------------------
    def compliance_latest(self) -> list[dict[str, Any]]:
        return self._query(
            """
            SELECT c.* FROM compliance_results c
            JOIN (SELECT device_id, max(collected_at) AS m FROM snapshots
                  WHERE tenant_id = ? AND collected_at <= ? GROUP BY device_id) l
              ON c.device_id = l.device_id AND c.collected_at = l.m
            WHERE c.tenant_id = ?
            ORDER BY c.device_id, c.check_id
            """,
            (self.tenant_id, self._as_of_cmp(), self.tenant_id),
        )

    def compliance_history(self, device_id: str | None = None) -> list[dict[str, Any]]:
        if device_id:
            return self._query(
                "SELECT * FROM compliance_results WHERE tenant_id = ? AND device_id = ? AND collected_at <= ? "
                "ORDER BY check_id, collected_at",
                (self.tenant_id, device_id, self._as_of_cmp()),
            )
        return self._query(
            "SELECT * FROM compliance_results WHERE tenant_id = ? AND collected_at <= ? "
            "ORDER BY device_id, check_id, collected_at",
            (self.tenant_id, self._as_of_cmp()),
        )

    # ---- software --------------------------------------------------------------------------
    def software_latest(self, device_id: str | None = None) -> list[dict[str, Any]]:
        sql = """
            SELECT w.* FROM software w
            JOIN (SELECT device_id, max(collected_at) AS m FROM snapshots
                  WHERE tenant_id = ? AND collected_at <= ? GROUP BY device_id) l
              ON w.device_id = l.device_id AND w.collected_at = l.m
            WHERE w.tenant_id = ?
        """
        params: tuple = (self.tenant_id, self._as_of_cmp(), self.tenant_id)
        if device_id:
            sql += " AND w.device_id = ?"
            params += (device_id,)
        return self._query(sql + " ORDER BY w.device_id, w.name", params)

    def software_history(self, name_norm: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM software WHERE tenant_id = ? AND collected_at <= ?"
        params: tuple = (self.tenant_id, self._as_of_cmp())
        if name_norm:
            sql += " AND name_norm LIKE ?"
            params += (f"%{name_norm}%",)
        return self._query(sql + " ORDER BY device_id, collected_at", params)

    def volumes(self, snapshot_id: str) -> list[dict[str, Any]]:
        return self._query(
            "SELECT * FROM disk_volumes WHERE tenant_id = ? AND snapshot_id = ?", (self.tenant_id, snapshot_id)
        )
