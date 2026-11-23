"""Fixes from the second full live run (new prompts, 53 of 55 passed, one fallback answer).

G-TC17: "Check the Dell". The tool said the reference was ambiguous (5 Dells); the model read all five anyway.
I04:    "What changed?" The answer said "five devices drifted" and kept which five, and since when, in its
        values, which were never shown.
I07:    a correct claim ("only 3 devices fail screen_lock: ...") was rejected three times as an incomplete
        list, because the result it cited also listed devices failing other checks. The answer fell back.
G-TC20: "no data for April 2026" was rejected once because no tool had run, so no tool result held "2026".
"""

from __future__ import annotations

from fleet_copilot.agent.graph import ambiguous_refs, clarification_text
from fleet_copilot.agent.models import FinalAnswer
from fleet_copilot.agent.render import claim_details, render_turn
from fleet_copilot.agent.verifier import Verifier
from fleet_copilot.llm.scripted import call
from tests.test_agent import plan, session, submit
from tests.test_tools import make_tk


# ---- an ambiguous reference ends the turn with a question written in code ---------------------------------------
def test_ambiguous_reference_asks_and_reads_no_device(home):
    sess, model = session(home, [plan(entities=["the Dell"], steps=[{"tool": "resolve_entities", "purpose": "which Dell"}]),
                                 call("resolve_entities", {"refs": ["the Dell"]}),
                                 call("get_device", {"device_id": "FX-DELL-01"})])     # must never be reached
    res = sess.ask("Check the Dell")
    assert res.status == "clarify" and not res.proposals
    assert "FX-DELL-01" in res.text and "FX-DELL-02" in res.text and "Which one do you mean?" in res.text
    assert len(model.steps) == 1                                                       # the get_device step was not used
    assert not any("get_device" in blob for blob in sess.tk.tool_corpus)


def test_an_employee_id_is_not_an_ambiguous_reference(home):
    tk = make_tk(home)
    emp = tk.repo.device("FX-DELL-01")["employee_id"]
    resolved = tk.call("resolve_entities", {"refs": [emp, "FX-DELL-01"]}).data
    assert ambiguous_refs(resolved) == []
    dells = tk.call("resolve_entities", {"refs": ["the Dell"]}).data
    assert [a["ref"] for a in ambiguous_refs(dells)] == ["the Dell"]
    assert "matches" in clarification_text(ambiguous_refs(dells))


# ---- list details are shown, and what is shown is verified -----------------------------------------------------------
def test_listed_details_are_rendered_once():
    claim = {"text": "Two devices drifted on screen_lock.", "evidence_ids": ["Q-1-1"],
             "values": {"devices": ["FX-DRIFT-01: failing since 2026-06-07", "FX-FLAP-01: failing since 2026-06-11"],
                        "count": 2, "check": "screen_lock"}}
    assert claim_details(claim) == ["FX-DRIFT-01: failing since 2026-06-07", "FX-FLAP-01: failing since 2026-06-11"]
    assert claim_details(claim, shown="... FX-DRIFT-01: failing since 2026-06-07 ...") == ["FX-FLAP-01: failing since 2026-06-11"]
    text = render_turn(tenant_name="Fixture Corp", as_of="2026-06-12", status="answered", note=None, proposals=[], skipped=[],
                       thresholds=make_thresholds(), answer={"summary": "Two devices drifted.", "claims": [claim], "caveats": []})
    assert "    - FX-DRIFT-01: failing since 2026-06-07" in text and text.count("FX-FLAP-01") == 1


def make_thresholds():
    from fleet_copilot.config import load_app_config
    from fleet_copilot.paths import PROJECT_ROOT, Paths
    return load_app_config(Paths(PROJECT_ROOT).config, "policy.eval.yaml").thresholds


def test_a_wrong_number_in_a_listed_detail_is_rejected(home):
    tk = make_tk(home)
    res = tk.call("find_devices", {"criteria": {"low_disk": True}})
    free = tk.repo.latest_snapshot("FX-DISK-01")["disk_free_pct"]

    def check(detail):
        return Verifier(tk).verify(FinalAnswer(summary="One device is low on disk.", definitions_used=["low_disk"], claims=[
            {"text": "One device is low on disk.", "evidence_ids": [res.receipt_id], "values": {"devices": [detail]}}]))

    assert check(f"FX-DISK-01: {free}% free").ok
    wrong = check("FX-DISK-01: 61.3% free")
    assert not wrong.ok and any("listed item" in e and "61.3" in e for e in wrong.errors), wrong.errors


# ---- a claim about one check is complete when it names that check's devices ------------------------------------------
def test_completeness_is_judged_for_the_check_the_claim_names(home):
    tk = make_tk(home)
    res = tk.call("get_compliance_overview", {})
    failing: dict[str, set[str]] = {}
    for e in res.evidence_ids:
        if "#" in e:
            failing.setdefault(e.rsplit("#", 1)[1], set()).add(e.split("@")[0])
    assert len(failing) > 1 and len(failing["screen_lock"]) >= 2
    lock = sorted(failing["screen_lock"])

    def check(devices):
        text = f"On the latest snapshot these devices fail the screen_lock check: {', '.join(devices)}."
        v = Verifier(tk).verify(FinalAnswer(summary="Screen lock failures.", claims=[{"text": text, "evidence_ids": [res.receipt_id]}],
                                            definitions_used=res.definitions_used))
        return [e for e in v.errors if "rule 6" in e]

    assert check(lock) == []                                   # every screen_lock device, none of the others: complete
    assert check(lock[:-1])                                    # one screen_lock device left out: still caught


# ---- the year of a period from the question does not need a tool result ----------------------------------------------
def test_period_named_back_with_no_tool_run(home):
    tk = make_tk(home)
    lo, hi = (d[:10] for d in tk.repo.data_window())
    v = Verifier(tk).verify(FinalAnswer(summary=f"There is no telemetry for April {lo[:4]}: the data covers {lo} to {hi}.",
                                        claims=[], answer_type="insufficient_data"), "What was the battery trend in April?")
    assert tk.tool_corpus == [] and v.ok, v.errors
    wrong = Verifier(tk).verify(FinalAnswer(summary=f"There is no telemetry for April 2019: the data covers {lo} to {hi}.",
                                            claims=[], answer_type="insufficient_data"), "What was the battery trend in April?")
    assert not wrong.ok


def test_submit_still_works_after_a_resolved_reference(home):
    sess, _ = session(home, [plan(), call("resolve_entities", {"refs": ["FX-DELL-01"]}),
                             submit({"summary": "Nothing to report.", "claims": [], "answer_type": "insufficient_data"})])
    assert sess.ask("Check FX-DELL-01").status == "answered"
