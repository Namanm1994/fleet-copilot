"""Verifier rules in isolation (TC-22, TC-25)."""

from __future__ import annotations

from fleet_copilot.agent.models import FinalAnswer
from fleet_copilot.agent.verifier import Verifier
from tests.test_tools import make_tk


def _tk_with_low_disk(home):
    tk = make_tk(home)
    tk.call("find_devices", {"criteria": {"low_disk": True}})  # FX-DISK-01, receipt Q-1-1
    return tk


def _answer(claims, **kw):
    return FinalAnswer(summary=kw.pop("summary", "Low disk results."), claims=claims,
                       definitions_used=kw.pop("definitions_used", ["low_disk"]), **kw)


def test_valid_answer_passes(home):
    tk = _tk_with_low_disk(home)
    v = Verifier(tk).verify(_answer([{"text": "FX-DISK-01 is low on disk.", "evidence_ids": ["FX-DISK-01@2026-06-12", "Q-1-1"],
                                      "values": {"count": 1}}]))
    assert v.ok, v.errors


def test_tc22_each_fabrication_fails_its_rule(home):
    tk = _tk_with_low_disk(home)
    fake_id = Verifier(tk).verify(_answer([{"text": "FX-DISK-01 is low.", "evidence_ids": ["FX-DISK-01@2026-01-01"]}]))
    assert not fake_id.ok and any("rule 1" in e for e in fake_id.errors)
    fake_device = Verifier(tk).verify(_answer([{"text": "FX-NOPE-0042 is low.", "evidence_ids": ["Q-1-1"]}]))
    assert not fake_device.ok and any("rule 2" in e and "FX-NOPE-0042" in e for e in fake_device.errors)
    wrong_count = Verifier(tk).verify(_answer([{"text": "3 devices are low on disk.", "evidence_ids": ["Q-1-1"],
                                                "values": {"count": 3}}]))
    assert not wrong_count.ok and any("rule 3" in e for e in wrong_count.errors)


def test_threshold_must_be_stated(home):  # G15, rule 4
    tk = _tk_with_low_disk(home)
    v = Verifier(tk).verify(_answer([{"text": "FX-DISK-01 is low.", "evidence_ids": ["Q-1-1"]}], definitions_used=[]))
    assert not v.ok and any("rule 4" in e and "low_disk" in e for e in v.errors)


def test_tc25_claim_without_evidence_rejected(home):
    tk = _tk_with_low_disk(home)
    v = Verifier(tk).verify(_answer([{"text": "Disks are fine overall.", "evidence_ids": []}]))
    assert not v.ok and any("rule 5" in e for e in v.errors)
    allowed = Verifier(tk).verify(FinalAnswer(summary="Not enough data.", claims=[{"text": "Too few snapshots.", "evidence_ids": []}],
                                              answer_type="insufficient_data"))
    assert allowed.ok, allowed.errors


def test_no_matches_needs_zero_receipt(home):
    tk = _tk_with_low_disk(home)  # Q-1-1 returned 1 row, so it can't back "no matches"
    v = Verifier(tk).verify(FinalAnswer(summary="None.", claims=[{"text": "No devices matched.", "evidence_ids": ["Q-1-1"]}],
                                        definitions_used=["low_disk"], answer_type="no_matches"))
    assert not v.ok and any("0 results" in e for e in v.errors)
