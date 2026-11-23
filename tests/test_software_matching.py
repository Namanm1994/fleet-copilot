"""Software names match on whole words; short queries cannot hit unrelated names."""

from __future__ import annotations

import pytest

from fleet_copilot.data.sanitize import software_matches
from tests.test_tools import make_tk


# ---- software matching ----------------------------------------------------------------------------
@pytest.mark.parametrize("query,name,expected", [
    ("go", "Google Chrome", False), ("chrome", "Google Chrome", True), ("google chrome", "Google Chrome", True),
    ("utorrent", "µTorrent", True), ("torrent", "uTorrent", True), ("git", "GitHub Desktop", False),
    ("office", "LibreOffice", True), ("zoom", "Zoom", True), ("", "Zoom", False), ("chrome google", "Google Chrome", False),
])
def test_software_matches(query, name, expected):
    assert software_matches(query, name) is expected


def test_short_query_no_longer_hits_unrelated_software(home):
    tk = make_tk(home)
    assert tk.call("find_devices", {"criteria": {"has_software": "go"}}).total_count == 0
    assert tk.call("find_devices", {"criteria": {"has_software": "chrome"}}).total_count > 0
    hit = tk.call("get_software", {"name": "torrent"})
    assert [i["device_id"] for i in hit.data["installed_on"]] == ["FX-UTOR-01"]
    assert hit.data["installed_on"][0]["first_seen"] == "2026-06-09"
