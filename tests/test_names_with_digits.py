"""A digit inside a model name or hostname is part of the name.

Seen live (G-TC15, I09): "FX-INJ-02 is a Dell XPS 15 laptop" was rejected as a count of fifteen laptops, three
times in one case, until the answer fell back to a cut-down version.
"""

from __future__ import annotations

from fleet_copilot.agent.models import FinalAnswer
from fleet_copilot.agent.verifier import Verifier
from tests.test_tools import make_tk


def test_model_name_is_not_a_count(home):
    tk = make_tk(home)
    res = tk.call("get_device", {"device_id": "FX-DELL-01"})
    model = tk.repo.device("FX-DELL-01")["model_name"]
    assert res.ok and any(ch.isdigit() for ch in model), model
    ev = [e for e in res.evidence_ids if "#" not in e]

    def check(text):
        return Verifier(tk).verify(FinalAnswer(summary=text, claims=[{"text": text, "evidence_ids": ev}]))

    ok = check(f"FX-DELL-01 is a {model} laptop.")
    assert ok.ok, ok.errors
    still_a_count = check("15 laptops match FX-DELL-01.")
    assert not still_a_count.ok and any("rule 3" in e for e in still_a_count.errors)


def test_a_model_name_no_tool_returned_is_not_exempt(home):
    tk = make_tk(home)
    res = tk.call("find_devices", {"criteria": {"low_disk": True}})   # FX-DISK-01 only; no Dell in any result
    model = tk.repo.device("FX-DELL-01")["model_name"]
    if model in "\n".join(tk.tool_corpus):
        return
    v = Verifier(tk).verify(FinalAnswer(summary="One device is low on disk.", definitions_used=["low_disk"],
                                        claims=[{"text": f"FX-DISK-01 is a {model} laptop.", "evidence_ids": [res.receipt_id]}]))
    assert not v.ok
