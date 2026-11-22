"""Plain-text rendering of a turn. The CLI adds styling and escapes device strings (G33)."""

from __future__ import annotations

from typing import Any

from fleet_copilot.config import Thresholds

CAPABILITIES = (
    "I can answer questions about this company's devices from telemetry (OS, memory, disk, battery, software, "
    "compliance), find patterns over the telemetry window (battery wear, RAM pressure, low storage, compliance "
    "drift, prohibited software), and propose IT actions for you to approve."
)


def _ids(ids: list[str], n: int = 3) -> str:
    if not ids:
        return ""
    shown = ", ".join(ids[:n])
    return f"{shown} +{len(ids) - n} more" if len(ids) > n else shown


def render_turn(*, tenant_name: str, as_of: str, status: str, answer: dict[str, Any] | None, note: str | None,
                proposals: list[dict[str, Any]], skipped: list[dict[str, Any]], thresholds: Thresholds) -> str:
    lines = [f"As of {as_of[:10]} · {tenant_name}", ""]
    if note:
        lines += [note, ""]
    if answer:
        lines.append(answer["summary"])
        if answer.get("claims"):
            lines.append("")
            for c in answer["claims"]:
                ev = _ids(c.get("evidence_ids", []))
                lines.append(f"• {c['text']}" + (f"  [evidence: {ev}]" if ev else ""))
        defs = [d for d in answer.get("definitions_used", []) if d in thresholds.names()]
        if defs:
            lines += ["", "Definitions:"] + [f"  {d}: {thresholds.describe(d)}" for d in defs]
        if answer.get("caveats"):
            lines += ["", "Caveats:"] + [f"  - {c}" for c in answer["caveats"]]
    if proposals:
        lines += ["", "Proposed actions (awaiting your approval; nothing has been executed):"]
        for p in proposals:
            origin = "" if p.get("origin") == "requested" else " (suggested)"
            lines.append(f"  {p['proposal_id']}{origin}: {p.get('preview', p.get('action'))}")
            for w in p.get("warnings") or []:
                lines.append(f"      warning: {w}")
    capped = [s for s in skipped if s["code"] == "AUTO_PROPOSAL_CAP"]
    dupes = [s for s in skipped if s["code"] == "DUPLICATE"]
    if capped:
        lines.append(f"  {len(capped)} more suggested action(s) available; ask to see them.")
    if dupes:
        lines.append(f"  {len(dupes)} suggested action(s) already have open proposals (see `fleet proposals list`).")
    return "\n".join(lines).rstrip() + "\n"
