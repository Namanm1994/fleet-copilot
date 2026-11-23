"""Proposals are numbered per company, so a number reveals nothing about other companies."""

from __future__ import annotations

import sqlite3

from fleet_copilot.store import AppStore
from tests.test_tools import make_tk

OLD_SCHEMA = """
CREATE TABLE proposals (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, session_id TEXT NOT NULL, action TEXT NOT NULL,
    device_id TEXT, employee_id TEXT, params TEXT NOT NULL, evidence_ids TEXT NOT NULL, origin TEXT NOT NULL,
    dedup_key TEXT NOT NULL, status TEXT NOT NULL, reason TEXT, preview TEXT, warnings TEXT, created_at TEXT NOT NULL,
    decided_at TEXT, decided_by TEXT, reject_reason TEXT, executed_at TEXT, error TEXT);
CREATE INDEX ix_prop_tenant ON proposals(tenant_id, status);
"""


def _new(store: AppStore, key: str) -> dict:
    return store.create(session_id="s", action="open_remediation_ticket", device_id="D1", employee_id=None, params={},
                        evidence_ids=["e"], origin="requested", dedup_key=key, reason="r", preview="p", warnings=[])


def test_each_company_starts_at_one(tmp_path):
    db = tmp_path / "app.db"
    acme, globex = AppStore(db, "acme-001"), AppStore(db, "globex-002")
    assert [_new(acme, f"a{i}")["proposal_id"] for i in range(3)] == ["PRP-1", "PRP-2", "PRP-3"]
    assert _new(globex, "g1")["proposal_id"] == "PRP-1"  # not PRP-4: Acme's activity is not visible in the number
    assert _new(acme, "a9")["proposal_id"] == "PRP-4"
    assert [p["proposal_id"] for p in globex.list()] == ["PRP-1"] and len(acme.list()) == 4
    assert globex.get("PRP-1")["dedup_key"] == "g1" and acme.get("PRP-1")["dedup_key"] == "a0"
    assert globex.get("PRP-2") is None  # Acme's PRP-2 does not exist for Globex


def test_same_number_in_two_companies_executes_independently(home):
    a, b = make_tk(home, "fixture-999"), make_tk(home, "fixture-998")
    a.call("get_device", {"device_id": "FX-DRIFT-01"})
    b.call("get_device", {"device_id": "FX-OTHER-01"})
    pa = a.call("open_remediation_ticket", {"device_id": "FX-DRIFT-01", "check_id": "screen_lock",
                                            "evidence_ids": ["FX-DRIFT-01@2026-06-12#screen_lock"]})
    pb = b.call("open_remediation_ticket", {"device_id": "FX-OTHER-01", "check_id": "disk_encryption",
                                            "evidence_ids": ["FX-OTHER-01@2026-06-12#disk_encryption"]})
    assert pa.ok and pb.ok and pa.data["proposal_id"] == pb.data["proposal_id"] == "PRP-1"
    assert a.executor.approve("PRP-1", "x")["status"] == "executed"
    assert b.store.get("PRP-1")["status"] == "proposed"  # the other company's PRP-1 is untouched
    assert b.executor.approve("PRP-1", "y")["status"] == "executed"  # and is not mistaken for already delivered
    lines = home.outbox.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and {("fixture-999" in line, "fixture-998" in line) for line in lines} == {(True, False), (False, True)}


def test_existing_database_is_migrated_and_keeps_its_numbers(tmp_path):
    db = tmp_path / "app.db"
    conn = sqlite3.connect(db)
    conn.executescript(OLD_SCHEMA)
    for tenant, n in (("acme-001", 5), ("globex-002", 3)):  # one shared sequence: Acme 1-5, Globex 6-8
        for i in range(n):
            conn.execute("INSERT INTO proposals (tenant_id, session_id, action, params, evidence_ids, origin, dedup_key, status,"
                         " created_at) VALUES (?, 's', 'open_remediation_ticket', '{}', '[]', 'scan', ?, ?, 't')",
                         (tenant, f"{tenant}-{i}", "executed" if (tenant, i) == ("acme-001", 0) else "proposed"))
    conn.commit()
    conn.close()
    acme, globex = AppStore(db, "acme-001"), AppStore(db, "globex-002")
    assert [p["proposal_id"] for p in acme.list()] == ["PRP-1", "PRP-2", "PRP-3", "PRP-4", "PRP-5"]
    assert [p["proposal_id"] for p in globex.list()] == ["PRP-6", "PRP-7", "PRP-8"]  # history keeps its numbers
    assert acme.get("PRP-1")["status"] == "executed"
    assert _new(acme, "next")["proposal_id"] == "PRP-6" and _new(globex, "next")["proposal_id"] == "PRP-9"
    assert _new(AppStore(db, "initech-003"), "first")["proposal_id"] == "PRP-1"  # a company with no history starts at 1
    AppStore(db, "acme-001")  # opening again does not migrate twice or lose anything
    assert len(acme.list()) == 6
