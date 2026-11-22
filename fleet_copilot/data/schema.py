"""SQLite schema for the derived telemetry database (rebuilt from the raw NDJSON)."""

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE devices (
    device_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    employee_id TEXT,
    hostname TEXT,
    model_name TEXT,
    model_identifier TEXT,
    manufacturer TEXT,
    platform TEXT,
    is_laptop INTEGER NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    snapshot_count INTEGER NOT NULL
);
CREATE INDEX ix_devices_tenant ON devices(tenant_id);

CREATE TABLE snapshots (
    snapshot_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    employee_id TEXT,
    collected_at TEXT NOT NULL,
    day TEXT NOT NULL,
    source_line INTEGER NOT NULL,
    agent_version TEXT,
    hostname TEXT,
    os_platform TEXT,
    os_name TEXT,
    os_version TEXT,
    os_v1 INTEGER, os_v2 INTEGER, os_v3 INTEGER,
    ram_total_gb REAL,
    ram_used_pct REAL,
    disk_total_gb REAL,
    disk_free_gb REAL,
    disk_free_pct REAL,
    disk_encrypted INTEGER,
    battery_present INTEGER,
    battery_condition TEXT,
    battery_cycle_count INTEGER,
    battery_full_charge_capacity REAL,
    battery_percentage REAL,
    battery_charging_status TEXT
);
CREATE INDEX ix_snap_tenant_dev ON snapshots(tenant_id, device_id, collected_at);

CREATE TABLE disk_volumes (
    snapshot_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    volume_name TEXT,
    mount_point TEXT,
    file_system TEXT,
    size_gb REAL,
    free_gb REAL,
    encrypted INTEGER,
    is_system INTEGER NOT NULL
);

CREATE TABLE software (
    snapshot_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    name TEXT NOT NULL,
    name_norm TEXT NOT NULL,
    version TEXT,
    publisher TEXT
);
CREATE INDEX ix_sw_tenant ON software(tenant_id, name_norm);

CREATE TABLE compliance_results (
    snapshot_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    check_id TEXT NOT NULL,
    status TEXT NOT NULL,
    severity TEXT
);
CREATE INDEX ix_cr_tenant ON compliance_results(tenant_id, device_id, check_id);
"""
