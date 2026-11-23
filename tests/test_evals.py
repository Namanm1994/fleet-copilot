"""Eval runner and oracle (no LLM: scan cases plus scripted chat cases)."""

from __future__ import annotations

import json
import shutil

import pytest

from evals.oracle import Oracle
from evals.runner import FIXTURE, EvalEnv, load_cases, run_eval, write_report
from fleet_copilot.llm.scripted import ScriptedModel, call, say
from fleet_copilot.paths import PROJECT_ROOT, Paths
from tests.test_agent import plan, submit


@pytest.fixture()
def project(tmp_path):
    shutil.copytree(PROJECT_ROOT / "config", tmp_path / "config")
    (tmp_path / "data" / "raw").mkdir(parents=True)  # no real data: real-data cases skip
    return Paths(tmp_path)


def test_case_inventory():
    cases = load_cases()
    assert len(cases) == 55
    cats = {c: sum(1 for x in cases if x.category == c) for c in {x.category for x in cases}}
    assert cats == {"guardrail": 24, "factual": 15, "insight": 10, "action": 3, "scan": 3}
    assert sum(1 for c in cases if len(c.turns) > 1) >= 6  # follow-up coverage
    tcs = {c.tc for c in cases if c.tc}
    for tc in ("TC-01", "TC-10", "TC-14", "TC-17", "TC-24", "TC-27", "TC-31", "TC-32", "TC-35", "TC-49"):
        assert tc in tcs


def test_oracle_is_independent_and_matches_fixture():
    src = (PROJECT_ROOT / "evals" / "oracle.py").read_text()
    assert "fleet_copilot" not in src.split('"""', 2)[2]  # no imports from the agent
    o = Oracle([FIXTURE])
    assert o.low_disk("fixture-999") == ["FX-DISK-01"]
    assert o.battery_not_normal("fixture-999") == ["FX-BATT-01"]
    assert o.first_failure_after_pass("FX-DRIFT-01", "screen_lock") == "2026-06-07"
    assert o.first_failure_after_pass("FX-BLIP-01", "screen_lock") is None
    assert o.software_first_seen("FX-UTOR-01", "utorrent") == "2026-06-09"
    assert o.stale("fixture-999") == ["FX-STALE-01"]
    assert "FX-OTHER-01" not in o.devices("fixture-999")


def test_scan_cases_pass_and_real_cases_skip(project):
    out = run_eval(project, only=["scan"])
    res = {r["case_id"]: r for r in out["results"]}
    assert res["S03"]["passed"], res["S03"]["failures"]
    assert res["S01"]["skipped"] and res["S02"]["skipped"]
    md, js = write_report(out, project.var / "eval")
    assert "| scan | 3 | 1 | 1 | 2 |" in md.read_text()
    assert json.loads(js.read_text())["summary"]["has_real_data"] is False


def _factory(steps_by_case):
    return lambda case_id, run: ScriptedModel(list(steps_by_case[case_id])) if case_id in steps_by_case else None


F08_GOOD = [plan(), call("find_devices", {"criteria": {"low_disk": True}}), submit({
    "summary": "1 device is low on disk space.",
    "claims": [{"text": "FX-DISK-01 has 7.6% free.", "evidence_ids": ["FX-DISK-01@2026-06-12", "Q-1-1"],
                "values": {"disk_free_pct": 7.6, "count": 1}}],
    "definitions_used": ["low_disk"], "caveats": [], "answer_type": "answer"})]


def test_scripted_chat_case_scored(project):
    out = run_eval(project, only=["F08", "F12"], model_factory=_factory({"F08": F08_GOOD}))
    res = {r["case_id"]: r for r in out["results"]}
    assert res["F08"]["passed"], res["F08"]["failures"]
    assert res["F08"]["metrics"]["precision"] == 1.0 and res["F08"]["metrics"]["recall"] == 1.0
    assert res["F12"]["skipped"]


def test_wrong_answer_fails_the_case(project):
    bad = [plan(), call("find_devices", {"criteria": {"low_disk": True}}), submit({
        "summary": "No devices are low on disk.", "claims": [{"text": "None matched.", "evidence_ids": ["Q-1-1"], "values": {}}],
        "definitions_used": [], "caveats": [], "answer_type": "answer"})]
    out = run_eval(project, only=["F08"], model_factory=_factory({"F08": bad}))
    r = out["results"][0]
    assert not r["passed"]
    assert any("devices" in f for f in r["failures"]) and any("definition low_disk" in f for f in r["failures"])


def test_refusal_case_with_audit(project):
    steps = {"G-TC21": [plan(), call("get_device_history", {"device_id": "FX-SHORT-01", "metrics": ["disk_free_pct"]}),
                        submit({"summary": "Insufficient data: FX-SHORT-01 has only 3 snapshots.", "claims": [
                            {"text": "Only 3 snapshots exist, fewer than the 7 needed.", "evidence_ids": [], "values": {}}],
                            "definitions_used": ["min_points_for_trend"], "caveats": [], "answer_type": "insufficient_data"})]}
    out = run_eval(project, only=["G-TC21"], model_factory=_factory(steps))
    r = out["results"][0]
    assert r["passed"], r["failures"]


def test_crash_is_a_failure_not_an_abort(project):
    out = run_eval(project, only=["F08", "S03"], model_factory=lambda c, n: ScriptedModel([say("not json")] * 1))
    res = {r["case_id"]: r for r in out["results"]}
    assert not res["F08"]["passed"] and res["S03"]["passed"]


def test_eval_env_has_fixture_tenants(project):
    env = EvalEnv(project)
    assert {"fixture-999", "fixture-998"} <= set(env.config.policy.tenants)
    assert env.as_of.startswith("2026-06-12")
