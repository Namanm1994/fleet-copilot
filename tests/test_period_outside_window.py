"""Rule 8: no answer about a period outside the telemetry window (G13).

Seen live (G-TC20): asked for the battery trend in April, with data from 2026-05-14 to 2026-06-12, the model
ran the fleet-wide detector, never asked for April, and answered "no devices with battery conditions starting
in April 2026" with the caveat "the telemetry window includes April 2026". Every ID and number in that answer
was real, so the verifier accepted it. Whether a named period lies inside the data is a fact code can check.
"""

from __future__ import annotations

from datetime import date

from fleet_copilot.agent.models import FinalAnswer
from fleet_copilot.agent.verifier import Verifier, asked_periods
from tests.test_tools import make_tk

AS_OF = date(2026, 6, 12)


def spans(question):
    return [(a.isoformat(), b.isoformat()) for a, b, _ in asked_periods(question, AS_OF)]


def test_periods_named_in_a_question():
    assert spans("What was the battery trend in April?") == [("2026-04-01", "2026-04-30")]
    assert spans("Anything unusual in December?") == [("2025-12-01", "2025-12-31")]      # the latest December
    assert spans("What happened on May 22?") == [("2026-05-22", "2026-05-22")]
    assert spans("What happened on 2026-04-03?") == [("2026-04-03", "2026-04-03")]
    assert spans("Compare March 2025 with June") == [("2025-03-01", "2025-03-31"), ("2026-06-01", "2026-06-30")]
    assert spans("How were things in 2024?") == [("2024-01-01", "2024-12-31")]
    assert spans("Which devices may need a new battery?") == []                            # the verb, not the month
    assert spans("Is FX-BATT-01@2026-06-07 the first bad day?") == []                      # a date inside an evidence ID


def _tk(home):
    tk = make_tk(home)
    res = tk.call("run_insights", {"detectors": ["battery_eol"]})
    lo, hi = tk.repo.data_window()
    return tk, res, lo[:10], hi[:10]


def test_the_live_answer_is_rejected(home):
    tk, res, lo, hi = _tk(home)
    ins = [e for e in res.evidence_ids if e.startswith("INS-")][:1]
    v = Verifier(tk).verify(FinalAnswer(
        summary="The battery detector found no devices with battery conditions starting in April 2026.",
        claims=[{"text": "No devices were detected with battery conditions originating in April 2026.", "evidence_ids": ins + [res.receipt_id]}],
        definitions_used=res.definitions_used, caveats=["Telemetry window includes April 2026."]), "What was the battery trend in April?")
    rule8 = [e for e in v.errors if "rule 8" in e]
    assert len(rule8) == 1 and lo in rule8[0] and hi in rule8[0] and "April" in rule8[0], v.errors


def test_stating_the_window_as_insufficient_data_passes(home):
    tk, res, lo, hi = _tk(home)
    v = Verifier(tk).verify(FinalAnswer(
        summary=f"There is no telemetry for April 2026: the data covers {lo} to {hi}.", claims=[], answer_type="insufficient_data"),
        "What was the battery trend in April?")
    assert v.ok, v.errors
    no_window = Verifier(tk).verify(FinalAnswer(summary="There is no telemetry for April 2026.", claims=[],
                                                answer_type="insufficient_data"), "What was the battery trend in April?")
    assert any("rule 8" in e for e in no_window.errors)


def test_a_period_inside_the_window_is_untouched(home):
    tk, res, lo, hi = _tk(home)
    month = date.fromisoformat(hi).strftime("%B")
    v = Verifier(tk).verify(FinalAnswer(summary="No battery findings.", claims=[], answer_type="insufficient_data"),
                            f"What was the battery trend in {month}?")
    assert not [e for e in v.errors if "rule 8" in e], v.errors
    mixed = Verifier(tk).verify(FinalAnswer(summary="No battery findings.", claims=[], answer_type="insufficient_data"),
                                f"Compare April with {month}")
    assert not [e for e in mixed.errors if "rule 8" in e], mixed.errors
    no_period = Verifier(tk).verify(FinalAnswer(summary="No battery findings.", claims=[], answer_type="insufficient_data"),
                                    "Which batteries are near end of life?")
    assert not [e for e in no_period.errors if "rule 8" in e]
