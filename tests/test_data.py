"""Data layer: loading, sanitization (TC-16), data minimization (TC-19), tenant scoping (TC-03)."""

import sqlite3

import pytest

from fleet_copilot.data.loader import derive_is_laptop, derive_manufacturer, load
from fleet_copilot.data.repository import Repository, TenantViolation, dataset_as_of, open_readonly
from fleet_copilot.data.sanitize import clean_text, normalize_name
from fleet_copilot.data.versions import format_version, parse_version


def test_version_parsing():
    assert parse_version("darwin", "14.5") == (14, 5, 0)
    assert parse_version("darwin", "9.1") < parse_version("darwin", "15")
    assert parse_version("win32", "11 22H2") == (11, 22, 2)
    assert parse_version("win32", "11 23H2") > parse_version("windows", "11 22H2")
    assert parse_version("darwin", "garbage") is None
    assert format_version("win32", (11, 22, 2)) == "11 22H2"


def test_sanitize():
    assert clean_text("Long\x07Name​x") == "LongNamex"
    assert len(clean_text("x" * 300)) == 100
    assert normalize_name("µTorrent") == "utorrent"


def test_derivations():
    assert derive_manufacturer("MacBook Pro") == "Apple"
    assert derive_manufacturer("Dell XPS 15") == "Dell"
    assert derive_manufacturer("ThinkPad X1 Carbon") == "Lenovo"
    assert derive_is_laptop("Dell XPS 15", None) is True
    assert derive_is_laptop("OptiPlex 7010", False) is False


def test_load_report(fixture_file, tmp_path):
    report = load([fixture_file], tmp_path / "t.db", registered_tenants=["fixture-999"])
    assert report["as_of"].startswith("2026-06-12")
    assert report["unregistered_tenants"] == ["fixture-998"]
    assert "Service Recommended" in report["battery_conditions"]
    assert "FX-STALE-01" not in report["missing_days"]


def test_never_loaded_fields(loaded_db):
    conn = sqlite3.connect(loaded_db)
    cols = " ".join(r[1] for t in ("snapshots", "devices", "disk_volumes", "software") for r in conn.execute(f"PRAGMA table_info({t})"))
    for banned in ("mac", "address", "uuid", "serial", "page_size", "interface"):
        assert banned not in cols.split(), banned
    blob = " ".join(str(v) for row in conn.execute("SELECT * FROM snapshots") for v in row)
    assert "00:00:00:00:00:00" not in blob and "10.0.0.1" not in blob


def test_sanitized_on_load(repo):
    names = [r["name"] for r in repo.software_latest("FX-LONG-01")]
    long = [n for n in names if n.startswith("Long")][0]
    assert len(long) <= 100 and "\x07" not in long and "​" not in long


def test_repository_tenant_scope(repo):
    for method in (repo.devices, repo.latest_snapshots, repo.all_snapshots, repo.compliance_latest,
                   repo.compliance_history, repo.software_latest, repo.software_history):
        rows = method()
        assert rows, method.__name__
        assert all(r["tenant_id"] == "fixture-999" for r in rows if "tenant_id" in r), method.__name__
    assert repo.device("FX-OTHER-01") is None
    assert repo.latest_snapshot("FX-OTHER-01") is None
    assert repo.employee_devices("emp-other-2001") == []


def test_repository_fails_closed_on_foreign_rows(loaded_db):
    conn = open_readonly(loaded_db)
    repo = Repository(conn, "fixture-999", dataset_as_of(loaded_db))
    with pytest.raises(TenantViolation):
        repo._query("SELECT * FROM devices WHERE tenant_id = ? OR 1=1", ("fixture-999",))
    with pytest.raises(TenantViolation):
        repo._query("SELECT * FROM devices", ())


def test_repository_is_read_only(loaded_db):
    conn = open_readonly(loaded_db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM devices")


def test_as_of_override(loaded_db):
    conn = open_readonly(loaded_db)
    repo = Repository(conn, "fixture-999", "2026-06-05T23:59:59Z")
    snap = repo.latest_snapshot("FX-DISK-01")
    assert snap["day"] == "2026-06-05"
