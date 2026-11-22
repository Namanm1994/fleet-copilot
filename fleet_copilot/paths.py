"""Filesystem locations. Everything runtime lives under var/ (gitignored)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def config(self) -> Path:
        return self.root / "config"

    @property
    def raw_data(self) -> Path:
        return self.root / "data" / "raw" / "telemetry.ndjson"

    @property
    def var(self) -> Path:
        return self.root / "var"

    @property
    def telemetry_db(self) -> Path:
        return self.var / "telemetry.db"

    @property
    def app_db(self) -> Path:
        return self.var / "app.db"

    @property
    def checkpoints_db(self) -> Path:
        return self.var / "checkpoints.db"

    @property
    def data_quality(self) -> Path:
        return self.var / "data_quality.json"

    @property
    def audit_log(self) -> Path:
        return self.var / "audit.jsonl"

    @property
    def outbox(self) -> Path:
        return self.var / "outbox.jsonl"

    @property
    def traces(self) -> Path:
        return self.var / "traces"

    @property
    def cassettes(self) -> Path:
        return self.root / "evals" / "cassettes"


def default_paths() -> Paths:
    root = os.environ.get("FLEET_HOME")
    return Paths(Path(root).resolve() if root else PROJECT_ROOT)


def ensure_private_dir(path: Path) -> None:
    """Create a directory readable by the owner only where the OS supports it."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def touch_private(path: Path) -> None:
    """Create a file with owner-only permissions where the OS supports it (no-op on Windows ACLs)."""
    ensure_private_dir(path.parent)
    if not path.exists():
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
