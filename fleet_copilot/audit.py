"""Append-only, hash-chained audit log (G32, G34).

Each entry stores `prev` (hash of the previous entry) and `hash` (SHA-256 of its own
canonical JSON including `prev`). `verify()` reports the first broken line. This makes
tampering detectable, not impossible: someone with file access could rewrite the chain.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fleet_copilot.paths import touch_private

GENESIS = "0" * 64


def _canonical(entry: dict[str, Any]) -> str:
    return json.dumps({k: v for k, v in entry.items() if k != "hash"}, sort_keys=True, ensure_ascii=False, default=str)


def _digest(entry: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(entry).encode("utf-8")).hexdigest()


@contextlib.contextmanager
def file_lock(path: Path, timeout_s: float = 5.0) -> Iterator[None]:
    """Cross-platform lock via an exclusive lock file."""
    lock = path.with_suffix(path.suffix + ".lock")
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            if time.monotonic() > deadline:
                raise TimeoutError(f"Could not lock {path}") from None
            time.sleep(0.02)
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(lock)


class AuditLog:
    def __init__(self, path: Path):
        self.path = path

    def _last_hash(self) -> str:
        if not self.path.exists():
            return GENESIS
        last = None
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    last = line
        return json.loads(last)["hash"] if last else GENESIS

    def append(self, event: str, **fields: Any) -> dict[str, Any]:
        touch_private(self.path)
        with file_lock(self.path):
            entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **fields}
            entry["prev"] = self._last_hash()
            entry["hash"] = _digest(entry)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        return entry

    def entries(self, tenant_id: str | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [e for e in out if tenant_id is None or e.get("tenant") == tenant_id]


def verify(path: Path) -> tuple[bool, int, str]:
    """Recompute the chain. Returns (ok, entries_checked, message)."""
    if not path.exists():
        return True, 0, "OK (0 entries)"
    prev = GENESIS
    n = 0
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            return False, n, f"line {lineno}: not valid JSON"
        if entry.get("prev") != prev:
            return False, n, f"line {lineno}: prev mismatch (an entry before it was deleted, inserted or reordered)"
        if _digest(entry) != entry.get("hash"):
            return False, n, f"line {lineno}: hash mismatch (entry was edited)"
        prev = entry["hash"]
        n += 1
    return True, n, f"OK ({n} entries)"
