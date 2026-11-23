"""Which model and settings produced a result, and the saved request log."""

from __future__ import annotations

import dataclasses
import json
import shutil

import pytest

from evals.runner import compare_runs, run_eval, write_report
from fleet_copilot.agent.session import CopilotSession
from fleet_copilot.config import load_app_config
from fleet_copilot.llm.base import FakeClock, LLMResponse
from fleet_copilot.llm.recorded import CassetteModel
from fleet_copilot.llm.scripted import ScriptedModel, Step, call
from fleet_copilot.paths import PROJECT_ROOT, Paths
from fleet_copilot.runinfo import changed_settings, fingerprint
from fleet_copilot.session import SessionContext
from fleet_copilot.stats import collect, render
from fleet_copilot.toolkit import Toolkit
from fleet_copilot.tracing import read_trace
from tests import test_agent
from tests.test_agent import LOW_DISK_ANSWER, SETTINGS, plan, session, submit
from tests.test_tools import AS_OF

STEPS = lambda: [plan(), call("find_devices", {"criteria": {"low_disk": True}}), submit(LOW_DISK_ANSWER)]  # noqa: E731


def ask(home, steps=None):
    sess, model = session(home, steps or STEPS())
    res = sess.ask("Which devices are low on disk space?")
    assert res.status == "answered", res.text
    return sess


# ---- settings in the trace, audit log and stats ------------------------------------------------------
def test_first_trace_event_names_the_model_and_settings(home):
    sess = ask(home)
    first = read_trace(sess.tracer.path, "fixture-999")[0]
    assert first["event"] == "run_settings"
    p = first["payload"]
    assert p["model"] == "scripted" and p["provider"] == "anthropic" and p["source"] == "live"
    assert p["max_output_tokens"] == SETTINGS.max_output_tokens and p["max_output_tokens_per_turn"] == 8000
    assert p["turn_deadline_s"] == 180 and p["prompt_version"] == sess.prompt_version
    assert p["fingerprint"] == fingerprint(p)


def test_no_key_material_in_recorded_settings(home):
    settings = ask(home).run_settings
    assert not [k for k in settings if "key" in k.lower()] and "api_key" not in json.dumps(settings).lower()


def test_audit_log_names_model_and_fingerprint(home):
    sess = ask(home)
    started = [json.loads(line) for line in home.audit_log.read_text(encoding="utf-8").splitlines()
               if '"session_started"' in line][0]
    assert sess.run_settings["fingerprint"] in json.dumps(started) and "scripted" in json.dumps(started)


def test_fingerprint_changes_with_a_setting_and_only_then(home, monkeypatch):
    a = ask(home).run_settings
    assert ask(home).run_settings["fingerprint"] == a["fingerprint"]
    monkeypatch.setattr(test_agent, "SETTINGS", dataclasses.replace(SETTINGS, max_output_tokens=4000))
    b = ask(home).run_settings
    assert b["fingerprint"] != a["fingerprint"]
    assert changed_settings(a, b) == {"max_output_tokens": {"previous": 1000, "current": 4000}}
    assert changed_settings(a, None) == {} and changed_settings(a, a) == {}


def test_stats_show_sessions_per_model(home):
    fp = ask(home).run_settings["fingerprint"]
    ask(home)
    st = collect(home.traces, "fixture-999", days=1)
    assert st["models"] == {f"scripted ({fp})": 2} and st["sessions"] == 2
    assert f"Models (sessions): scripted ({fp}) 2" in render(st)


# ---- the request log -----------------------------------------------------------------------------------
def requests_of(sess):
    return [json.loads(line) for line in sess.tracer.requests_path.read_text(encoding="utf-8").splitlines()]


def test_every_request_and_reply_is_saved(home):
    sess = ask(home)
    reqs = requests_of(sess)
    assert [r["request"] for r in reqs] == [1, 2, 3] and all(r["tenant"] == "fixture-999" and r["turn"] == 1 for r in reqs)
    assert reqs[0]["tools"] == [] and reqs[0]["messages"][0]["role"] == "system"       # the planning request
    assert reqs[0]["messages"][-1] == {"role": "user", "content": "Which devices are low on disk space?"}
    assert "find_devices" in reqs[1]["tools"] and "submit_answer" in reqs[1]["tools"]
    assert reqs[1]["response"]["tool_calls"][0]["name"] == "find_devices"
    assert any(m["role"] == "tool" for m in reqs[2]["messages"])                           # the tool result it saw
    calls = [e for e in sess.tracer.events if e["event"] == "llm_call"]
    assert [e["payload"]["request"] for e in calls] == [1, 2, 3]                           # trace points at the log


def test_request_log_sits_outside_the_trace_listing(home):
    sess = ask(home)
    assert sess.tracer.requests_path.parent.name == "prompts"
    assert collect(home.traces, "fixture-999", days=1)["sessions"] == 1                    # not counted as a session


def test_a_failed_request_is_saved_with_its_error(home):
    sess, _ = session(home, [Step(raise_=ValueError("boom sk-ant-abcdefghijklmnopqrstuvwxyz0123456789"))])
    assert sess.ask("Which devices are low on disk space?").status != "answered"
    req = requests_of(sess)[0]
    assert req["response"] is None and "boom" in req["error"] and "sk-ant-abcdef" not in json.dumps(req)


def test_request_log_can_be_turned_off(home, monkeypatch):
    monkeypatch.setattr(test_agent, "SETTINGS", dataclasses.replace(SETTINGS, save_prompts=False))
    sess = ask(home)
    assert not sess.tracer.requests_path.exists()
    assert [e["payload"]["request"] for e in sess.tracer.events if e["event"] == "llm_call"] == [None, None, None]


# ---- recordings ----------------------------------------------------------------------------------------
def make_session(home, model, settings):
    cfg = load_app_config(home.config, "policy.eval.yaml")
    t = cfg.policy.resolve_tenant("fixture-999")
    tk = Toolkit(SessionContext.new(t.tenant_id, t.name, "tester", AS_OF), cfg, home)
    return CopilotSession(tk, model, settings, PROJECT_ROOT / "prompts", clock=FakeClock())


def test_recording_states_what_made_it_and_replay_reports_that(home, tmp_path):
    path = tmp_path / "run-1.json"
    rec = CassetteModel(path, "record", ScriptedModel(STEPS()))
    s1 = make_session(home, rec, SETTINGS)
    assert s1.ask("Which devices are low on disk space?").status == "answered"
    rec.save()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["model"] == "scripted" and saved["settings"]["fingerprint"] == s1.run_settings["fingerprint"]
    assert "messages" not in json.dumps(saved["entries"])  # prompts are not stored in committed recordings

    # Replay under different current settings: the session must name the settings of the recording.
    s2 = make_session(home, CassetteModel(path, "replay"), dataclasses.replace(SETTINGS, max_output_tokens=9999))
    assert s2.ask("Which devices are low on disk space?").status == "answered"
    assert s2.run_settings["fingerprint"] == s1.run_settings["fingerprint"] and s2.run_settings["source"] == "recording"
    assert s2.run_settings["max_output_tokens"] == SETTINGS.max_output_tokens


# ---- eval report ---------------------------------------------------------------------------------------
@pytest.fixture()
def project(tmp_path):
    shutil.copytree(PROJECT_ROOT / "config", tmp_path / "config")
    (tmp_path / "data" / "raw").mkdir(parents=True)
    return Paths(tmp_path)


class Named(ScriptedModel):
    pass


def model_named(name):
    return lambda c, n: Named(STEPS(), model=name)


def test_eval_report_states_the_settings_and_flags_a_change(project):
    out_dir = project.var / "report"
    first = run_eval(project, only=["F08"], model_factory=model_named("model-a"))
    md, js = write_report(first, out_dir)
    settings = first["summary"]["run_settings"]
    assert len(settings) == 1 and settings[0]["model"] == "model-a"
    text = md.read_text(encoding="utf-8")
    assert "## Run settings" in text and "| Model | model-a |" in text and "| Reasoning effort | planner: low |" in text
    assert "| Output limit per call (tokens) | 4000 |" in text and f"| Settings fingerprint | {settings[0]['fingerprint']} |" in text
    assert json.loads(js.read_text(encoding="utf-8"))["results"][0]["settings"]["model"] == "model-a"

    same = run_eval(project, only=["F08"], model_factory=model_named("model-a"))
    md, _ = write_report(same, out_dir)
    assert same["comparison"]["settings_changed"] == {} and "settings changed" not in md.read_text(encoding="utf-8")

    other = run_eval(project, only=["F08"], model_factory=model_named("model-b"))
    md, _ = write_report(other, out_dir)
    assert other["comparison"]["settings_changed"] == {"model": {"previous": "model-a", "current": "model-b"}}
    text = md.read_text(encoding="utf-8")
    assert "The model or its settings changed since the previous run" in text and "- model: model-a → model-b" in text


def test_report_says_so_when_cases_ran_under_different_settings(project):
    out = run_eval(project, only=["F08", "F09"], model_factory=lambda c, n: Named(STEPS(), model=f"model-{c}"))
    md, _ = write_report(out, project.var / "report")
    assert {s["model"] for s in out["summary"]["run_settings"]} == {"model-F08", "model-F09"}
    assert "**Mixed settings:**" in md.read_text(encoding="utf-8")


def test_scan_only_run_has_no_model_settings(project):
    out = run_eval(project, only=["S03"], model_factory=model_named("unused"))
    md, _ = write_report(out, project.var / "report")
    assert out["summary"]["run_settings"] == [] and "No LLM case ran" in md.read_text(encoding="utf-8")


def test_compare_tolerates_older_results_without_settings():
    prev = {"summary": {"mode": "live"}, "results": []}
    cur = {"summary": {"mode": "live", "run_settings": [{"model": "m", "fingerprint": "f"}]}, "results": []}
    c = compare_runs(prev, cur)
    assert c["settings_changed"] == {} and c["settings_mixed"] is False


def test_cut_off_flag_reaches_the_trace(home):
    cut = Step(LLMResponse(content="", input_tokens=10, output_tokens=10, model="scripted", truncated=True))
    sess = ask(home, [plan(), call("find_devices", {"criteria": {"low_disk": True}}), cut, submit(LOW_DISK_ANSWER)])
    assert [e["payload"]["cut_off"] for e in sess.tracer.events if e["event"] == "llm_call"] == [False, False, True, False]
