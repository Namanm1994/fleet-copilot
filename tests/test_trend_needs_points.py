"""Rule 7: no trend from too few snapshots (G13).

Seen live (G-TC21): asked whether FX-SHORT-01's disk usage was trending down, the model read 3 snapshots,
answered "not trending down" and noted in the same breath that 7 are needed for a trend. The answer type
was "answer". The history tool already says a trend cannot be assessed; the verifier now holds the answer to it.
"""

from __future__ import annotations

from fleet_copilot.agent.models import FinalAnswer
from fleet_copilot.agent.verifier import Verifier, trend_statements
from tests.test_tools import make_tk

Q = "Is FX-SHORT-01's disk usage trending down?"
SHORT = ["FX-SHORT-01@2026-06-10", "FX-SHORT-01@2026-06-11", "FX-SHORT-01@2026-06-12"]


def short_history(home):
    tk = make_tk(home)
    res = tk.call("get_device_history", {"device_id": "FX-SHORT-01", "metrics": ["disk_free_pct"]})
    assert res.ok and sorted(res.evidence_ids) == SHORT
    return tk, res.receipt_id


def answer(summary, claims, answer_type="answer"):
    return FinalAnswer(summary=summary, claims=claims, definitions_used=["min_points_for_trend"], answer_type=answer_type)


def test_trend_statements_keep_sentences_that_say_it_cannot_be_told():
    assert trend_statements("Free space is flat at 45%, so disk usage is not trending down.") == [
        "Free space is flat at 45%, so disk usage is not trending down."]
    assert trend_statements("Only 3 snapshots exist, too few for a trend.") == []
    assert trend_statements("A trend needs at least 7 snapshots. Free space was 45% on each day.") == []
    assert trend_statements("Free space fell from 50% to 45% between the two days.") == []


def test_the_live_answer_is_rejected(home):
    tk, rid = short_history(home)
    v = Verifier(tk).verify(answer(
        "FX-SHORT-01's disk free percentage was 45% across the three snapshots, so disk usage is not trending down. "
        "However, only 3 data points exist, which is below the 7-point minimum required for a trend assessment.",
        [{"text": "Disk free percentage on FX-SHORT-01 was 45% on each of the three recorded snapshots.", "evidence_ids": SHORT},
         {"text": "The series shows zero changes, indicating no downward trend in free space.", "evidence_ids": SHORT},
         {"text": "This is insufficient data for a trend: 3 snapshots, fewer than the required 7.", "evidence_ids": [rid]}]), Q)
    rule7 = [e for e in v.errors if "rule 7" in e]
    assert len(rule7) == 3, v.errors
    assert any(e.startswith("summary:") for e in rule7) and any(e.startswith("claim 2:") for e in rule7)
    assert any("insufficient_data" in e for e in rule7)
    assert v.failed_claims == [1]


def test_values_plus_cannot_tell_passes_as_insufficient_data(home):
    tk, rid = short_history(home)
    v = Verifier(tk).verify(answer(
        "A trend cannot be assessed for FX-SHORT-01: only 3 snapshots exist and a trend needs at least 7. "
        "Free disk space was 45% on each of them.",
        [{"text": "Disk free percentage on FX-SHORT-01 was 45% on each of the three recorded snapshots.", "evidence_ids": SHORT},
         {"text": "Too few snapshots for a trend: 3, fewer than the required 7.", "evidence_ids": [rid]}],
        answer_type="insufficient_data"), Q)
    assert v.ok, v.errors


def test_a_question_about_values_keeps_its_answer_type(home):
    tk, rid = short_history(home)
    v = Verifier(tk).verify(answer(
        "FX-SHORT-01 had 45% free disk space on each of its 3 snapshots. Too few snapshots for a trend.",
        [{"text": "Disk free percentage on FX-SHORT-01 was 45% on 2026-06-10, 2026-06-11 and 2026-06-12.", "evidence_ids": SHORT}]),
        "What was FX-SHORT-01's free disk space this week?")
    assert v.ok, v.errors


def test_a_long_history_may_state_a_trend(home):
    tk = make_tk(home)
    res = tk.call("get_device_history", {"device_id": "FX-DISK-01", "metrics": ["disk_free_pct"]})
    assert res.ok and len(res.evidence_ids) >= 7
    v = Verifier(tk).verify(answer("Free disk space on FX-DISK-01 is falling.",
                                   [{"text": "Free disk space on FX-DISK-01 is declining across the window.",
                                     "evidence_ids": [res.receipt_id]}]), "Is FX-DISK-01's disk usage trending down?")
    assert not [e for e in v.errors if "rule 7" in e], v.errors


def test_a_snapshot_also_in_a_long_history_is_not_short(home):
    tk = make_tk(home)
    short = tk.call("get_device_history", {"device_id": "FX-DISK-01", "metrics": ["disk_free_pct"], "start": "2026-06-11"})
    long = tk.call("get_device_history", {"device_id": "FX-DISK-01", "metrics": ["disk_free_pct"]})
    assert short.ok and long.ok and len(short.evidence_ids) == 2 and set(short.evidence_ids) <= set(long.evidence_ids)
    v = Verifier(tk).verify(answer("Free disk space on FX-DISK-01 is falling.",
                                   [{"text": "Free disk space on FX-DISK-01 is declining.", "evidence_ids": long.evidence_ids[-2:]}]),
                            "Is it trending down?")
    assert not [e for e in v.errors if "rule 7" in e], v.errors


def test_a_narrow_window_alone_cannot_carry_a_trend(home):
    tk = make_tk(home)
    short = tk.call("get_device_history", {"device_id": "FX-DISK-01", "metrics": ["disk_free_pct"], "start": "2026-06-11"})
    v = Verifier(tk).verify(answer("Free disk space on FX-DISK-01 is falling.",
                                   [{"text": "Free disk space on FX-DISK-01 is declining.", "evidence_ids": short.evidence_ids}]),
                            "Is it trending down?")
    assert len([e for e in v.errors if "rule 7" in e]) == 3, v.errors
