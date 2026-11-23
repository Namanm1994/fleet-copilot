"""get_device_history counts for the model; the verifier compares sizes, not signs.

Seen live: I07 counted "13 passes" itself (rejected, rightly: nothing to check it against);
I04 wrote "declining 0.51% a day" for a slope of -0.51 (rejected twice, wrongly).
"""

from __future__ import annotations

from fleet_copilot.agent.models import FinalAnswer
from fleet_copilot.agent.verifier import Verifier, _matches
from fleet_copilot.tools.read_tools import series_summary
from tests.test_tools import make_tk


def pts(*values):
    return [{"date": f"2026-06-{i + 1:02d}", "value": v, "evidence_id": f"e{i}"} for i, v in enumerate(values)]


# ---- the summary -----------------------------------------------------------------------------------
def test_compliance_series_summary():
    s = series_summary(pts("pass", "pass", "fail", "pass", "pass"))
    assert s == {"points": 5, "changes": 2, "current": "pass", "current_since": "2026-06-04", "pass_count": 4,
                 "fail_count": 1, "first_fail": "2026-06-03", "last_fail": "2026-06-03"}


def test_numeric_series_summary():
    s = series_summary(pts(40.0, 38.5, 38.5, 35.0))
    assert s == {"points": 4, "changes": 2, "current": 35.0, "current_since": "2026-06-04", "min": 35.0, "max": 40.0,
                 "first": 40.0, "last": 35.0}


def test_text_series_and_empty_series():
    s = series_summary(pts("Normal", "Normal", "Service Recommended"))
    assert s == {"points": 3, "changes": 1, "current": "Service Recommended", "current_since": "2026-06-03"}
    assert series_summary([]) == {"points": 0}
    assert series_summary(pts("pass", "pass")) == {"points": 2, "changes": 0, "current": "pass", "current_since": "2026-06-01",
                                                   "pass_count": 2, "fail_count": 0}


def test_tool_returns_a_summary_for_every_series(home):
    tk = make_tk(home)
    res = tk.call("get_device_history", {"device_id": "FX-BLIP-01", "metrics": ["compliance:screen_lock", "disk_free_pct"]})
    assert res.ok and set(res.data["summary"]) == {"compliance:screen_lock", "disk_free_pct"}
    comp = res.data["summary"]["compliance:screen_lock"]
    series = res.data["series"]["compliance:screen_lock"]
    assert comp["points"] == len(series) and comp["fail_count"] == 1 and comp["pass_count"] == len(series) - 1
    assert comp["first_fail"] == comp["last_fail"] and comp["current"] == "pass"
    assert res.data["summary"]["disk_free_pct"]["min"] <= res.data["summary"]["disk_free_pct"]["max"]


def test_counts_from_the_summary_pass_the_verifier_and_made_up_ones_fail(home):  # seen live (I07)
    tk = make_tk(home)
    res = tk.call("get_device_history", {"device_id": "FX-BLIP-01", "metrics": ["compliance:screen_lock"]})
    s = res.data["summary"]["compliance:screen_lock"]
    ids = res.evidence_ids

    def check(passes, fails):
        return Verifier(tk).verify(FinalAnswer(summary="FX-BLIP-01 is not drifting.", definitions_used=["min_points_for_trend"], claims=[
            {"text": f"Screen lock history for FX-BLIP-01 shows {passes} passes and {fails} fail, on {s['first_fail']}.",
             "evidence_ids": ids, "values": {"pass_count": passes, "fail_count": fails}}]))

    assert check(s["pass_count"], s["fail_count"]).ok
    wrong = check(s["pass_count"] + 20, s["fail_count"])
    assert not wrong.ok and any("rule 3" in e for e in wrong.errors)


# ---- sizes, not signs --------------------------------------------------------------------------------
def test_matches_compares_sizes():
    allowed = {-0.507, -0.2, 7.6, 993.0}
    assert _matches(0.51, allowed) and _matches(-0.51, allowed) and _matches(0.507, allowed)   # rounded, either sign
    assert _matches(0.2, allowed) and _matches(-7.6, allowed)
    assert _matches(0.5, allowed)                                    # 0.507 rounded to one decimal is still that value
    assert not _matches(0.61, allowed) and not _matches(0.4, allowed) and not _matches(994.0, allowed)


def test_a_decline_written_without_its_minus_sign_verifies(home):  # seen live (I04)
    tk = make_tk(home)
    res = tk.call("run_insights", {"detectors": ["low_storage"]})
    slope = res.data["insights"][0]["data"]["slope_pct_per_day"]
    assert slope < 0

    def check(number):
        return Verifier(tk).verify(FinalAnswer(summary="One device is low on disk.", definitions_used=["low_disk", "low_disk_persistent"], claims=[
            {"text": f"FX-DISK-01 is losing about {number}% of free space per day.", "evidence_ids": [res.evidence_ids[0]]}]))

    assert check(f"{abs(slope):.2f}").ok, check(f"{abs(slope):.2f}").errors
    wrong = check(f"{abs(slope) + 0.37:.2f}")
    assert not wrong.ok and any("rule 3" in e for e in wrong.errors)
