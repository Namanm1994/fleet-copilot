"""Written dates: several days after or before one month name, and a day followed by ordinary words.

Seen live (G-TC21): the model wrote "three snapshots (June 10-12)" and then "June 10, 11 and 12". Both are
correct. The verifier read only "June 10" as a date and rejected 11, 12 and -12 as numbers no tool returned,
twice, until the answer fell back to a cut-down version.
"""

from __future__ import annotations

import pytest

from fleet_copilot.agent.models import FinalAnswer
from fleet_copilot.agent.verifier import Verifier, quantities
from fleet_copilot.scan import ScanResult, ScanSummary, verify_summary
from tests.test_tools import make_tk


def needles(text):
    return [n for _, n in quantities(text).dates]


@pytest.mark.parametrize("text,expected", [
    ("three snapshots (June 10-12), showing", ["-06-10", "-06-12"]),
    ("on Jun 10–12, 2026.", ["2026-06-10", "2026-06-12"]),
    ("from June 10 to 12 it held", ["-06-10", "-06-12"]),
    ("on June 10, 11 and 12.", ["-06-10", "-06-11", "-06-12"]),
    ("on June 10, 11, and 12 show", ["-06-10", "-06-11", "-06-12"]),
    ("on June 10th and 11th were", ["-06-10", "-06-11"]),
    ("recorded June 10, 11, 12.", ["-06-10", "-06-11", "-06-12"]),
    ("between June 10 and June 12", ["-06-10", "-06-12"]),
    ("from 10 to 12 June 2026", ["2026-06-10", "2026-06-12"]),
    ("on 10, 11 and 12 June 2026", ["2026-06-10", "2026-06-11", "2026-06-12"]),
    ("on the 22nd of May 2026", ["2026-05-22"]),
])
def test_several_days_with_one_month(text, expected):
    q = quantities(text)
    assert [n for _, n in q.dates] == expected and q.numbers == []


@pytest.mark.parametrize("text", ["On May 22 the check failed.", "on May 22nd the check failed", "It failed on May 22."])
def test_a_day_followed_by_ordinary_words_is_a_date(text):
    q = quantities(text)
    assert [n for _, n in q.dates] == ["-05-22"] and q.numbers == []


def test_numbers_near_a_month_that_are_not_days():
    q = quantities("in May 16 GB devices")                       # a measurement
    assert q.dates == [] and q.measurements == [16.0]
    q = quantities("in May 12 devices failed")                   # a count
    assert q.dates == [] and [n for n, _ in q.counts] == [12.0]
    q = quantities("on May 22, 3 devices failed")                # a date, then a count
    assert needles("on May 22, 3 devices failed") == ["-05-22"] and [n for n, _ in q.counts] == [3.0]
    q = quantities("on May 3, 5 checks failed")                  # a bare comma does not make 5 a day
    assert [n for _, n in q.dates] == ["-05-03"] and q.values == [5.0]
    q = quantities("values 3, 10 June")                          # nor does it make 3 one
    assert [n for _, n in q.dates] == ["-06-10"] and q.values == [3.0]
    q = quantities("45% on June 10, 45% on June 11")
    assert [n for _, n in q.dates] == ["-06-10", "-06-11"] and q.measurements == [45.0, 45.0]


def test_the_live_answer_passes_and_a_wrong_day_fails(home):
    tk = make_tk(home)
    res = tk.call("get_device_history", {"device_id": "FX-SHORT-01", "metrics": ["disk_free_pct"]})
    ids = res.evidence_ids

    def check(summary):
        return Verifier(tk).verify(FinalAnswer(
            summary=summary, claims=[{"text": "Free disk space on FX-SHORT-01 was 45% on each snapshot.", "evidence_ids": ids}],
            definitions_used=["min_points_for_trend"], answer_type="insufficient_data"))

    assert check("FX-SHORT-01 has three snapshots (June 10-12), each at 45% free.").ok
    assert check("FX-SHORT-01 was at 45% free on June 10, 11 and 12.").ok
    wrong = check("FX-SHORT-01 was at 45% free on June 10, 11 and 14.")
    assert not wrong.ok and any("date" in e and "rule 3" in e for e in wrong.errors), wrong.errors


def test_scan_summary_dates_are_checked():
    result = ScanResult(tenant_name="Acme", as_of="2026-06-12T11:00:00Z", clean_detectors=[], insufficient={},
                        definitions_used=[], proposals=[], skipped=[], receipt_id=None,
                        insights=[{"device_id": "FX-DRIFT-01", "detector": "compliance_drift", "severity": "high",
                                   "finding": "screen_lock has failed since 2026-05-22"}])
    ok = verify_summary(ScanSummary(summary="FX-DRIFT-01 has failed screen lock since May 22 (as of June 12, 2026)."), result)
    assert ok == [], ok
    bad = verify_summary(ScanSummary(summary="FX-DRIFT-01 has failed screen lock since May 2."), result)
    assert bad == ["date 'May 2' is not in the findings"], bad
    bad_iso = verify_summary(ScanSummary(summary="FX-DRIFT-01 has failed screen lock since 2026-04-01."), result)
    assert bad_iso == ["date '2026-04-01' is not in the findings"], bad_iso


def test_a_period_from_the_question_may_be_named_back(home):
    """Seen live (G-TC20): "there is no battery data for April 2026" was rejected three times because no tool
    result holds an April date. The user asked about April; the tools can only say the data starts in May."""
    tk = make_tk(home)
    res = tk.call("get_device_history", {"device_id": "FX-SHORT-01", "metrics": ["disk_free_pct"]})

    def check(summary, question):
        return Verifier(tk).verify(FinalAnswer(
            summary=summary, claims=[{"text": "FX-SHORT-01 has snapshots from 2026-06-10.", "evidence_ids": res.evidence_ids}],
            definitions_used=["min_points_for_trend"], answer_type="insufficient_data"), question)

    assert check("There is no data for April 2026.", "What was the battery trend in April?").ok
    assert check("There is no data for April 3.", "What happened on April 3?").ok
    not_asked = check("There is no data for March 2026.", "What was the battery trend in April?")
    assert not not_asked.ok and any("March 2026" in e for e in not_asked.errors)
    wrong_year = check("There is no data for April 2019.", "What was the battery trend in April?")
    assert not wrong_year.ok and any("April 2019" in e for e in wrong_year.errors)
    a_day_not_asked = check("Nothing was recorded on April 3.", "What was the battery trend in April?")
    assert not a_day_not_asked.ok
