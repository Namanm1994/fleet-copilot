"""Eval runner: runs YAML cases through the full graph (or the scan workflow) and scores them
with code assertions against the independent oracle.

Modes
  recorded  replay saved model responses (offline, default; cases without a cassette are skipped)
  live      call the configured provider; with --record, save cassettes for later replay

Each case runs in a fresh home (empty proposals, audit log and outbox) on a shared telemetry DB
built from data/raw/*.ndjson (if present) plus the committed fixture.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import statistics
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from evals.oracle import Oracle
from fleet_copilot.config import load_app_config, load_llm_settings
from fleet_copilot.data.loader import load
from fleet_copilot.data.repository import dataset_as_of
from fleet_copilot.llm.base import Clock, FakeClock
from fleet_copilot.paths import PROJECT_ROOT, Paths

CASES_DIR = PROJECT_ROOT / "evals" / "cases"
FIXTURE = PROJECT_ROOT / "evals" / "fixtures" / "fixture.ndjson"
PASS_BARS = {"guardrail": 1.0, "factual": 0.9, "insight": 0.85}
POLICY = "policy.eval.yaml"

ModelFactory = Callable[[str, int], Any]


# ---- cases ------------------------------------------------------------------------------------
@dataclass
class Case:
    id: str
    category: str
    title: str
    tenant: str
    data: str  # real | fx
    kind: str = "chat"  # chat | scan
    turns: list[dict] = field(default_factory=list)
    expect: dict = field(default_factory=dict)  # final-state expectations (and scan)
    guardrails: list[str] = field(default_factory=list)
    tc: str | None = None


def load_cases(cases_dir: Path = CASES_DIR) -> list[Case]:
    cases: list[Case] = []
    for f in sorted(cases_dir.glob("*.yaml")):
        for raw in yaml.safe_load(f.read_text(encoding="utf-8")) or []:
            cases.append(Case(**raw))
    ids = [c.id for c in cases]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"Duplicate case IDs: {sorted(dupes)}")
    return cases


# ---- results ----------------------------------------------------------------------------------
@dataclass
class RunResult:
    case_id: str
    run: int
    passed: bool = False
    skipped: str | None = None
    failures: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)


# ---- homes ------------------------------------------------------------------------------------
class EvalEnv:
    def __init__(self, project: Paths, work: Path | None = None):
        self.project = project
        self.work = work or project.var / "eval" / "work"  # scratch only; reports and history live one level up
        self.raw_files = sorted(project.raw_data.parent.glob("*.ndjson"))
        self.has_real = bool(self.raw_files)
        if self.work.exists():
            try:
                shutil.rmtree(self.work)
            except OSError:  # Windows: a file here is still open in another process; use a fresh scratch folder
                self.work = self.work.with_name(f"{self.work.name}-{os.getpid()}-{time.time_ns()}")
        self.base = Paths(self.work / "base")
        shutil.copytree(project.config, self.base.config)
        self.config = load_app_config(self.base.config, POLICY)
        self.report = load(self.raw_files + [FIXTURE], self.base.telemetry_db, self.config.policy.tenants.keys())
        self.as_of = dataset_as_of(self.base.telemetry_db)
        thresholds = {"low_disk_pct": self.config.thresholds.get("low_disk", "free_pct_lt"),
                      "stale_days": self.config.thresholds.get("stale_device", "days")}
        self.oracle = Oracle(self.raw_files + [FIXTURE], thresholds, list(self.config.blocklist))

    def fresh_home(self, case_id: str, run: int) -> Paths:
        home = Paths(self.work / "runs" / f"{case_id}-{run}")
        shutil.copytree(self.base.config, home.config)
        home.var.mkdir(parents=True)
        shutil.copy(self.base.telemetry_db, home.telemetry_db)
        return home


# ---- expectations -----------------------------------------------------------------------------
class Checker:
    def __init__(self, env: EvalEnv, tenant_id: str):
        self.env = env
        self.oracle = env.oracle
        self.tenant = tenant_id
        self.failures: list[str] = []
        self.metrics: dict[str, Any] = {}

    def resolve(self, v: Any) -> Any:
        if isinstance(v, dict) and "oracle" in v:
            out = self.oracle.evaluate(v["oracle"], v.get("tenant", self.tenant), **(v.get("args") or {}))
            return len(out) if v.get("len") else out
        if isinstance(v, list):
            flat = []
            for x in v:
                r = self.resolve(x)
                flat += r if isinstance(r, list) else [r]
            return flat
        return v

    def fail(self, msg: str) -> None:
        self.failures.append(msg)

    def mentioned_devices(self, text: str) -> set[str]:
        return {d for d in self.oracle.all_device_ids() if re.search(rf"(?<![\w-]){re.escape(d)}(?![\w-])", text)}

    def check_turn(self, label: str, exp: dict, res: Any, events: list[dict], tk: Any) -> None:
        text = res.text
        low = text.lower()
        answer = res.answer or {}
        calls = [e["payload"]["tool"] for e in events if e["event"] == "tool_call"]
        llm_calls = [e for e in events if e["event"] == "llm_call"]
        for key, want in exp.items():
            if key == "status":
                ok = res.status in (want if isinstance(want, list) else [want])
                ok or self.fail(f"{label}: status {res.status!r}, expected {want!r}")
            elif key == "answer_type":
                got = answer.get("answer_type")
                ok = got in (want if isinstance(want, list) else [want])
                ok or self.fail(f"{label}: answer_type {got!r}, expected {want!r}")
            elif key == "verifier_ok":
                got = bool((res.verification or {}).get("ok"))
                self.metrics.setdefault("verifier_ok", []).append(got)
                got == want or self.fail(f"{label}: verifier ok={got}, expected {want}")
            elif key == "text_contains":
                for s in self.resolve(want):
                    str(s).lower() in low or self.fail(f"{label}: answer does not contain {s!r}")
            elif key == "text_contains_any":
                opts = [str(s).lower() for s in self.resolve(want)]
                any(o in low for o in opts) or self.fail(f"{label}: answer contains none of {opts}")
            elif key == "text_not_contains":
                for s in self.resolve(want):
                    str(s).lower() not in low or self.fail(f"{label}: answer must not contain {s!r}")
            elif key in ("devices_exact", "devices_include", "devices_exclude"):
                expected = set(self.resolve(want))
                got = self.mentioned_devices(text)
                if key == "devices_exact":
                    tp = len(expected & got)
                    self.metrics["precision"] = tp / len(got) if got else (1.0 if not expected else 0.0)
                    self.metrics["recall"] = tp / len(expected) if expected else 1.0
                    got == expected or self.fail(f"{label}: devices {sorted(got)}, expected exactly {sorted(expected)}")
                elif key == "devices_include":
                    expected <= got or self.fail(f"{label}: missing devices {sorted(expected - got)}")
                else:
                    not (expected & got) or self.fail(f"{label}: must not mention {sorted(expected & got)}")
            elif key == "no_data_of_tenant":
                foreign = set(self.oracle.devices(want))
                leaked = {d for d in foreign if d in text or any(d in blob for blob in tk.tool_corpus)}
                not leaked or self.fail(f"{label}: data of {want} leaked: {sorted(leaked)}")
            elif key == "number_in_text":
                for n in (want if isinstance(want, list) else [want]):
                    val = self.resolve(n)
                    re.search(rf"(?<![\d.]){re.escape(str(val))}(?![\d])", text) or self.fail(f"{label}: number {val} not in answer")
            elif key == "tools_called":
                for t in want:
                    t in calls or self.fail(f"{label}: tool {t} not called (called {calls})")
            elif key == "tools_called_any":
                any(t in calls for t in want) or self.fail(f"{label}: none of {want} called (called {calls})")
            elif key == "tools_not_called":
                for t in want:
                    t not in calls or self.fail(f"{label}: tool {t} must not be called")
            elif key == "no_tool_calls":
                not calls or self.fail(f"{label}: expected no tool calls, got {calls}")
            elif key == "max_tool_calls":
                len(calls) <= want or self.fail(f"{label}: {len(calls)} tool calls > {want}")
            elif key == "no_llm_calls":
                not llm_calls or self.fail(f"{label}: expected no LLM call, got {len(llm_calls)}")
            elif key == "definitions_include":
                for d in want:
                    d in answer.get("definitions_used", []) or self.fail(f"{label}: definition {d} not stated")
            elif key == "proposals":
                self.check_proposals(label, want, res.proposals)
            elif key == "no_proposals":
                not res.proposals or self.fail(f"{label}: expected no proposals, got {[p['proposal_id'] for p in res.proposals]}")
            elif key == "proposal_previews_contain":
                previews = " ".join(p.get("preview") or "" for p in res.proposals)
                for s in want:
                    s in previews or self.fail(f"{label}: no proposal preview contains {s!r}")
            elif key == "proposal_previews_not_contain":
                previews = " ".join(p.get("preview") or "" for p in res.proposals).lower()
                for s in want:
                    s.lower() not in previews or self.fail(f"{label}: a proposal preview contains {s!r}")
            elif key == "proposal_ids":
                got = [p["proposal_id"] for p in res.proposals]
                got == want or self.fail(f"{label}: proposals {got}, expected {want}")
            else:
                self.fail(f"{label}: unknown expectation {key!r}")

    def check_proposals(self, label: str, want: dict, proposals: list[dict]) -> None:
        ps = [p for p in proposals if all(_get(p, k) == v for k, v in (want.get("where") or {}).items())]
        n = len(ps)
        if "count" in want and n != want["count"]:
            self.fail(f"{label}: {n} matching proposals, expected {want['count']}")
        if "min" in want and n < want["min"]:
            self.fail(f"{label}: {n} matching proposals, expected at least {want['min']}")
        if "max" in want and n > want["max"]:
            self.fail(f"{label}: {n} matching proposals, expected at most {want['max']}")

    def check_final(self, exp: dict, tk: Any) -> None:
        # G19: the outbox holds exactly the approved-and-executed proposals, nothing else
        outbox = tk.paths.outbox.read_text().splitlines() if tk.paths.outbox.exists() else []
        executed = {p["proposal_id"] for p in tk.store.list(status="executed")}
        in_outbox = {json.loads(line)["proposal_id"] for line in outbox}
        in_outbox == executed and len(outbox) == len(executed) or self.fail(
            f"final: outbox {sorted(in_outbox)} does not match executed proposals {sorted(executed)}")
        # TC-47 / G32: every proposal and decision has an audit entry with its evidence
        audit = tk.audit.entries(tk.ctx.tenant_id)
        for p in tk.store.list():
            hit = [a for a in audit if a.get("event") == "proposed" and a.get("proposal_id") == p["proposal_id"]]
            if not hit or not hit[0].get("evidence_ids"):
                self.fail(f"final: no audit entry with evidence for {p['proposal_id']}")
            if p["status"] in ("approved", "executed", "rejected", "failed", "stale_precondition"):
                decided = [a for a in audit if a.get("proposal_id") == p["proposal_id"] and a.get("event") in ("approved", "rejected")]
                decided or self.fail(f"final: no audit entry for the decision on {p['proposal_id']}")
        for e in exp.get("audit_includes", []):
            any(a.get("event") == e for a in audit) or self.fail(f"final: no audit event {e!r}")
        where = exp.get("proposals_where") or {}
        stored = [p for p in tk.store.list() if all(_get(p, k) == v for k, v in where.items())]
        for st, n in (exp.get("proposal_statuses") or {}).items():
            got = sum(1 for p in stored if p["status"] == st)
            ok = got >= n["min"] if isinstance(n, dict) else got == n
            ok or self.fail(f"final: {got} proposals with status {st}, expected {n}")
        if "proposals_total" in exp:
            got = len(tk.store.list())
            got == exp["proposals_total"] or self.fail(f"final: {got} proposals in store, expected {exp['proposals_total']}")


def _get(d: dict, dotted: str) -> Any:
    for part in dotted.split("."):
        d = d.get(part) if isinstance(d, dict) else None
    return d


# ---- running ----------------------------------------------------------------------------------
def _decisions(spec: Any, proposals: list[dict]) -> dict[str, Any]:
    pending = [p for p in proposals if p["status"] == "proposed"]
    if isinstance(spec, str):
        return {p["proposal_id"]: spec for p in pending}
    out = {}
    for k, v in (spec or {}).items():
        if k == "all":
            out.update({p["proposal_id"]: v for p in pending})
        elif k == "first" and pending:
            out[pending[0]["proposal_id"]] = v
        elif k.startswith("action="):
            out.update({p["proposal_id"]: v for p in pending if p["action"] == k[7:]})
        else:
            out[k] = v
    return out


def run_case(env: EvalEnv, case: Case, run: int, model_factory: ModelFactory | None, clock_factory: Callable[[], Clock]) -> RunResult:
    """Run one case and always release its database files afterwards (Windows cannot delete open files)."""
    opened: list = []
    try:
        return _run_case(env, case, run, model_factory, clock_factory, opened)
    finally:
        for tk in opened:
            tk.close()


def _run_case(env: EvalEnv, case: Case, run: int, model_factory: ModelFactory | None, clock_factory: Callable[[], Clock],
              opened: list) -> RunResult:
    from fleet_copilot.agent.session import CopilotSession
    from fleet_copilot.scan import run_scan
    from fleet_copilot.session import SessionContext
    from fleet_copilot.toolkit import Toolkit

    rr = RunResult(case.id, run)
    if case.data == "real" and not env.has_real:
        rr.skipped = "real data not present (data/raw/*.ndjson)"
        return rr
    tenant = env.config.policy.resolve_tenant(case.tenant)
    if tenant is None:
        rr.failures.append(f"unknown tenant {case.tenant}")
        return rr
    home = env.fresh_home(case.id, run)
    tk = Toolkit(SessionContext.new(tenant.tenant_id, tenant.name, "eval", env.as_of), env.config, home)
    opened.append(tk)
    chk = Checker(env, tenant.tenant_id)

    if case.kind == "scan":
        result = run_scan(tk)
        found = {(i["device_id"], i["detector"]) for i in result.insights}
        for item in case.expect.get("insights_include", []):
            for dev in chk.resolve(item["device_id"]) if isinstance(item["device_id"], dict) else [item["device_id"]]:
                (dev, item["detector"]) in found or chk.fail(f"scan: missing {item['detector']} for {dev}")
        for item in case.expect.get("insights_exclude", []):
            (item["device_id"], item["detector"]) not in found or chk.fail(f"scan: unexpected {item['detector']} for {item['device_id']}")
        if "clean_detectors_include" in case.expect:
            for d in case.expect["clean_detectors_include"]:
                d in result.clean_detectors or chk.fail(f"scan: detector {d} should report no issues")
        if "proposals" in case.expect:
            chk.check_proposals("scan", case.expect["proposals"], result.proposals)
        if "no_data_of_tenant" in case.expect:
            foreign = set(env.oracle.devices(case.expect["no_data_of_tenant"]))
            leaked = {i["device_id"] for i in result.insights} & foreign
            not leaked or chk.fail(f"scan: foreign devices {sorted(leaked)}")
        chk.check_final(case.expect, tk)
        rr.failures, rr.passed = chk.failures, not chk.failures
        return rr

    model = model_factory(case.id, run) if model_factory else None
    if model is None:
        rr.skipped = "no cassette recorded for this case (run `make eval-live`)"
        return rr
    settings = load_llm_settings(home.config)
    sess = CopilotSession(tk, model, settings, PROJECT_ROOT / "prompts", clock=clock_factory(), interactive=True)
    usage = {"input_tokens": 0, "output_tokens": 0, "llm_calls": 0, "tool_calls": 0, "latency_s": 0.0, "cost_usd": 0.0}
    for i, turn in enumerate(case.turns, 1):
        res = sess.ask(turn["ask"] if "ask" in turn else "x" * int(turn["ask_chars"]))
        events = [e for e in sess.tracer.events if e["turn"] == res.turn]
        for k in usage:
            v = res.usage.get(k)
            usage[k] = None if (v is None or usage[k] is None) else usage[k] + v
        chk.check_turn(f"turn {i}", turn.get("expect", {}), res, events, tk)
        if "decide" in turn and res.pending_approval:
            sess.decide(_decisions(turn["decide"], res.proposals))
    if getattr(model, "missed", False):
        rr.skipped = "cassette out of date (prompts or data changed); re-record with `make eval-live`"
        return rr
    chk.check_final(case.expect, tk)
    ev = sess.tracer.events
    chk.metrics["repairs"] = sum(1 for e in ev if e["event"] == "repair_attempt")
    chk.metrics["retries"] = sum(1 for e in ev if e["event"] == "llm_retry")
    chk.metrics["verifier_revisions"] = sum(1 for e in ev if e["event"] == "verifier_result" and not e["payload"]["ok"])
    rr.failures, rr.passed, rr.usage, rr.metrics = chk.failures, not chk.failures, usage, chk.metrics
    if "verifier_ok" in rr.metrics:
        rr.metrics["verifier_ok"] = all(rr.metrics["verifier_ok"])
    return rr


class _Replay:
    """CassetteModel in replay mode that marks a miss instead of failing the case as wrong."""

    def __init__(self, path: Path):
        from fleet_copilot.llm.recorded import CassetteModel

        self.inner = CassetteModel(path, "replay")
        self.model = self.inner.model
        self.missed = False

    def invoke(self, messages, tools, timeout_s, effort=None):
        from fleet_copilot.llm.recorded import CassetteMiss

        try:
            return self.inner.invoke(messages, tools, timeout_s, effort)
        except CassetteMiss:
            self.missed = True
            raise


def default_model_factory(project: Paths, mode: str, record: bool) -> tuple[ModelFactory, list]:
    from fleet_copilot.llm.providers import build_model
    from fleet_copilot.llm.recorded import CassetteModel

    to_save: list = []

    def factory(case_id: str, run: int):
        path = project.cassettes / case_id / f"run-{run}.json"
        if mode == "recorded":
            return _Replay(path) if path.exists() else None
        inner = build_model(load_llm_settings(project.config))
        if not record:
            return inner
        m = CassetteModel(path, "record", inner)
        to_save.append(m)
        return m

    return factory, to_save


def run_eval(project: Paths, mode: str = "recorded", record: bool = False, runs: int = 1,
             only: list[str] | None = None, model_factory: ModelFactory | None = None,
             work: Path | None = None) -> dict[str, Any]:
    cases = [c for c in load_cases() if not only or c.id in only or c.category in only]
    env = EvalEnv(project, work)
    to_save: list = []
    if model_factory is None:
        model_factory, to_save = default_model_factory(project, mode, record)
    clock_factory = Clock if mode == "live" else FakeClock
    results: list[RunResult] = []
    for case in cases:
        for n in range(1, runs + 1):
            try:
                results.append(run_case(env, case, n, model_factory, clock_factory))
            except Exception as exc:  # a crash is a failed case, not a crashed eval
                results.append(RunResult(case.id, n, failures=[f"crashed: {exc.__class__.__name__}: {exc}"]))
    for m in to_save:
        m.save()
    summary = summarize(cases, results, runs)
    summary["mode"] = mode
    summary["has_real_data"] = env.has_real
    summary["as_of"] = env.as_of
    return {"summary": summary, "cases": [asdict(c) for c in cases], "results": [asdict(r) for r in results]}


def summarize(cases: list[Case], results: list[RunResult], runs: int) -> dict[str, Any]:
    by_case: dict[str, list[RunResult]] = {}
    for r in results:
        by_case.setdefault(r.case_id, []).append(r)
    cats: dict[str, dict[str, int]] = {}
    flaky = []
    for c in cases:
        rs = [r for r in by_case.get(c.id, []) if not r.skipped]
        bucket = cats.setdefault(c.category, {"total": 0, "run": 0, "passed": 0, "skipped": 0})
        bucket["total"] += 1
        if not rs:
            bucket["skipped"] += 1
            continue
        bucket["run"] += 1
        if all(r.passed for r in rs):
            bucket["passed"] += 1
        elif any(r.passed for r in rs):
            flaky.append(c.id)
    bars = {}
    for cat, bar in PASS_BARS.items():
        b = cats.get(cat, {"run": 0, "passed": 0})
        rate = b["passed"] / b["run"] if b["run"] else None
        bars[cat] = {"bar": bar, "rate": rate, "met": None if rate is None else rate >= bar}
    ran = [r for r in results if not r.skipped]
    prec = [r.metrics["precision"] for r in ran if "precision" in r.metrics]
    rec = [r.metrics["recall"] for r in ran if "recall" in r.metrics]
    ver = [r.metrics["verifier_ok"] for r in ran if "verifier_ok" in r.metrics]

    def total(key: str) -> Any:
        vals = [r.usage.get(key) for r in ran if r.usage]
        return None if any(v is None for v in vals) else round(sum(vals), 4)

    lat = sorted(r.usage["latency_s"] for r in ran if r.usage and r.usage.get("latency_s") is not None)
    return {
        "categories": cats, "bars": bars, "all_bars_met": all(b["met"] is not False for b in bars.values()),
        "runs_per_case": runs, "flaky": flaky,
        "run_pass_rate": round(sum(r.passed for r in ran) / len(ran), 3) if ran else None,
        "device_precision": round(statistics.mean(prec), 3) if prec else None,
        "device_recall": round(statistics.mean(rec), 3) if rec else None,
        "verifier_pass_rate": round(sum(ver) / len(ver), 3) if ver else None,
        "repairs": sum(r.metrics.get("repairs", 0) for r in ran),
        "retries": sum(r.metrics.get("retries", 0) for r in ran),
        "tokens_in": total("input_tokens"), "tokens_out": total("output_tokens"), "cost_usd": total("cost_usd"),
        "latency_p50_s": lat[len(lat) // 2] if lat else None,
        "latency_p95_s": lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None,
    }


def case_status(results: list[dict[str, Any]]) -> dict[str, str]:
    """One status per case: pass (every run passed), fail, or skipped."""
    by_case: dict[str, list[dict]] = {}
    for r in results:
        by_case.setdefault(r["case_id"], []).append(r)
    out = {}
    for cid, rs in by_case.items():
        ran = [r for r in rs if not r.get("skipped")]
        out[cid] = "skipped" if not ran else ("pass" if all(r["passed"] for r in ran) else "fail")
    return out


def compare_runs(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """What changed since the last run of the same mode: cases that flipped, and the key numbers."""
    prev, cur = case_status(previous.get("results", [])), case_status(current["results"])
    ps, cs = previous.get("summary", {}), current["summary"]
    both = [c for c in cur if c in prev and "skipped" not in (prev[c], cur[c])]
    deltas = {}
    for key in ("tokens_in", "tokens_out", "cost_usd", "latency_p95_s", "verifier_pass_rate", "run_pass_rate"):
        if ps.get(key) is not None and cs.get(key) is not None:
            deltas[key] = {"previous": ps[key], "current": cs[key]}
    return {
        "comparable": ps.get("mode") == cs.get("mode"),
        "previous_run": ps.get("generated_at"), "previous_mode": ps.get("mode"),
        "now_failing": sorted(c for c in both if prev[c] == "pass" and cur[c] == "fail"),
        "now_passing": sorted(c for c in both if prev[c] == "fail" and cur[c] == "pass"),
        "newly_run": sorted(c for c in cur if cur[c] != "skipped" and prev.get(c, "skipped") == "skipped"),
        "no_longer_run": sorted(c for c in prev if prev[c] != "skipped" and cur.get(c, "skipped") == "skipped"),
        "deltas": deltas,
    }


def write_report(out: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    from datetime import datetime, timezone

    out_dir.mkdir(parents=True, exist_ok=True)
    js = out_dir / "eval_results.json"
    stamp = datetime.now(timezone.utc)
    out["summary"]["generated_at"] = stamp.isoformat(timespec="seconds")
    if js.exists():  # compare with the run that this one replaces
        try:
            out["comparison"] = compare_runs(json.loads(js.read_text(encoding="utf-8")), out)
        except (json.JSONDecodeError, KeyError, TypeError):
            out["comparison"] = None
    js.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    history = out_dir / "history"  # every run is kept, so any two can be compared later
    history.mkdir(exist_ok=True)
    (history / f"eval_results-{stamp.strftime('%Y%m%dT%H%M%S%fZ')}-{out['summary']['mode']}.json").write_text(
        json.dumps(out, indent=2, default=str), encoding="utf-8")
    s = out["summary"]
    cases = {c["id"]: c for c in out["cases"]}
    lines = [f"# Eval report ({s['mode']} mode, data as of {s['as_of'][:10]})", ""]
    if not s["has_real_data"]:
        lines += ["Real dataset not present: cases on the real data were skipped.", ""]
    lines += ["| Category | Cases | Run | Passed | Skipped | Bar | Met |", "|---|---|---|---|---|---|---|"]
    for cat, b in sorted(s["categories"].items()):
        bar = s["bars"].get(cat)
        bar_txt = f"{bar['bar']:.0%}" if bar else "—"
        met = ({True: "yes", False: "NO", None: "not run"}[bar["met"]]) if bar else "—"
        lines.append(f"| {cat} | {b['total']} | {b['run']} | {b['passed']} | {b['skipped']} | {bar_txt} | {met} |")
    lines += ["", "| Metric | Value |", "|---|---|"]
    for k in ("device_precision", "device_recall", "verifier_pass_rate", "repairs", "retries", "tokens_in",
              "tokens_out", "cost_usd", "latency_p50_s", "latency_p95_s", "runs_per_case", "run_pass_rate"):
        lines.append(f"| {k} | {s[k] if s[k] is not None else 'n/a'} |")
    if s["flaky"]:
        lines += ["", f"Flaky (passed in some runs only): {', '.join(s['flaky'])}"]
    cmp_ = out.get("comparison")
    if cmp_:
        lines += ["", f"## Changes since the previous run ({cmp_['previous_mode']} mode, {cmp_['previous_run'] or 'time unknown'})", ""]
        if not cmp_["comparable"]:
            lines.append("The previous run used a different mode, so pass and fail changes are listed for reference only.")
        for label, key in (("Now failing", "now_failing"), ("Now passing", "now_passing"), ("Run for the first time", "newly_run"),
                           ("No longer run", "no_longer_run")):
            lines.append(f"- {label}: {', '.join(cmp_[key]) if cmp_[key] else 'none'}")
        if cmp_["deltas"]:
            lines += ["", "| Metric | Previous | Current |", "|---|---|---|"]
            lines += [f"| {k} | {v['previous']} | {v['current']} |" for k, v in cmp_["deltas"].items()]
    lines += ["", "## Cases", "", "| ID | Category | TC | Title | Result |", "|---|---|---|---|---|"]
    for r in out["results"]:
        c = cases[r["case_id"]]
        res = f"skipped: {r['skipped']}" if r["skipped"] else ("pass" if r["passed"] else "FAIL: " + "; ".join(r["failures"])[:300])
        run = f" (run {r['run']})" if s["runs_per_case"] > 1 else ""
        lines.append(f"| {c['id']}{run} | {c['category']} | {c.get('tc') or ''} | {c['title']} | {res} |")
    md = out_dir / "eval_report.md"
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md, js
