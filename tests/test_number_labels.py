"""Every number in an answer is labelled once (date, measurement, count, plain value) and checked by that label's rule.

The four phrases marked "seen live" each made the verifier reject a correct answer in a live run.
"""

from __future__ import annotations

import pytest

from fleet_copilot.agent.models import FinalAnswer
from fleet_copilot.agent.verifier import COUNT_KEY_RE, Verifier, quantities
from tests.test_tools import make_tk

BATT = ["FX-BATT-01@2026-06-07", "FX-BATT-01@2026-06-12"]
DEFS = ["battery_eol", "battery_degrading"]


def battery_tk(home):
    tk = make_tk(home)
    tk.call("run_insights", {"detectors": ["battery_eol"]})   # receipt Q-1-1: 2 findings, 24 devices checked, cycle count 993
    return tk


def check(tk, text, evidence, values=None, summary="Battery findings."):
    return Verifier(tk).verify(FinalAnswer(summary=summary, definitions_used=DEFS,
                                           claims=[{"text": text, "evidence_ids": evidence, "values": values or {}}]))


# ---- the labelling function -----------------------------------------------------------------------------
def test_each_number_gets_one_label():
    q = quantities("5 of 12 devices fail; 16 GB device; 2.6% free; since May 2026; on 2026-05-22; cycle count 993; in 14 days")
    assert [n for n, _ in q.counts] == [5.0] and q.counts[0][1] == "5 of 12 devices"
    assert sorted(q.measurements) == [2.6, 14.0, 16.0]
    assert sorted(q.values) == [12.0, 993.0]
    assert q.dates == [("2026-05-22", "2026-05-22"), ("May 2026", "2026-05-")]


@pytest.mark.parametrize("text,needle", [("May 2026", "2026-05-"), ("22 May 2026", "2026-05-22"), ("May 22", "-05-22"),
                                         ("May 22, 2026", "2026-05-22"), ("March 3rd, 2026", "2026-03-03"),
                                         ("during 2026", "2026-")])
def test_written_dates(text, needle):
    q = quantities(f"Failing since {text}.")
    assert [d[1] for d in q.dates] == [needle] and q.numbers == []


def test_things_that_are_not_dates_or_units():
    assert quantities("It may fail; 3 devices may 5 times").dates == []        # the verb, not the month
    assert quantities("2026 devices").dates == [] and quantities("2026 devices").counts[0][0] == 2026.0
    assert quantities("evidence FX-BATT-01@2026-06-07 and Q-1-1").dates == []  # dates inside evidence IDs belong to the ID
    assert quantities("16 GB device").counts == [] and quantities("16 devices").counts[0][0] == 16.0


def test_only_result_count_names_are_counts():
    for name in ("count", "total", "n", "device_count", "num_devices", "total_devices", "result_count", "matches_count"):
        assert COUNT_KEY_RE.match(name), name
    for name in ("cycle_count", "fail_count", "software_count", "num_snapshots", "snapshot_count"):
        assert not COUNT_KEY_RE.match(name), name


# ---- seen live: correct answers that were rejected ------------------------------------------------------------
def test_month_and_year_is_a_date(home):          # seen live (I04): "number 2026 does not match any tool value"
    v = check(battery_tk(home), "FX-BATT-01 has reported Service Recommended since early June 2026.", BATT)
    assert v.ok, v.errors


def test_size_before_device_is_not_a_count(home):  # seen live (I03): "'16 GB device' doesn't match the cited evidence counts"
    tk = make_tk(home)
    dev = tk.call("get_device", {"device_id": "FX-RAM-01"})
    gb = int(dev.data["ram_total_gb"])
    v = Verifier(tk).verify(FinalAnswer(summary="One device.", claims=[
        {"text": f"FX-RAM-01 is a {gb} GB device.", "evidence_ids": [dev.evidence_ids[0]]}]))
    assert v.ok, v.errors
    wrong = Verifier(tk).verify(FinalAnswer(summary="One device.", claims=[
        {"text": f"FX-RAM-01 is a {gb + 7} GB device.", "evidence_ids": [dev.evidence_ids[0]]}]))
    assert not wrong.ok and any("rule 3" in e for e in wrong.errors)           # the size itself is still checked


def test_cycle_count_is_a_value_not_a_tally(home):  # seen live (I05): "cycle_count=993 doesn't match ... receipt counts"
    tk = battery_tk(home)
    assert check(tk, "FX-BATT-01 is at cycle count 993.", BATT, {"cycle_count": 993}).ok
    wrong = check(tk, "FX-BATT-01 is at cycle count 994.", BATT, {"cycle_count": 994})
    assert not wrong.ok and any("994" in e for e in wrong.errors)


def test_checked_count_from_the_cited_result_is_a_count(home):  # seen live (I05): "'24 devices' doesn't match"
    tk = battery_tk(home)
    assert tk.evidence.receipts["Q-1-1"]["numbers"].count(24) == 1
    assert check(tk, "The detector checked 24 devices.", ["Q-1-1"], {"devices_checked": 24}).ok
    wrong = check(tk, "The detector checked 25 devices.", ["Q-1-1"])
    assert not wrong.ok and any("'25 devices'" in e for e in wrong.errors)


def test_naming_an_unrelated_device_is_not_a_partial_list(home):  # seen live (I05): rule 6 on FX-SHORT-01
    v = check(battery_tk(home), "The detector checked 24 devices; FX-SHORT-01 had too little data.", ["Q-1-1"])
    assert v.ok, v.errors


# ---- what must still be rejected -----------------------------------------------------------------------------
def test_wrong_device_count_still_fails(home):
    tk = battery_tk(home)
    v = check(tk, "17 devices need a new battery.", BATT)
    assert not v.ok and any("'17 devices'" in e for e in v.errors)
    by_key = check(tk, "Devices need a new battery.", BATT, {"device_count": 17})
    assert not by_key.ok and any("device_count=17" in e for e in by_key.errors)


def test_a_count_must_come_from_the_evidence_it_cites(home):
    tk = battery_tk(home)                       # 24 is in Q-1-1, but this claim cites only the two snapshots
    v = check(tk, "24 devices need a new battery.", BATT)
    assert not v.ok and any("'24 devices'" in e for e in v.errors)


def test_dates_are_now_checked_against_tool_results(home):
    tk = battery_tk(home)
    assert check(tk, "FX-BATT-01 changed condition on 2026-06-07.", BATT).ok
    wrong_day = check(tk, "FX-BATT-01 changed condition on 2026-04-01.", BATT)
    assert not wrong_day.ok and any("date '2026-04-01'" in e for e in wrong_day.errors)
    wrong_month = check(tk, "FX-BATT-01 changed condition in March 2026.", BATT)
    assert not wrong_month.ok and any("date 'March 2026'" in e for e in wrong_month.errors)
    wrong_year = check(tk, "FX-BATT-01 has been failing since 2019.", BATT)
    assert not wrong_year.ok and any("date '2019'" in e for e in wrong_year.errors)


def test_summary_dates_and_numbers_are_checked_too(home):
    tk = battery_tk(home)
    good = check(tk, "FX-BATT-01 is at cycle count 993.", BATT, summary="One laptop needs a battery, flagged in June 2026.")
    assert good.ok, good.errors
    bad = check(tk, "FX-BATT-01 is at cycle count 993.", BATT, summary="One laptop needs a battery, flagged in January 2026.")
    assert not bad.ok and any("summary" in e and "January 2026" in e for e in bad.errors)
