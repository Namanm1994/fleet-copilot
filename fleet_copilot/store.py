"""Application state (proposals, acknowledgements) in its own database, separate from the
read-only telemetry database. Every query is scoped to the session tenant."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fleet_copilot.paths import touch_private

OPEN_STATUSES = ("proposed", "pending", "approved", "executed", "failed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    action TEXT NOT NULL,
    device_id TEXT,
    employee_id TEXT,
    params TEXT NOT NULL,
    evidence_ids TEXT NOT NULL,
    origin TEXT NOT NULL,
    dedup_key TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    preview TEXT,
    warnings TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    reject_reason TEXT,
    executed_at TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS ix_prop_tenant ON proposals(tenant_id, status);
CREATE TABLE IF NOT EXISTS usage (
    day TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    turns INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, tenant_id)
);
CREATE TABLE IF NOT EXISTS acknowledgements (
    tenant_id TEXT NOT NULL,
    insight_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    until TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, insight_id)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pid(seq: int) -> str:
    return f"PRP-{seq}"


def seq_of(proposal_id: str) -> int | None:
    try:
        return int(proposal_id.split("-", 1)[1]) if proposal_id.startswith("PRP-") else None
    except ValueError:
        return None


class AppStore:
    def __init__(self, path: Path, tenant_id: str):
        touch_private(path)
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self.tenant_id = tenant_id

    def _row(self, r: sqlite3.Row) -> dict[str, Any]:
        d = {k: r[k] for k in r.keys()}
        d["proposal_id"] = pid(d["seq"])
        d["params"] = json.loads(d["params"])
        d["evidence_ids"] = json.loads(d["evidence_ids"])
        d["warnings"] = json.loads(d["warnings"] or "[]")
        if d["tenant_id"] != self.tenant_id:
            raise RuntimeError("Proposal from another tenant reached the store boundary")
        return d

    def create(self, *, session_id: str, action: str, device_id: str | None, employee_id: str | None,
               params: dict, evidence_ids: list[str], origin: str, dedup_key: str, reason: str,
               preview: str, warnings: list[str]) -> dict[str, Any]:
        cur = self._conn.execute(
            "INSERT INTO proposals (tenant_id, session_id, action, device_id, employee_id, params, evidence_ids, origin,"
            " dedup_key, status, reason, preview, warnings, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.tenant_id, session_id, action, device_id, employee_id, json.dumps(params), json.dumps(evidence_ids),
             origin, dedup_key, "proposed", reason, preview, json.dumps(warnings), now_iso()),
        )
        self._conn.commit()
        return self.get(pid(cur.lastrowid))  # type: ignore[return-value]

    def get(self, proposal_id: str) -> dict[str, Any] | None:
        seq = seq_of(proposal_id)
        if seq is None:
            return None
        r = self._conn.execute("SELECT * FROM proposals WHERE tenant_id = ? AND seq = ?", (self.tenant_id, seq)).fetchone()
        return self._row(r) if r else None

    def find_open(self, dedup_key: str) -> dict[str, Any] | None:
        marks = ",".join("?" * len(OPEN_STATUSES))
        r = self._conn.execute(
            f"SELECT * FROM proposals WHERE tenant_id = ? AND dedup_key = ? AND status IN ({marks}) ORDER BY seq DESC LIMIT 1",
            (self.tenant_id, dedup_key, *OPEN_STATUSES),
        ).fetchone()
        return self._row(r) if r else None

    def list(self, status: str | None = None, device_id: str | None = None) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM proposals WHERE tenant_id = ?", [self.tenant_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        if device_id:
            sql += " AND device_id = ?"
            params.append(device_id)
        return [self._row(r) for r in self._conn.execute(sql + " ORDER BY seq", params)]

    def update(self, proposal_id: str, **fields: Any) -> dict[str, Any]:
        seq = seq_of(proposal_id)
        if not fields or seq is None:
            raise ValueError("nothing to update")
        cols = ", ".join(f"{k} = ?" for k in fields)
        cur = self._conn.execute(
            f"UPDATE proposals SET {cols} WHERE tenant_id = ? AND seq = ?", (*fields.values(), self.tenant_id, seq)
        )
        if cur.rowcount != 1:
            raise KeyError(proposal_id)
        self._conn.commit()
        return self.get(proposal_id)  # type: ignore[return-value]

    def add_acknowledgement(self, insight_id: str, reason: str, until: str, proposal_id: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO acknowledgements VALUES (?,?,?,?,?,?)",
            (self.tenant_id, insight_id, reason, until, proposal_id, now_iso()),
        )
        self._conn.commit()

    def acknowledgements(self) -> dict[str, dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM acknowledgements WHERE tenant_id = ?", (self.tenant_id,)).fetchall()
        return {r["insight_id"]: {k: r[k] for k in r.keys()} for r in rows}

    # ---- usage ledger (daily cap) ----------------------------------------------------------------
    def add_usage(self, day: str, input_tokens: int, output_tokens: int) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO usage (day, tenant_id, input_tokens, output_tokens, turns) VALUES (?,?,?,?,1) "
                "ON CONFLICT(day, tenant_id) DO UPDATE SET input_tokens = input_tokens + excluded.input_tokens, "
                "output_tokens = output_tokens + excluded.output_tokens, turns = turns + 1",
                (day, self.tenant_id, int(input_tokens), int(output_tokens)))

    def usage(self, day: str) -> dict[str, int]:
        row = self._conn.execute("SELECT input_tokens, output_tokens, turns FROM usage WHERE day = ? AND tenant_id = ?",
                                 (day, self.tenant_id)).fetchone()
        return {"input_tokens": row[0], "output_tokens": row[1], "turns": row[2]} if row else {"input_tokens": 0, "output_tokens": 0, "turns": 0}
