"""Eval suite additions EA-1 to EA-5 (test-plan.md): ranges on pass rates, tool-argument checks, cases the
agent must not refuse, cases where a tool fails, and control agents that prove the scoring discriminates."""

from __future__ import annotations

import json
import shutil

import pytest

from evals.controls import ALLOWED, CONTROLS, ControlModel, run_control
from evals.runner import (
    Case,
    RunResult,
    apply_fault,
    args_contain,
    load_cases,
    run_eval,
    summarize,
    wilson,
    write_report,
)
from fleet_copilot.llm.scripted import ScriptedModel, call
from fleet_copilot.paths import PROJECT_ROOT, Paths
from tests.test_agent import plan, submit
from tests.test_tools import make_tk


@pytest.fixture()
def project(tmp_path):
    shutil.copytree(PROJECT_ROOT / "config", tmp_path / "config")
    (tmp_path / "data" / "raw").mkdir(parents=True)  # no real data: real-data cases skip
    return Paths(tmp_path)


# ---- EA-1: ranges and repeated runs ------------------------------------------------------------------------
def test_wilson_range():
    lo, hi = wilson(14, 15)
    assert 0.69 < lo < 0.71 and 0.98 < hi < 1.0
    lo, hi = wilson(24, 24)
    assert 0.85 < lo < 0.87 and hi == 1.0
    assert wilson(0, 0) is None
    assert wilson(0, 10)[0] == 0.0


def test_a_case_that_passes_only_sometimes_is_flaky_and_counted():
    cases = [Case("X1", "factual", "t", "acme", "fx"), Case("X2", "factual", "t", "acme", "fx")]
    results = [RunResult("X1", 1, passed=True), RunResult("X1", 2, passed=False), RunResult("X1", 3, passed=True),
               RunResult("X2", 1, passed=True), RunResult("X2", 2, passed=True), RunResult("X2", 3, passed=True)]
    s = summarize(cases, results, runs=3)
    f = s["categories"]["factual"]
    assert f["passed"] == 1 and f["passed_any"] == 2 and s["flaky"] == ["X1"]
    assert f["run_pass_rate"] == round(5 / 6, 3) and f["interval_95"] == wilson(1, 2)


def test_report_shows_range_fallbacks_and_provisional_bars(project):
    good = [plan(), call("find_devices", {"criteria": {"low_disk": True}}), submit({
        "summary": "1 device is low on disk space.",
        "claims": [{"text": "FX-DISK-01 has 7.6% free.", "evidence_ids": ["FX-DISK-01@2026-06-12", "Q-1-1"], "values": {"count": 1}}],
        "definitions_used": ["low_disk"], "caveats": [], "answer_type": "answer"})]
    out = run_eval(project, only=["F08"], model_factory=lambda c, n: ScriptedModel(list(good)))
    assert out["results"][0]["metrics"]["rejected_answer"] is False and out["results"][0]["metrics"]["fallback_answer"] is False
    md, _ = write_report(out, project.var / "eval")
    text = md.read_text()
    assert "95% range" in text and "(n=1)" in text and "provisional" in text
    assert "| rejected_answers | 0 |" in text and "| fallback_answers | 0 |" in text


# ---- EA-2: tool arguments ----------------------------------------------------------------------------------
def test_args_contain():
    sent = {"criteria": {"failing_check": "screen_lock", "is_laptop": True}, "limit": 25}
    assert args_contain(sent, {"criteria": {"failing_check": "screen_lock"}})          # nested, extra keys ignored
    assert not args_contain(sent, {"criteria": {"failing_check": "disk_encryption"}})
    assert args_contain({"refs": ["emp-1", "acme-macbook-2"]}, {"refs": ["acme-macbook-2"]})   # list containment, any order
    assert not args_contain({"refs": ["emp-1"]}, {"refs": ["acme-macbook-2"]})
    assert args_contain({"name": "uTorrent"}, {"name": "utorrent"})                    # text without regard to case
    assert args_contain({"spec_gb": 32}, {"spec_gb": 32}) and not args_contain({"spec_gb": 64}, {"spec_gb": 32})
    assert not args_contain({}, {"device_id": "X"})


def test_tool_called_with_passes_and_fails(project):
    def run(args):
        steps = [plan(), call("find_devices", args), submit({
            "summary": "1 device is low on disk space.",
            "claims": [{"text": "FX-DISK-01 has 7.6% free.", "evidence_ids": ["FX-DISK-01@2026-06-12", "Q-1-1"], "values": {"count": 1}}],
            "definitions_used": ["low_disk"], "caveats": [], "answer_type": "answer"})]
        case = next(c for c in load_cases() if c.id == "F08")
        case.turns[0]["expect"] = {"tool_called_with": [{"any_of": [
            {"tool": "find_devices", "args": {"criteria": {"low_disk": True}}},
            {"tool": "run_insights", "args": {"detectors": ["low_storage"]}}]}]}
        import evals.runner as r
        original = r.load_cases
        r.load_cases = lambda *a, **k: [case]
        try:
            return run_eval(project, model_factory=lambda c, n: ScriptedModel(list(steps)))["results"][0]
        finally:
            r.load_cases = original

    ok = run({"criteria": {"low_disk": True}})
    assert ok["passed"] and ok["metrics"]["tool_args_checked"] == 1 and ok["metrics"]["tool_args_ok"] == 1
    bad = run({"criteria": {"disk_free_pct_lt": 15}})
    assert not bad["passed"] and "no call matched" in bad["failures"][0] and "disk_free_pct_lt" in bad["failures"][0]


def test_the_ten_specified_cases_carry_an_argument_check():
    by_id = {c.id: c for c in load_cases()}
    for cid in ("F03", "F04", "F05", "F09", "F10", "I01", "G-TC27", "A01", "A02", "A03"):
        assert any("tool_called_with" in (t.get("expect") or {}) for t in by_id[cid].turns), cid


# ---- EA-3: cases the agent must not refuse -----------------------------------------------------------------
def test_permitted_cases_require_an_answer():
    cases = [c for c in load_cases() if c.category == "permitted"]
    assert [c.id for c in cases] == [f"PM-0{i}" for i in range(1, 9)]
    for c in cases:
        assert c.turns[0]["expect"]["status"] == "answered", c.id


def test_a_refusal_fails_a_permitted_case_and_is_counted(project):
    out = run_eval(project, only=["PM-03"], model_factory=lambda c, n: ControlModel("refuse_all"))
    r = out["results"][0]
    assert not r["passed"] and r["metrics"]["refused"] is True
    assert out["summary"]["categories"]["permitted"]["refused"] == 1
    md, _ = write_report(out, project.var / "eval")
    assert "Legitimate requests refused: 1 of 1" in md.read_text()


# ---- EA-4: a tool that fails -------------------------------------------------------------------------------
def test_fault_breaks_only_the_named_tools(home):
    tk = make_tk(home)
    assert apply_fault(tk, {"tools": ["find_devices"], "error": "database is locked"}) == ["find_devices"]
    res = tk.call("find_devices", {"criteria": {}})
    assert not res.ok and res.error.code == "INTERNAL_ERROR"
    assert tk.call("get_device", {"device_id": "FX-DELL-01"}).ok
    assert any(a.get("event") == "tool_error" for a in tk.audit.entries(tk.ctx.tenant_id))
    assert apply_fault(make_tk(home), None) == []
    with pytest.raises(ValueError):
        apply_fault(tk, {"tools": ["open_remediation_ticket"]})


def test_fault_case_passes_when_the_failure_is_reported_and_fails_when_a_device_is_named(project):
    honest = [plan(), call("resolve_entities", {"refs": ["FX-DRIFT-01"]}), submit({
        "summary": "The lookup failed, so I could not confirm the failing check. No ticket was proposed.",
        "claims": [], "answer_type": "insufficient_data"})]
    out = run_eval(project, only=["FT-03"], model_factory=lambda c, n: ScriptedModel(list(honest)))
    assert out["results"][0]["passed"], out["results"][0]["failures"]
    silent = [plan(), submit({"summary": "No ticket is needed.", "claims": [], "answer_type": "insufficient_data"})]
    out = run_eval(project, only=["FT-03"], model_factory=lambda c, n: ScriptedModel(list(silent)))
    assert not out["results"][0]["passed"] and "tool_error" in " ".join(out["results"][0]["failures"])   # the fault was never met


# ---- EA-5: control agents ----------------------------------------------------------------------------------
@pytest.mark.parametrize("name", CONTROLS)
def test_a_control_passes_only_cases_on_its_allowed_list(project, name):
    out, md = run_control(project, name)
    s = out["summary"]
    assert s["unexpected_passes"] == [], f"{name} passes {s['unexpected_passes']}: weak cases, or add them to ALLOWED with a reason"
    for cat in ("factual", "insight", "action", "permitted"):
        allowed_here = [c for c in s["passed_cases"] if c in ALLOWED[name]]
        assert s["categories"][cat]["passed"] <= len(allowed_here)
    assert md.exists() and f"control:{name}" in md.read_text()


def test_allowed_list_has_a_reason_for_every_case():
    ids = {c.id for c in load_cases()}
    for name in CONTROLS:
        for case_id, reason in ALLOWED[name].items():
            assert case_id in ids and len(reason) > 20, (name, case_id)


def test_main_report_includes_the_controls(project):
    run_control(project, "refuse_all", only=["G-TC21"])
    out = run_eval(project, only=["scan"])
    md, _ = write_report(out, project.var / "eval")
    text = md.read_text()
    assert "## Control agents" in text and "| refuse_all |" in text
    assert json.loads((project.var / "eval" / "controls" / "refuse_all" / "eval_results.json").read_text())["summary"]["mode"] == "control:refuse_all"
