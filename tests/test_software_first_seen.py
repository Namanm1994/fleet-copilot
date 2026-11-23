"""Both routes of get_software return the same facts about a software entry."""

from __future__ import annotations

from tests.test_tools import make_tk


def entry(items, device_id=None):
    return next(i for i in items if "torrent" in i["name"].lower() and (device_id is None or i.get("device_id") == device_id))


def test_first_seen_by_device_and_by_name_agree(home):   # seen live (I02): the by-device route had no first_seen
    tk = make_tk(home)
    by_device = entry(tk.call("get_software", {"device_id": "FX-UTOR-01"}).data["software"])
    by_name = entry(tk.call("get_software", {"name": "utorrent"}).data["installed_on"], "FX-UTOR-01")
    assert by_device["first_seen"] == by_name["first_seen"] == "2026-06-09"
    shared = ("name", "version", "first_seen", "prohibited", "evidence_id")
    assert {k: by_device[k] for k in shared} == {k: by_name[k] for k in shared}
    assert "device_id" not in by_device and by_name["device_id"] == "FX-UTOR-01"


def test_every_entry_on_a_device_has_first_seen(home):
    tk = make_tk(home)
    items = tk.call("get_software", {"device_id": "FX-UTOR-01"}).data["software"]
    assert items and all(i["first_seen"] and i["first_seen"] <= "2026-06-12" for i in items)


def test_name_filter_on_a_device_still_works(home):
    tk = make_tk(home)
    res = tk.call("get_software", {"device_id": "FX-UTOR-01", "name": "utorrent"})
    assert res.ok and res.total_count == 1 and res.data["software"][0]["first_seen"] == "2026-06-09"
