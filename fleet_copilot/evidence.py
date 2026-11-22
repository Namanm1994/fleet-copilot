"""Evidence ID formats. Readable, stable across reloads, traceable to raw lines.

snapshot      1LYSSFD074BB@2026-05-22
check result  1LYSSFD074BB@2026-05-22#screen_lock
software      1LYSSFD074BB@2026-05-31$utorrent
insight       INS-compliance_drift-1LYSSFD074BB-3f9a1c
query receipt Q-<turn>-<n>
proposal      PRP-<n>
"""

from __future__ import annotations

import hashlib


def snapshot_eid(snapshot_id: str) -> str:
    return snapshot_id


def check_eid(snapshot_id: str, check_id: str) -> str:
    return f"{snapshot_id}#{check_id}"


def software_eid(snapshot_id: str, name_norm: str) -> str:
    return f"{snapshot_id}${name_norm}"


def insight_eid(detector: str, device_id: str, evidence: list[str]) -> str:
    digest = hashlib.sha256("|".join(sorted(evidence)).encode()).hexdigest()[:6]
    return f"INS-{detector}-{device_id}-{digest}"


def device_of(evidence_id: str) -> str | None:
    """Device an evidence ID refers to (None for receipts and proposals)."""
    if evidence_id.startswith("INS-"):
        parts = evidence_id.split("-")
        return "-".join(parts[2:-1]) if len(parts) >= 4 else None
    if "@" in evidence_id:
        return evidence_id.split("@", 1)[0]
    return None


def kind_of(evidence_id: str) -> str:
    if evidence_id.startswith("INS-"):
        return "insight"
    if evidence_id.startswith("Q-"):
        return "receipt"
    if evidence_id.startswith("PRP-"):
        return "proposal"
    if "#" in evidence_id:
        return "check"
    if "$" in evidence_id:
        return "software"
    return "snapshot"
