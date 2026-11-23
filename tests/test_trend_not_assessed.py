"""run_insights says which devices have too little history for a trend only when asked about specific devices.

Seen live (F08): asked "which devices are low on disk?", the model also named a device from the old
`insufficient_data_devices` list. That device had been checked and was fine; only its trend was unknown.
"""

from __future__ import annotations

import json

from fleet_copilot.agent.models import FinalAnswer
from fleet_copilot.agent.verifier import Verifier
from fleet_copilot.scan import render_scan, run_scan
from tests.test_tools import make_tk


def test_fleet_wide_call_gives_a_count_and_no_device_ids(home):
    tk = make_tk(home)
    res = tk.call("run_insights", {"detectors": ["low_storage"]})
    assert res.ok and res.data["trend_not_assessed"] == {"low_storage": {"count": 1}}
    assert "FX-SHORT-01" not in res.model_dump_json()                       # nothing for the model to repeat
    note = [c for c in res.caveats if "Too few snapshots" in c][0]
    assert "1 device(s)" in note and "latest snapshot was still checked" in note and "Pass device_ids" in note


def test_call_about_specific_devices_names_them(home):
    tk = make_tk(home)
    res = tk.call("run_insights", {"detectors": ["low_storage"], "device_ids": ["FX-SHORT-01", "FX-DISK-01"]})
    assert res.data["trend_not_assessed"] == {"low_storage": {"count": 1, "device_ids": ["FX-SHORT-01"]}}
    note = [c for c in res.caveats if "Too few snapshots" in c][0]
    assert "FX-SHORT-01" in note and "Pass device_ids" not in note


def test_nothing_reported_when_every_device_has_enough_history(home):
    tk = make_tk(home)
    res = tk.call("run_insights", {"detectors": ["low_storage"], "device_ids": ["FX-DISK-01"]})
    assert res.data["trend_not_assessed"] == {} and not [c for c in res.caveats if "Too few snapshots" in c]


def test_naming_that_device_in_a_fleet_wide_answer_is_now_rejected(home):
    tk = make_tk(home)
    tk.call("run_insights", {"detectors": ["low_storage"]})
    v = Verifier(tk).verify(FinalAnswer(summary="One device is low on disk.", definitions_used=["low_disk", "low_disk_persistent"], claims=[
        {"text": "FX-DISK-01 is low on disk.", "evidence_ids": ["Q-1-1"]},
        {"text": "FX-SHORT-01 had insufficient data.", "evidence_ids": ["Q-1-1"]}]))
    assert not v.ok and any("'FX-SHORT-01' does not appear in any tool result" in e for e in v.errors)


def test_count_of_devices_without_a_trend_can_be_stated(home):
    tk = make_tk(home)
    tk.call("run_insights", {"detectors": ["low_storage"]})
    v = Verifier(tk).verify(FinalAnswer(summary="One device is low on disk.", definitions_used=["low_disk", "low_disk_persistent"], claims=[
        {"text": "FX-DISK-01 is low on disk; 1 device has too little history for a trend.", "evidence_ids": ["Q-1-1"]}]))
    assert v.ok, v.errors


def test_scan_reports_the_count(home):
    tk = make_tk(home)
    result = run_scan(tk)
    assert all(set(v) == {"count"} for v in result.insufficient.values())
    text = render_scan(result, tk.config.thresholds)
    assert "Too few snapshots for a trend on" in text and "latest snapshot was still checked" in text
    assert "FX-SHORT-01" not in json.dumps(result.insufficient)
