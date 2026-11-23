"""The view across sessions (fleet stats) and run-to-run eval comparison."""

from __future__ import annotations

import json
import shutil

import pytest
from click.testing import CliRunner

from evals.runner import compare_runs, run_eval, write_report
from fleet_copilot import cli
from fleet_copilot.llm.scripted import ScriptedModel, call
from fleet_copilot.paths import PROJECT_ROOT, Paths
from fleet_copilot.stats import collect, render
from tests.test_agent import LOW_DISK_ANSWER, plan, session, submit


def _bad_answer():
    bad = json.loads(json.dumps(LOW_DISK_ANSWER))
    bad["claims"][0]["text"] = "FX-FAKE-0099 and FX-DISK-01 are low on disk."
    return bad


# ---- fleet stats ----------------------------------------------------------------------------------
def test_stats_aggregate_across_sessions(home):
    s1, _ = session(home, [plan(), call("find_devices", {"criteria": {"low_disk": True}}), submit(_bad_answer()),
                           submit(LOW_DISK_ANSWER)])
    s1.ask("Which devices are low on disk?")
    s2, _ = session(home, [plan(in_scope=False, refusal_reason="Out of scope."),
                           plan(), call("get_device", {"device_id": "NOPE-0000001"}),
                           submit({"summary": "No such device.", "claims": [], "definitions_used": [], "caveats": [],
                                   "answer_type": "insufficient_data"})])
    s2.ask("weather?")
    s2.ask("show NOPE-0000001")
    st = collect(home.traces, "fixture-999", days=1)
    assert st["sessions"] == 2 and st["turns"] == 3
    assert st["verifier_checks"] == 3 and st["verifier_rejections"] == 1 and st["verifier_rejection_rate"] == 0.333
    assert st["turn_outcomes"] == {"answered": 2, "refused": 1}
    assert st["tool_errors"] == {"NOT_FOUND": 1}
    assert st["llm_calls"] >= 7 and st["latency_p95_s"] is not None
    text = render(st)
    assert "1 of 3 answers rejected at first (33%)" in text and "Tool errors: NOT_FOUND 1" in text


def test_stats_are_tenant_scoped_and_bounded_by_days(home):
    s, _ = session(home, [plan(in_scope=False, refusal_reason="No.")])
    s.ask("weather?")
    assert collect(home.traces, "fixture-998", days=7)["sessions"] == 0
    assert collect(home.traces, "fixture-999", days=7, today="2020-01-01")["sessions"] == 0
    assert "No sessions in this period." in render(collect(home.traces, "fixture-998", days=7))


def test_stats_command(home, monkeypatch):
    s, _ = session(home, [plan(in_scope=False, refusal_reason="No.")])
    s.ask("weather?")
    monkeypatch.setattr(cli, "POLICY_FILE", "policy.eval.yaml")
    monkeypatch.setenv("FLEET_HOME", str(home.root))
    r = CliRunner().invoke(cli.main, ["stats", "--tenant", "fixture-999", "--json"], catch_exceptions=False)
    assert r.exit_code == 0 and json.loads(r.output)["turns"] == 1
    assert CliRunner().invoke(cli.main, ["stats", "--tenant", "nobody"]).exit_code == 2


# ---- comparing eval runs --------------------------------------------------------------------------
GOOD = [plan(), call("find_devices", {"criteria": {"low_disk": True}}), submit(LOW_DISK_ANSWER)]
BAD = [plan(), call("find_devices", {"criteria": {"low_disk": True}}), submit(
    {"summary": "No devices are low on disk.", "claims": [{"text": "None matched.", "evidence_ids": ["Q-1-1"], "values": {}}],
     "definitions_used": [], "caveats": [], "answer_type": "answer"})]


@pytest.fixture()
def project(tmp_path):
    shutil.copytree(PROJECT_ROOT / "config", tmp_path / "config")
    (tmp_path / "data" / "raw").mkdir(parents=True)
    return Paths(tmp_path)


def test_eval_report_compares_with_the_previous_run(project):
    out_dir = project.var / "report"
    first = run_eval(project, only=["F08", "S03"], model_factory=lambda c, n: ScriptedModel(list(BAD)))
    write_report(first, out_dir)
    assert "comparison" not in first  # nothing to compare the first run with
    second = run_eval(project, only=["F08", "S03"], model_factory=lambda c, n: ScriptedModel(list(GOOD)))
    md, _ = write_report(second, out_dir)
    cmp_ = second["comparison"]
    assert cmp_["comparable"] and cmp_["now_passing"] == ["F08"] and cmp_["now_failing"] == []
    assert "## Changes since the previous run" in md.read_text() and "- Now passing: F08" in md.read_text()
    third = run_eval(project, only=["F08", "S03"], model_factory=lambda c, n: ScriptedModel(list(BAD)))
    write_report(third, out_dir)
    assert third["comparison"]["now_failing"] == ["F08"]
    assert len(list((out_dir / "history").glob("eval_results-*.json"))) == 3  # every run is kept


def test_compare_ignores_skipped_cases_and_flags_mode_change():
    prev = {"summary": {"mode": "live", "generated_at": "t0", "tokens_in": 100},
            "results": [{"case_id": "A", "passed": True, "skipped": None}, {"case_id": "B", "passed": False, "skipped": "no cassette"}]}
    cur = {"summary": {"mode": "recorded", "tokens_in": 80},
           "results": [{"case_id": "A", "passed": False, "skipped": None}, {"case_id": "B", "passed": True, "skipped": None}]}
    c = compare_runs(prev, cur)
    assert c["comparable"] is False and c["now_failing"] == ["A"] and c["newly_run"] == ["B"] and c["now_passing"] == []
    assert c["deltas"]["tokens_in"] == {"previous": 100, "current": 80}
