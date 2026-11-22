"""Session traces: one JSONL file per session under var/traces/YYYY-MM-DD/.

Each event: {ts, session_id, tenant, turn, node, event, payload, tokens?, latency_ms?}.
Strings are redacted. Plans and stated reasons are logged, not hidden chain-of-thought.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fleet_copilot.paths import touch_private
from fleet_copilot.redact import redact


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


class Tracer:
    def __init__(self, traces_dir: Path, session_id: str, tenant_id: str, prompt_version: str = ""):
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.path = traces_dir / day / f"{session_id}.jsonl"
        self.session_id = session_id
        self.tenant_id = tenant_id
        self.prompt_version = prompt_version
        self.turn = 0
        self.events: list[dict[str, Any]] = []

    def emit(self, node: str, event: str, payload: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "session_id": self.session_id, "tenant": self.tenant_id, "turn": self.turn,
            "node": node, "event": event, "payload": _clean(payload or {}), **extra,
        }
        if event == "input":
            entry["prompt_version"] = self.prompt_version
        self.events.append(entry)
        touch_private(self.path)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        return entry


def find_trace(traces_dir: Path, session_id: str) -> Path | None:
    for p in sorted(traces_dir.glob(f"*/{session_id}.jsonl")):
        return p
    return None


def read_trace(path: Path, tenant_id: str | None = None) -> list[dict[str, Any]]:
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if tenant_id is not None and events and events[0].get("tenant") != tenant_id:
        raise PermissionError("Trace belongs to another tenant")
    return events
