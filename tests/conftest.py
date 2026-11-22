"""Shared fixtures. Tests use the synthetic fixture dataset only (no real data needed)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from fleet_copilot.config import load_app_config
from fleet_copilot.data.loader import load
from fleet_copilot.data.repository import Repository, dataset_as_of, open_readonly
from fleet_copilot.paths import PROJECT_ROOT, Paths

FIXTURE = PROJECT_ROOT / "evals" / "fixtures" / "fixture.ndjson"


@pytest.fixture(scope="session")
def fixture_file() -> Path:
    if not FIXTURE.exists():
        from evals.fixtures.generate import write_fixture

        write_fixture(FIXTURE)
    return FIXTURE


@pytest.fixture(scope="session")
def loaded_db(tmp_path_factory, fixture_file) -> Path:
    db = tmp_path_factory.mktemp("db") / "telemetry.db"
    load([fixture_file], db)
    return db


@pytest.fixture()
def app_config():
    return load_app_config(PROJECT_ROOT / "config", "policy.eval.yaml")


@pytest.fixture()
def repo(loaded_db) -> Repository:
    conn = open_readonly(loaded_db)
    return Repository(conn, "fixture-999", dataset_as_of(loaded_db))


@pytest.fixture()
def home(tmp_path, loaded_db) -> Paths:
    """An isolated project home: copied config, loaded DB, empty var/."""
    shutil.copytree(PROJECT_ROOT / "config", tmp_path / "config")
    paths = Paths(tmp_path)
    paths.var.mkdir()
    shutil.copy(loaded_db, paths.telemetry_db)
    return paths
