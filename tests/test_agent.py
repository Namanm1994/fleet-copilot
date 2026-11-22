"""Agent graph end to end with a scripted model (no network)."""

from __future__ import annotations

import json

import pytest

from fleet_copilot.agent.session import CopilotSession
from fleet_copilot.config import LLMSettings, load_app_config
from fleet_copilot.llm.base import FakeClock, LLMResponse
from fleet_copilot.llm.scripted import ScriptedModel, Step, call, say
from fleet_copilot.paths import PROJECT_ROOT
from fleet_copilot.session import SessionContext
from fleet_copilot.toolkit import Toolkit
from tests.test_tools import AS_OF

SETTINGS = LLMSettings(provider="anthropic", model="scripted", base_url=None, api_key_env="X", temperature=0,
                       max_output_tokens=1000, call_timeout_s=30, network_retries=3, backoff_base_s=1.0, repair_retries=2)


def plan(**kw) -> Step:
    base = {"intent_class": "lookup", "in_scope": True, "entities": [], "needs_clarification": False, "steps": []}
    return say(json.dumps(base | kw))


def submit(answer: dict) -> Step:
    return call("submit_answer", answer, call_id="submit")


def session(home, steps, interactive=False, clock=None):
    cfg = load_app_config(home.config, "policy.eval.yaml")
    t = cfg.policy.resolve_tenant("fixture-999")
    tk = Toolkit(SessionContext.new(t.tenant_id, t.name, "tester", AS_OF), cfg, home)
    model = ScriptedModel(steps, clock=clock)
    return CopilotSession(tk, model, SETTINGS, PROJECT_ROOT / "prompts", clock=clock or FakeClock(),
                          interactive=interactive), model


LOW_DISK_ANSWER = {
    "summary": "1 device is low on disk space.",
    "claims": [{"text": "FX-DISK-01 has 7.6% free on its system volume.",
                "evidence_ids": ["FX-DISK-01@2026-06-12", "Q-1-1"], "values": {"disk_free_pct": 7.6, "count": 1}}],
    "definitions_used": ["low_disk"], "caveats": [], "answer_type": "answer",
}


def test_happy_path_grounded_answer(home):
    sess, model = session(home, [plan(steps=[{"tool": "find_devices", "purpose": "low disk"}]),
                                 call("find_devices", {"criteria": {"low_disk": True}}),
                                 submit(LOW_DISK_ANSWER)])
    res = sess.ask("Which devices are low on disk space?")
    assert res.status == "answered", res.text
    assert res.verification["ok"] is True
    assert "System volume has less than 15% free" in res.text and "As of 2026-06-12" in res.text
    assert not res.proposals
    tool_msg = [m for m in model.seen[-1] if m["role"] == "tool"][0]
    assert tool_msg["content"].startswith("<tool_data>")
    events = [e["event"] for e in sess.tracer.events]
    for expected in ("input", "plan", "tool_call", "tool_result", "verifier_result", "final_answer", "usage"):
        assert expected in events


def test_verifier_rejects_invented_device_then_accepts(home):
    bad = json.loads(json.dumps(LOW_DISK_ANSWER))
    bad["claims"][0]["text"] = "FX-FAKE-0099 and FX-DISK-01 are low on disk."
    sess, model = session(home, [plan(), call("find_devices", {"criteria": {"low_disk": True}}), submit(bad),
                                 submit(LOW_DISK_ANSWER)])
    res = sess.ask("Which devices are low on disk?")
    assert res.status == "answered" and res.verification["ok"]
    feedback = [m for m in model.seen[-1] if m["role"] == "tool" and "Verification failed" in m["content"]]
    assert feedback and "FX-FAKE-0099" in feedback[0]["content"]


def test_verifier_fallback_strips_unverifiable(home):
    bad = json.loads(json.dumps(LOW_DISK_ANSWER))
    bad["claims"].append({"text": "FX-DISK-01 has 999 GB free.", "evidence_ids": ["FX-DISK-01@2026-06-12"], "values": {"free_gb": 999}})
    sess, _ = session(home, [plan(), call("find_devices", {"criteria": {"low_disk": True}}),
                             submit(bad), submit(bad), submit(bad)])
    res = sess.ask("low disk?")
    assert res.verification["fallback"] is True
    assert "999" not in res.text and "could not be verified" in res.text


def test_no_matches_must_cite_zero_receipt(home):
    answer = {"summary": "No Lenovo devices.", "claims": [{"text": "No devices matched.", "evidence_ids": ["Q-1-1"], "values": {"count": 0}}],
              "definitions_used": [], "caveats": [], "answer_type": "no_matches"}
    sess, _ = session(home, [plan(), call("find_devices", {"criteria": {"manufacturer": "Lenovo"}}), submit(answer)])
    res = sess.ask("Any Lenovo laptops?")
    assert res.status == "answered" and res.verification["ok"]


def test_out_of_scope_refused_without_tools(home):  # TC-11
    sess, model = session(home, [plan(in_scope=False, refusal_reason="I can't help with weather.")])
    res = sess.ask("What's the weather in London?")
    assert res.status == "refused" and "I can answer questions" in res.text
    assert len(model.seen) == 1


def test_clarification_before_reading_data(home):  # TC-17
    sess, model = session(home, [plan(needs_clarification=True, clarification_question="Which Dell: FX-DELL-01 or FX-DELL-02?")])
    res = sess.ask("Check the Dell")
    assert res.status == "clarify" and "FX-DELL-02" in res.text
    assert not sess.tk.evidence.records


def test_input_too_long(home):  # TC-10
    sess, model = session(home, [])
    res = sess.ask("x" * 5000)
    assert res.status == "refused" and not model.seen


def test_plan_repair_exhausted(home):  # G35
    sess, _ = session(home, [say("nope"), say("still nope"), say("{bad")])
    res = sess.ask("hello")
    assert res.status == "plan_failed" and "rephrase" in res.text


def test_tool_call_cap(home):  # TC-43 at graph level
    steps = [plan()] + [call("get_device", {"device_id": f"FX-MULTI-0{i % 8 + 1}"}, call_id=f"c{i}") for i in range(9)]
    steps.append(submit({"summary": "Checked devices.", "claims": [{"text": "FX-MULTI-01 is failing disk_encryption.",
                                                                    "evidence_ids": ["FX-MULTI-01@2026-06-12#disk_encryption"], "values": {}}],
                         "definitions_used": [], "caveats": [], "answer_type": "answer"}))
    sess, model = session(home, steps)
    res = sess.ask("look at the multi devices")
    capped = [m for m in model.seen[-1] if m["role"] == "tool" and "Tool-call limit" in m["content"]]
    assert capped and res.usage["tool_calls"] == 8


def test_budget_stop_returns_partial(home):  # TC-42 at graph level
    huge = Step(LLMResponse(content=json.dumps({"in_scope": True, "steps": []}), input_tokens=70000, output_tokens=10))
    sess, _ = session(home, [huge])
    res = sess.ask("anything")
    assert res.status == "partial" and "budget" in res.text.lower()


def test_auto_proposals_capped_and_approval_interrupt(home):  # TC-33 at graph level + G19
    answer = {"summary": "9 devices fail a high-severity check.",
              "claims": [{"text": "9 devices fail disk_encryption (high).", "evidence_ids": ["Q-1-1"], "values": {"count": 9}}],
              "definitions_used": [], "caveats": [], "answer_type": "answer"}
    sess, _ = session(home, [plan(), call("find_devices", {"criteria": {"failing_severity": "high"}}), submit(answer)],
                      interactive=True)
    res = sess.ask("Show devices failing high-severity checks")
    assert res.status == "answered", res.text
    assert len(res.proposals) == 5 and all(p["origin"] == "unprompted" for p in res.proposals)
    assert "4 more suggested action(s)" in res.text
    assert res.pending_approval
    assert not sess.tk.paths.outbox.exists()
    first, second = res.proposals[0]["proposal_id"], res.proposals[1]["proposal_id"]
    results = sess.decide({first: "approve", second: ("reject", "not now")})
    assert {r["proposal_id"]: r["status"] for r in results} == {first: "executed", second: "rejected"}
    assert len(sess.tk.paths.outbox.read_text().splitlines()) == 1


def test_requested_action_flow(home):  # TC-27: reads first, then proposes with retrieved evidence
    steps = [plan(intent_class="action"),
             call("get_device", {"device_id": "FX-DRIFT-01"}),
             lambda msgs: call("open_remediation_ticket", {"device_id": "FX-DRIFT-01", "check_id": "screen_lock",
                                                           "evidence_ids": ["FX-DRIFT-01@2026-06-12#screen_lock"]}),
             submit({"summary": "Proposed a ticket for FX-DRIFT-01.", "claims": [
                 {"text": "FX-DRIFT-01 fails screen_lock; ticket proposed.", "evidence_ids": ["FX-DRIFT-01@2026-06-12#screen_lock"], "values": {}}],
                 "definitions_used": [], "caveats": [], "answer_type": "answer"})]
    sess, _ = session(home, steps)
    res = sess.ask("Open a ticket for FX-DRIFT-01's screen lock")
    requested = [p for p in res.proposals if p["origin"] == "requested"]
    assert len(requested) == 1 and requested[0]["action"] == "open_remediation_ticket"
    assert not res.pending_approval  # non-interactive: stays proposed
    assert sess.tk.store.get(requested[0]["proposal_id"])["status"] == "proposed"


def test_history_carries_between_turns(home):
    sess, model = session(home, [plan(), call("find_devices", {"criteria": {"low_disk": True}}), submit(LOW_DISK_ANSWER),
                                 plan(intent_class="follow_up"), submit({"summary": "Same device as before.", "claims": [
                                     {"text": "FX-DISK-01 was the low-disk device.", "evidence_ids": ["FX-DISK-01@2026-06-12"], "values": {}}],
                                     "definitions_used": [], "caveats": [], "answer_type": "answer"})])
    sess.ask("Which devices are low on disk?")
    res = sess.ask("Which one was that again?")
    assert res.status == "answered", res.text
    planner_msgs = model.seen[3]
    assert any(m["role"] == "assistant" and "low on disk" in m["content"] for m in planner_msgs)


def test_injected_text_stays_data(home):  # G10 structural check
    sess, model = session(home, [plan(), call("get_software", {"device_id": "FX-INJ-01"}),
                                 submit({"summary": "FX-INJ-01 has 4 software items.", "claims": [
                                     {"text": "FX-INJ-01 lists 4 installed items.", "evidence_ids": ["Q-1-1"], "values": {"count": 4}}],
                                     "definitions_used": ["prohibited_software"], "caveats": [], "answer_type": "answer"})])
    res = sess.ask("What software is on FX-INJ-01?")
    tool = [m for m in model.seen[-1] if m["role"] == "tool"][0]["content"]
    assert "Ignore previous instructions" in tool and tool.startswith("<tool_data>")
    assert not res.proposals


@pytest.mark.parametrize("answer_type", ["refusal"])
def test_refusal_answers_need_no_evidence(home, answer_type):
    sess, _ = session(home, [plan(), submit({"summary": "I can't wipe devices; I can propose IT actions for approval.", "claims": [],
                                             "definitions_used": [], "caveats": [], "answer_type": answer_type})])
    res = sess.ask("Wipe FX-DELL-01")
    assert res.status == "answered" and res.verification["ok"]


def test_sqlite_checkpointer_resume(home):
    from fleet_copilot.agent.session import sqlite_checkpointer

    answer = {"summary": "9 devices fail a high-severity check.",
              "claims": [{"text": "9 devices fail disk_encryption (high).", "evidence_ids": ["Q-1-1"], "values": {"count": 9}}],
              "definitions_used": [], "caveats": [], "answer_type": "answer"}
    cfg = load_app_config(home.config, "policy.eval.yaml")
    t = cfg.policy.resolve_tenant("fixture-999")
    tk = Toolkit(SessionContext.new(t.tenant_id, t.name, "tester", AS_OF), cfg, home)
    model = ScriptedModel([plan(), call("find_devices", {"criteria": {"failing_severity": "high"}}), submit(answer)])
    sess = CopilotSession(tk, model, SETTINGS, PROJECT_ROOT / "prompts", clock=FakeClock(),
                          checkpointer=sqlite_checkpointer(home.checkpoints_db), interactive=True)
    res = sess.ask("devices failing high-severity checks")
    assert res.pending_approval and home.checkpoints_db.exists()
    pid = res.proposals[0]["proposal_id"]
    assert sess.decide({pid: "later"}) == [{"proposal_id": pid, "status": "pending", "error": None}]
