"""Read tools, action policy checks, executor and audit (guardrail unit tests from test-plan.md)."""

from __future__ import annotations

import json

import pytest

from fleet_copilot import executor as executor_mod
from fleet_copilot.audit import AuditLog, verify
from fleet_copilot.config import load_app_config
from fleet_copilot.data.repository import Repository, open_readonly
from fleet_copilot.session import SessionContext
from fleet_copilot.toolkit import Toolkit
from fleet_copilot.tools.read_tools import eligible_actions_from_evidence

AS_OF = "2026-06-12T23:59:59Z"


def make_tk(home, tenant="fixture-999", as_of=AS_OF) -> Toolkit:
    cfg = load_app_config(home.config, "policy.eval.yaml")
    t = cfg.policy.resolve_tenant(tenant)
    ctx = SessionContext.new(t.tenant_id, t.name, "tester", as_of)
    tk = Toolkit(ctx, cfg, home)
    tk.new_turn()
    return tk


@pytest.fixture()
def tk(home):
    return make_tk(home)


def ok(result):
    assert result.ok, result.error
    return result


# ---- read tools -----------------------------------------------------------------------------
def test_bound_tools_match_policy(tk):  # TC-07
    names = tk.tool_names()
    assert "lock_device" not in names and "wipe_device" not in names
    assert set(names) == set(tk.config.policy.read_tools) | set(tk.config.policy.action_tools)
    assert tk.call("wipe_device", {"device_id": "FX-DELL-01"}).error.code == "FORBIDDEN_ACTION"


def test_resolve_ambiguous_dell(tk):
    res = ok(tk.call("resolve_entities", {"refs": ["the Dell", "fx-dell-1", "nothing-here"]}))
    dell, exact, none = res.data
    assert dell["status"] == "ambiguous" and len(dell["matches"]) >= 2
    assert exact["status"] == "resolved" and exact["matches"][0]["device_id"] == "FX-DELL-01"
    assert none["status"] == "not_found"


def test_find_devices_low_disk_and_receipt(tk):
    res = ok(tk.call("find_devices", {"criteria": {"low_disk": True}}))
    ids = [d["device_id"] for d in res.data["devices"]]
    assert ids == ["FX-DISK-01"]
    assert res.definitions_used == ["low_disk"]
    assert res.receipt_id and tk.evidence.receipts[res.receipt_id]["result_count"] == 1


def test_find_devices_os_older_excludes_other_platforms(tk):
    res = ok(tk.call("find_devices", {"criteria": {"os_older_than": {"platform": "macos", "version": "15"}}}))
    ids = {d["device_id"] for d in res.data["devices"]}
    assert ids == {"FX-BATT-01", "FX-DRIFT-01"}
    assert any("other platforms were excluded" in c for c in res.caveats)


def test_find_devices_failing_high_severity_laptops(tk):
    res = ok(tk.call("find_devices", {"criteria": {"failing_severity": "high", "is_laptop": True}, "limit": 25}))
    assert res.total_count == 9  # FX-MARKUP-01 + 8 FX-MULTI
    assert all("#disk_encryption" in e or "@" in e for e in res.evidence_ids)


def test_find_devices_no_matches_has_receipt(tk):
    res = ok(tk.call("find_devices", {"criteria": {"manufacturer": "Lenovo"}}))
    assert res.total_count == 0 and res.receipt_id and "no matches" in res.caveats[-1]


def test_other_tenant_invisible(tk):  # TC-02 at tool level
    res = tk.call("get_device", {"device_id": "FX-OTHER-01"})
    assert res.error.code == "NOT_FOUND"
    sw = ok(tk.call("get_software", {"name": "uTorrent"}))
    assert {i["device_id"] for i in sw.data["installed_on"]} == {"FX-UTOR-01"}


def test_history_outside_window_is_insufficient(tk):  # TC-20 at tool level
    res = tk.call("get_device_history", {"device_id": "FX-DELL-01", "metrics": ["battery_condition"],
                                         "start": "2026-04-01", "end": "2026-04-30"})
    assert res.error.code == "INSUFFICIENT_DATA" and "2026-05" in res.error.message


def test_history_short_device_caveat(tk):
    res = ok(tk.call("get_device_history", {"device_id": "FX-SHORT-01", "metrics": ["disk_free_pct"]}))
    assert any("insufficient data" in c for c in res.caveats)


def test_invalid_args_are_typed_errors(tk):
    assert tk.call("find_devices", {"criteria": {"platform": "linux"}}).error.code == "INVALID_ARGUMENT"
    assert tk.call("get_device", {"device_id": "x; DROP TABLE"}).error.code == "INVALID_ARGUMENT"


# ---- action policy checks -------------------------------------------------------------------
def _ticket_args(tk, device="FX-MULTI-01", check="disk_encryption"):
    res = ok(tk.call("get_device", {"device_id": device}))
    eid = next(c["evidence_id"] for c in res.data["compliance"] if c["check_id"] == check)
    return {"device_id": device, "check_id": check, "evidence_ids": [eid]}


def test_ticket_happy_path(tk):
    res = ok(tk.call("open_remediation_ticket", _ticket_args(tk)))
    assert res.data["status"] == "proposed" and res.data["proposal_id"].startswith("PRP-")
    assert tk.audit.entries()[-1]["event"] == "proposed"


def test_invented_evidence_refused(tk):  # TC-26
    res = tk.call("open_remediation_ticket", {"device_id": "FX-MULTI-01", "check_id": "disk_encryption",
                                              "evidence_ids": ["FX-MULTI-01@2026-06-12#disk_encryption"]})
    assert res.error.code == "PRECONDITION_FAILED" and "not retrieved" in res.error.message


def test_evidence_must_be_about_target(tk):  # F-18
    args = _ticket_args(tk, "FX-MULTI-01")
    args["device_id"] = "FX-MULTI-02"
    res = tk.call("open_remediation_ticket", args)
    assert res.error.code == "PRECONDITION_FAILED" and "about FX-MULTI-02" in res.error.message


def test_ticket_for_passing_check_refused(tk):  # TC-28
    res = tk.call("open_remediation_ticket", _ticket_args(tk, "FX-DELL-01", "disk_encryption"))
    assert res.error.code == "PRECONDITION_FAILED" and "passes" in res.error.message


def test_replacement_requires_bad_battery(tk):  # TC-29
    ev = ok(tk.call("get_device", {"device_id": "FX-DELL-01"})).evidence_ids
    res = tk.call("flag_device_for_replacement", {"device_id": "FX-DELL-01", "evidence_ids": ev[:1]})
    assert res.error.code == "PRECONDITION_FAILED"
    ev = ok(tk.call("get_device", {"device_id": "FX-BATT-01"})).evidence_ids
    ok(tk.call("flag_device_for_replacement", {"device_id": "FX-BATT-01", "evidence_ids": ev[:1]}))


def test_duplicate_returns_existing(tk):  # TC-31
    args = _ticket_args(tk)
    first = ok(tk.call("open_remediation_ticket", args))
    second = tk.call("open_remediation_ticket", args)
    assert second.error.code == "DUPLICATE" and first.data["proposal_id"] in second.error.message


def test_rejected_not_reproposed(tk):  # TC-32
    args = _ticket_args(tk)
    pid = ok(tk.call("open_remediation_ticket", args)).data["proposal_id"]
    tk.executor.reject(pid, "tester", "not now")
    assert tk.call("open_remediation_ticket", args).error.code == "REJECTED_THIS_SESSION"


def test_upgrade_spec_allowlist(tk):  # TC-35 / TC-36 (G24)
    ins = ok(tk.call("run_insights", {"detectors": ["ram_constrained"], "device_ids": ["FX-RAM-01"]}))
    eid = ins.evidence_ids[0]
    bad = tk.call("create_upgrade_order", {"device_id": "FX-RAM-01", "component": "ram", "spec_gb": 128, "evidence_ids": [eid]})
    assert bad.error.code == "PRECONDITION_FAILED" and "Allowed" in bad.error.message
    down = tk.call("create_upgrade_order", {"device_id": "FX-RAM-01", "component": "ram", "spec_gb": 16, "evidence_ids": [eid]})
    assert down.error.code == "PRECONDITION_FAILED" and "not larger" in down.error.message
    ok(tk.call("create_upgrade_order", {"device_id": "FX-RAM-01", "component": "ram", "spec_gb": 32, "evidence_ids": [eid]}))
    spike = ok(tk.call("run_insights", {"detectors": ["ram_constrained"], "device_ids": ["FX-RAM-02"]})).evidence_ids[0]
    res = tk.call("create_upgrade_order", {"device_id": "FX-RAM-02", "component": "ram", "spec_gb": 64, "evidence_ids": [spike]})
    assert res.error.code == "PRECONDITION_FAILED" and "persistent" in res.error.message


def test_notify_templates_only(tk):  # TC-37 (G25)
    ev = ok(tk.call("get_device", {"device_id": "FX-DISK-01"}))
    emp = ev.data["employee_id"]
    res = tk.call("notify_employee", {"employee_id": emp, "device_id": "FX-DISK-01", "template": "low_disk",
                                      "evidence_ids": ev.evidence_ids[:1], "message": "free text"})
    assert res.error.code == "INVALID_ARGUMENT"
    res = ok(tk.call("notify_employee", {"employee_id": emp, "device_id": "FX-DISK-01", "template": "low_disk",
                                         "evidence_ids": ev.evidence_ids[:1]}))
    assert "less than 15% free" in res.data["preview"]
    wrong = tk.call("notify_employee", {"employee_id": emp, "device_id": "FX-DISK-01", "template": "battery_replacement",
                                        "evidence_ids": ev.evidence_ids[:1]})
    assert wrong.error.code == "PRECONDITION_FAILED"


def test_notify_other_tenant_employee(tk):  # F-19
    ev = ok(tk.call("get_device", {"device_id": "FX-DISK-01"})).evidence_ids
    res = tk.call("notify_employee", {"employee_id": "emp-other-2001", "device_id": "FX-DISK-01",
                                      "template": "low_disk", "evidence_ids": ev[:1]})
    assert res.error.code == "NOT_FOUND"


def test_ticket_note_cannot_name_others(tk):  # F-21
    args = _ticket_args(tk) | {"note": "Same issue as FX-MULTI-02 and emp-fx-1099"}
    assert tk.call("open_remediation_ticket", args).error.code == "INVALID_ARGUMENT"
    args["note"] = "Encryption off\x07 since last week"
    res = ok(tk.call("open_remediation_ticket", args))
    assert "\x07" not in res.data["preview"]


def test_stale_device_never_auto_proposed(tk):  # F-20
    ev = ok(tk.call("get_device", {"device_id": "FX-STALE-01"}))
    assert ev.data["stale"] is True
    args = _ticket_args(tk, "FX-STALE-01", "screen_lock")
    auto = tk.call("open_remediation_ticket", args, origin="unprompted")
    assert auto.error.code == "PRECONDITION_FAILED" and "stale" in auto.error.message
    asked = ok(tk.call("open_remediation_ticket", args))
    assert asked.data["warnings"] and "stale" in asked.data["warnings"][0]


def test_software_removal(tk):
    sw = ok(tk.call("get_software", {"device_id": "FX-UTOR-01", "name": "torrent"}))
    eid = sw.data["software"][0]["evidence_id"]
    ok(tk.call("request_software_removal", {"device_id": "FX-UTOR-01", "software_name": "µTorrent", "evidence_ids": [eid]}))
    chrome = ok(tk.call("get_software", {"device_id": "FX-UTOR-01", "name": "chrome"})).evidence_ids
    res = tk.call("request_software_removal", {"device_id": "FX-UTOR-01", "software_name": "Google Chrome", "evidence_ids": chrome})
    assert res.error.code == "PRECONDITION_FAILED"


def test_acknowledge_limits(tk):  # TC-39 / TC-40
    res = tk.call("acknowledge_finding", {"insight_id": "INS-x-y-z", "reason": "known", "until": "2026-07-01"})
    assert res.error.code == "PRECONDITION_FAILED"
    ins = ok(tk.call("run_insights", {"detectors": ["low_storage"]}))
    iid = ins.evidence_ids[0]
    too_long = tk.call("acknowledge_finding", {"insight_id": iid, "reason": "known", "until": "2026-10-11"})
    assert too_long.error.code == "PRECONDITION_FAILED" and "90 days" in too_long.error.message
    pid = ok(tk.call("acknowledge_finding", {"insight_id": iid, "reason": "vendor ticket open", "until": "2026-07-01"})).data["proposal_id"]
    tk.executor.approve(pid, "tester")
    again = ok(tk.call("run_insights", {"detectors": ["low_storage"]}))
    assert iid not in again.evidence_ids


def test_auto_proposal_cap(tk):  # TC-33
    ok(tk.call("find_devices", {"criteria": {"failing_severity": "high"}}))
    eligible = [e for e in eligible_actions_from_evidence(tk, tk.evidence.this_turn())
                if e["action"] == "open_remediation_ticket"]
    assert len(eligible) >= 8
    made = [tk.call(e["action"], {**e["params"], "evidence_ids": e["evidence_ids"]}, origin="unprompted") for e in eligible]
    assert sum(r.ok for r in made) == 5
    assert {r.error.code for r in made if not r.ok} == {"AUTO_PROPOSAL_CAP"}


def test_batch_cap(tk):  # G23
    # emulate 10 earlier proposals of this action in the same turn
    tk.turn_counters["open_remediation_ticket"] = 10
    assert tk.call("open_remediation_ticket", _ticket_args(tk)).error.code == "BATCH_LIMIT"


# ---- executor -------------------------------------------------------------------------------
def test_approve_executes_into_outbox(tk, home):
    pid = ok(tk.call("open_remediation_ticket", _ticket_args(tk))).data["proposal_id"]
    assert not home.outbox.exists()  # TC-30: nothing before approval
    done = tk.executor.approve(pid, "tester")
    assert done["status"] == "executed"
    rec = json.loads(home.outbox.read_text(encoding="utf-8").splitlines()[0])
    assert rec["proposal_id"] == pid and rec["tenant"] == "fixture-999" and rec["approved_by"] == "tester"


def test_stale_precondition_at_execution(home):  # TC-38
    tk = make_tk(home)
    ins = ok(tk.call("run_insights", {"detectors": ["compliance_drift"], "device_ids": ["FX-DRIFT-01"]}))
    drift = next(i for i in ins.data["insights"] if i["data"].get("kind") == "drift")
    pid = ok(tk.call("open_remediation_ticket", {"device_id": "FX-DRIFT-01", "check_id": "screen_lock",
                                                 "evidence_ids": [drift["insight_id"]]})).data["proposal_id"]
    early = make_tk(home, as_of="2026-06-05T23:59:59Z")
    out = early.executor.approve(pid, "tester")
    assert out["status"] == "stale_precondition" and not home.outbox.exists()


def test_failed_delivery_and_idempotent_retry(tk, home):  # TC-41
    pid = ok(tk.call("open_remediation_ticket", _ticket_args(tk))).data["proposal_id"]
    executor_mod.arm_failure()
    assert tk.executor.approve(pid, "tester")["status"] == "failed"
    assert tk.executor.retry(pid, "tester")["status"] == "executed"
    assert tk.executor.retry(pid, "tester")["status"] == "executed"
    assert len(home.outbox.read_text(encoding="utf-8").splitlines()) == 1


def test_other_tenant_proposals_invisible(home):  # F-19
    tk = make_tk(home)
    pid = ok(tk.call("open_remediation_ticket", _ticket_args(tk))).data["proposal_id"]
    other = make_tk(home, tenant="fixture-998")
    with pytest.raises(KeyError):
        other.executor.approve(pid, "intruder")
    assert other.store.list() == []


# ---- audit ----------------------------------------------------------------------------------
def test_audit_entries_and_chain(tk, home):  # TC-46 / TC-50
    pid = ok(tk.call("open_remediation_ticket", _ticket_args(tk))).data["proposal_id"]
    tk.executor.approve(pid, "tester")
    events = [e["event"] for e in tk.audit.entries()]
    assert events[-3:] == ["proposed", "approved", "executed"]
    okay, n, msg = verify(home.audit_log)
    assert okay and n == len(events) and msg.startswith("OK")


def test_audit_tamper_detection(tmp_path):  # TC-51
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    for i in range(5):
        log.append("proposed", tenant="t", proposal_id=f"PRP-{i}")
    lines = path.read_text(encoding="utf-8").splitlines()

    path.write_text("\n".join(lines[:2] + lines[3:]) + "\n", encoding="utf-8")
    assert verify(path) == (False, 2, "line 3: prev mismatch (an entry before it was deleted, inserted or reordered)")
    edited = json.loads(lines[1]) | {"proposal_id": "PRP-99"}
    path.write_text("\n".join([lines[0], json.dumps(edited)] + lines[2:]) + "\n", encoding="utf-8")
    assert verify(path)[2].startswith("line 2: hash mismatch")
    path.write_text("\n".join([lines[0], lines[2], lines[1]] + lines[3:]) + "\n", encoding="utf-8")
    assert verify(path)[0] is False


def test_repository_cannot_be_swapped(home, loaded_db):
    cfg = load_app_config(home.config, "policy.eval.yaml")
    ctx = SessionContext.new("fixture-999", "Fixture", "t", AS_OF)
    wrong = Repository(open_readonly(loaded_db), "fixture-998", AS_OF)
    with pytest.raises(RuntimeError):
        Toolkit(ctx, cfg, home, repo=wrong)
