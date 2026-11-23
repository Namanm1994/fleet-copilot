"""`fleet load` appends new snapshots and keeps an open session's view."""

from __future__ import annotations

import json
import sqlite3

from fleet_copilot.data.loader import load
from fleet_copilot.data.repository import Repository, open_readonly


# ---- incremental load ---------------------------------------------------------------------------
def _split(fixture_file, tmp_path, cutoff="2026-06-08"):
    old, new = [], []
    for line in fixture_file.read_text(encoding="utf-8").splitlines():
        if line.strip():
            (old if json.loads(line)["collected_at"][:10] < cutoff else new).append(line)
    a, b = tmp_path / "old.ndjson", tmp_path / "new.ndjson"
    a.write_text("\n".join(old) + "\n", encoding="utf-8")
    b.write_text("\n".join(new) + "\n", encoding="utf-8")
    return a, b, len(old), len(new)


def _dump(db):
    conn = sqlite3.connect(db)
    out = {t: sorted(conn.execute(f"SELECT * FROM {t}").fetchall(), key=repr)
           for t in ("snapshots", "devices", "software", "compliance_results", "disk_volumes")}
    conn.close()
    out["snapshots"] = [row[:6] + row[7:] for row in out["snapshots"]]  # source_line is the line in its own file
    return out


def test_load_appends_and_is_idempotent(fixture_file, tmp_path):
    a, b, n_old, n_new = _split(fixture_file, tmp_path)
    db = tmp_path / "t.db"
    r1 = load([a], db)
    assert r1["mode"] == "rebuild" and r1["new_snapshots"] == n_old
    r2 = load([b], db)
    assert r2["mode"] == "append" and r2["new_snapshots"] == n_new and r2["already_loaded"] == 0
    r3 = load([a, b], db)  # loading everything again adds nothing
    assert r3["new_snapshots"] == 0 and r3["already_loaded"] == n_old + n_new
    whole = tmp_path / "whole.db"
    load([fixture_file], whole)
    assert _dump(db) == _dump(whole)  # appending in two steps gives the same database as one load
    assert r3["as_of"] == load([fixture_file], whole, rebuild=True)["as_of"]


def test_running_session_keeps_its_view_when_data_is_appended(fixture_file, tmp_path):
    a, b, _, _ = _split(fixture_file, tmp_path)
    db = tmp_path / "t.db"
    as_of_before = load([a], db)["as_of"]
    repo = Repository(open_readonly(db), "fixture-999", as_of_before)
    before = repo.latest_snapshot("FX-DISK-01")
    load([b], db)  # new telemetry arrives while the session is open
    assert repo.latest_snapshot("FX-DISK-01") == before
    fresh = Repository(open_readonly(db), "fixture-999", "2026-06-12T23:59:59Z")
    assert fresh.latest_snapshot("FX-DISK-01")["snapshot_id"] == "FX-DISK-01@2026-06-12"


def test_load_rebuild_flag(fixture_file, tmp_path):
    db = tmp_path / "t.db"
    load([fixture_file], db)
    r = load([fixture_file], db, rebuild=True)
    assert r["mode"] == "rebuild" and r["already_loaded"] == 0 and r["new_snapshots"] > 0
