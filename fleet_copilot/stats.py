"""A view across sessions: cost, latency and answer quality aggregated from the session traces.

Reads var/traces/YYYY-MM-DD/<session>.jsonl for one tenant. Nothing is sent anywhere.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _pct(sorted_values: list[float], q: float) -> float | None:
    if not sorted_values:
        return None
    return sorted_values[min(len(sorted_values) - 1, int(len(sorted_values) * q))]


def collect(traces_dir: Path, tenant_id: str, days: int = 7, today: str | None = None) -> dict[str, Any]:
    end = datetime.strptime(today, "%Y-%m-%d").date() if today else datetime.now(timezone.utc).date()
    start = end - timedelta(days=max(days, 1) - 1)
    sessions = turns = llm_calls = tool_calls = tokens_in = tokens_out = 0
    cost: float | None = 0.0
    latencies: list[float] = []
    statuses: Counter = Counter()
    tool_errors: Counter = Counter()
    counts: Counter = Counter()
    per_day: dict[str, dict[str, Any]] = {}
    for day_dir in sorted(p for p in traces_dir.glob("*") if p.is_dir()):
        try:
            day = datetime.strptime(day_dir.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if not start <= day <= end:
            continue
        for path in sorted(day_dir.glob("*.jsonl")):
            events = []
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    counts["unreadable_trace_lines"] += 1
            if not events or events[0].get("tenant") != tenant_id:
                continue  # other tenants' sessions are never counted or shown
            sessions += 1
            d = per_day.setdefault(day_dir.name, {"sessions": 0, "turns": 0, "tokens_in": 0, "tokens_out": 0})
            d["sessions"] += 1
            for e in events:
                kind, payload = e.get("event"), e.get("payload") or {}
                if kind == "usage":
                    turns += 1
                    d["turns"] += 1
                    llm_calls += payload.get("llm_calls", 0)
                    tool_calls += payload.get("tool_calls", 0)
                    tokens_in += payload.get("input_tokens", 0)
                    tokens_out += payload.get("output_tokens", 0)
                    d["tokens_in"] += payload.get("input_tokens", 0)
                    d["tokens_out"] += payload.get("output_tokens", 0)
                    if payload.get("latency_s") is not None:
                        latencies.append(float(payload["latency_s"]))
                    if payload.get("cost_usd") is None:
                        cost = None if payload.get("llm_calls") else cost
                    elif cost is not None:
                        cost += float(payload["cost_usd"])
                elif kind == "final_answer":
                    statuses[payload.get("status") or "unknown"] += 1
                    if (payload.get("verification") or {}).get("fallback"):
                        counts["answers_with_claims_removed"] += 1
                elif kind == "verifier_result" and e.get("node") == "verifier":
                    counts["verifier_checks"] += 1
                    if not payload.get("ok"):
                        counts["verifier_rejections"] += 1
                elif kind == "tool_result" and payload.get("error"):
                    tool_errors[payload["error"].get("code", "UNKNOWN")] += 1
                elif kind in ("repair_attempt", "llm_retry", "deadline_reached", "budget_stop", "context_compacted",
                              "tool_output_shortened", "cancelled", "error", "proposal"):
                    counts[kind] += 1
    latencies.sort()
    checks = counts.get("verifier_checks", 0)
    return {
        "tenant": tenant_id, "from": start.isoformat(), "to": end.isoformat(), "sessions": sessions, "turns": turns,
        "llm_calls": llm_calls, "tool_calls": tool_calls, "input_tokens": tokens_in, "output_tokens": tokens_out,
        "cost_usd": None if cost is None else round(cost, 4),
        "latency_p50_s": _pct(latencies, 0.5), "latency_p95_s": _pct(latencies, 0.95),
        "turn_outcomes": dict(statuses),
        "verifier_checks": checks, "verifier_rejections": counts.get("verifier_rejections", 0),
        "verifier_rejection_rate": round(counts.get("verifier_rejections", 0) / checks, 3) if checks else None,
        "answers_with_claims_removed": counts.get("answers_with_claims_removed", 0),
        "repairs": counts.get("repair_attempt", 0), "llm_retries": counts.get("llm_retry", 0),
        "deadline_stops": counts.get("deadline_reached", 0), "budget_stops": counts.get("budget_stop", 0),
        "context_condensed": counts.get("context_compacted", 0), "tool_outputs_shortened": counts.get("tool_output_shortened", 0),
        "cancelled_turns": counts.get("cancelled", 0), "failed_turns": counts.get("error", 0),
        "proposals_created": counts.get("proposal", 0), "tool_errors": dict(tool_errors), "per_day": per_day,
    }


def render(s: dict[str, Any]) -> str:
    def val(x: Any, unit: str = "") -> str:
        return "n/a" if x is None else f"{x}{unit}"

    cost = "n/a (model not in pricing.yaml)" if s["cost_usd"] is None else f"${s['cost_usd']:.4f}"
    rate = "n/a" if s["verifier_rejection_rate"] is None else f"{s['verifier_rejection_rate']:.0%}"
    lines = [
        f"Usage and quality · {s['tenant']} · {s['from']} to {s['to']}", "",
        f"Sessions {s['sessions']} · turns {s['turns']} · LLM calls {s['llm_calls']} · tool calls {s['tool_calls']}",
        f"Tokens in/out {s['input_tokens']:,} / {s['output_tokens']:,} · cost {cost}",
        f"Turn latency p50 {val(s['latency_p50_s'], ' s')} · p95 {val(s['latency_p95_s'], ' s')}", "",
        f"Verifier: {s['verifier_rejections']} of {s['verifier_checks']} answers rejected at first ({rate}); "
        f"{s['answers_with_claims_removed']} shown with unverified claims removed",
        f"Repairs {s['repairs']} · LLM retries {s['llm_retries']} · deadline stops {s['deadline_stops']} · "
        f"budget stops {s['budget_stops']} · failed turns {s['failed_turns']} · cancelled {s['cancelled_turns']}",
        f"Context condensed {s['context_condensed']} · tool outputs shortened {s['tool_outputs_shortened']} · "
        f"proposals created {s['proposals_created']}",
    ]
    if s["turn_outcomes"]:
        lines.append("Turn outcomes: " + ", ".join(f"{k} {v}" for k, v in sorted(s["turn_outcomes"].items())))
    if s["tool_errors"]:
        lines.append("Tool errors: " + ", ".join(f"{k} {v}" for k, v in sorted(s["tool_errors"].items())))
    if s["per_day"]:
        lines += ["", "Per day:"] + [f"  {day}: {d['sessions']} sessions, {d['turns']} turns, "
                                     f"{d['tokens_in']:,} / {d['tokens_out']:,} tokens" for day, d in sorted(s["per_day"].items())]
    if not s["sessions"]:
        lines.append("No sessions in this period.")
    return "\n".join(lines) + "\n"
