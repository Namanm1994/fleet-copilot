"""Plain-text rendering of a turn. The CLI adds styling and escapes device strings (G33)."""

from __future__ import annotations

import re
from typing import Any

from fleet_copilot.config import Thresholds

CAPABILITIES = (
    "I can answer questions about this company's devices from telemetry (OS, memory, disk, battery, software, "
    "compliance), find patterns over the telemetry window (battery wear, RAM pressure, low storage, compliance "
    "drift, prohibited software), and propose IT actions for you to approve."
)


def device_names(devices: list[dict[str, Any]], seen_in: str) -> dict[str, str]:
    """Hostname -> device ID for the devices this session's tools returned.

    `seen_in` is the text of the tool results, so a device no tool returned is never named. A hostname
    shared by two devices is left out: adding one ID to it would be a guess.
    """
    by_host: dict[str, set[str]] = {}
    for d in devices:
        host = (d.get("hostname") or "").strip()
        if host and host != d["device_id"] and d["device_id"] in seen_in:
            by_host.setdefault(host, set()).add(d["device_id"])
    return {h: next(iter(ids)) for h, ids in by_host.items() if len(ids) == 1}


def _standalone(name: str) -> re.Pattern:
    return re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])", re.IGNORECASE)


def label_devices(text: str, names: dict[str, str] | None) -> str:
    """Show every device the same way: where the text names one by hostname, add its ID once.

    "acme-macbook-2 is failing" becomes "acme-macbook-2 (1LYSSFD074BB) is failing". The model may use
    either name; the admin always gets both, and anything that looks for the ID finds it.
    """
    for host, device_id in sorted((names or {}).items(), key=lambda kv: -len(kv[0])):
        if _standalone(device_id).search(text):
            continue                                        # the ID is already there
        text = _standalone(host).sub(lambda m, i=device_id: f"{m.group()} ({i})", text, count=1)
    return text


MAX_DETAILS = 25


def claim_details(claim: dict[str, Any], shown: str = "") -> list[str]:
    """Lines a claim lists in its values and does not already say: "five devices drifted" plus the five.

    Only lists of text are shown, and only items the answer has not already printed. The verifier checks
    exactly these lines, so nothing reaches the admin unchecked."""
    out: list[str] = []
    for value in (claim.get("values") or {}).values():
        if isinstance(value, list):
            out += [v.strip() for v in value if isinstance(v, str) and v.strip() and v.strip() not in shown and v.strip() not in out]
    return out[:MAX_DETAILS]


def _ids(ids: list[str], n: int = 3) -> str:
    if not ids:
        return ""
    shown = ", ".join(ids[:n])
    return f"{shown} +{len(ids) - n} more" if len(ids) > n else shown


def render_turn(*, tenant_name: str, as_of: str, status: str, answer: dict[str, Any] | None, note: str | None,
                proposals: list[dict[str, Any]], skipped: list[dict[str, Any]], thresholds: Thresholds,
                names: dict[str, str] | None = None) -> str:
    lines = [f"As of {as_of[:10]} · {tenant_name}", ""]
    if note:
        lines += [note, ""]
    if answer:
        lines.append(label_devices(answer["summary"], names))
        if answer.get("claims"):
            lines.append("")
            shown = " ".join([answer["summary"], *(c["text"] for c in answer["claims"])])
            for c in answer["claims"]:
                ev = _ids(c.get("evidence_ids", []))
                lines.append(f"• {label_devices(c['text'], names)}" + (f"  [evidence: {ev}]" if ev else ""))
                lines += [f"    - {label_devices(item, names)}" for item in claim_details(c, shown)]
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
