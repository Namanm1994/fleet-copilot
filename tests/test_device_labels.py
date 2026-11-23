"""One function decides how a device is shown: a hostname always carries its device ID."""

from __future__ import annotations

from fleet_copilot.agent.render import device_names, label_devices
from fleet_copilot.llm.scripted import call
from tests.test_agent import plan, session, submit
from tests.test_tools import make_tk

NAMES = {"acme-macbook-2": "1LYSSFD074BB", "acme-macbook-21": "ZZZZ00000021"}


def test_hostname_gets_its_id_once():
    assert label_devices("acme-macbook-2 has uTorrent; acme-macbook-2 again.", NAMES) == \
        "acme-macbook-2 (1LYSSFD074BB) has uTorrent; acme-macbook-2 again."


def test_nothing_added_when_the_id_is_already_there():
    text = "Device 1LYSSFD074BB (acme-macbook-2) fails screen lock."
    assert label_devices(text, NAMES) == text


def test_whole_names_only():
    assert label_devices("acme-macbook-21 is fine.", NAMES) == "acme-macbook-21 (ZZZZ00000021) is fine."
    assert label_devices("my-acme-macbook-2-old is fine.", NAMES) == "my-acme-macbook-2-old is fine."


def test_an_id_inside_an_evidence_id_does_not_count_as_naming_the_device():
    out = label_devices("acme-macbook-2 is flagged (INS-prohibited_software-1LYSSFD074BB-01e55d).", NAMES)
    assert out.startswith("acme-macbook-2 (1LYSSFD074BB) is flagged")


def test_case_and_no_names():
    assert label_devices("ACME-MACBOOK-2 is up.", NAMES) == "ACME-MACBOOK-2 (1LYSSFD074BB) is up."
    assert label_devices("acme-macbook-2 is up.", None) == "acme-macbook-2 is up."


def test_only_devices_the_tools_returned_are_named():
    devices = [{"device_id": "AAAA11112222", "hostname": "host-a"}, {"device_id": "BBBB33334444", "hostname": "host-b"}]
    assert device_names(devices, seen_in='{"device_id": "AAAA11112222"}') == {"host-a": "AAAA11112222"}


def test_a_hostname_shared_by_two_devices_is_left_alone():
    devices = [{"device_id": "AAAA11112222", "hostname": "shared"}, {"device_id": "BBBB33334444", "hostname": "shared"},
               {"device_id": "CCCC55556666", "hostname": None}, {"device_id": "DDDD77778888", "hostname": "DDDD77778888"}]
    assert device_names(devices, seen_in="AAAA11112222 BBBB33334444 CCCC55556666 DDDD77778888") == {}


def test_another_companys_device_can_never_be_named(home):
    tk = make_tk(home)
    tk.call("find_devices", {"criteria": {}})
    names = device_names(tk.repo.devices(), "\n".join(tk.tool_corpus))
    other = {d["device_id"] for d in make_tk(home, tenant="fixture-998").repo.devices()}
    assert names and not other & set(names.values())


def test_answer_names_the_device_both_ways_end_to_end(home):   # seen live (I03): correct answer, device given by hostname only
    answer = {"summary": "fx-macbook-air-disk is low on disk space.",
              "claims": [{"text": "fx-macbook-air-disk has 7.6% free on its system volume.",
                          "evidence_ids": ["FX-DISK-01@2026-06-12", "Q-1-1"], "values": {"disk_free_pct": 7.6}}],
              "definitions_used": ["low_disk"], "caveats": [], "answer_type": "answer"}
    sess, _ = session(home, [plan(), call("find_devices", {"criteria": {"low_disk": True}}), submit(answer)])
    res = sess.ask("Which devices are low on disk space?")
    assert res.status == "answered", res.text
    assert "fx-macbook-air-disk (FX-DISK-01) is low on disk space." in res.text
    assert "• fx-macbook-air-disk (FX-DISK-01) has 7.6% free" in res.text
    assert res.answer["summary"] == answer["summary"]             # the stored answer keeps the model's own words
