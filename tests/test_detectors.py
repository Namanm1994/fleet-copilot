"""Detectors on the fixture: each planted pattern is found, and look-alikes are not."""

from fleet_copilot.insights.detectors import Detectors, failing_check_actions, sort_insights


def _by(result, device):
    return [i for i in result.insights if i.device_id == device]


def test_battery(repo, app_config):
    res = Detectors(repo, app_config).battery_eol()
    eol = [i for i in _by(res, "FX-BATT-01") if "condition" in i.finding]
    assert len(eol) == 1 and eol[0].severity == "high"
    assert eol[0].data["since"] == "2026-06-07"
    assert eol[0].eligible_actions[0]["action"] == "flag_device_for_replacement"
    degrading = [i for i in _by(res, "FX-BATT-01") if i.data.get("kind") == "degrading"]
    assert degrading and degrading[0].data["decline_pct"] >= 5
    assert not _by(res, "FX-DELL-01")
    assert "FX-SHORT-01" in res.insufficient_data


def test_ram(repo, app_config):
    res = Detectors(repo, app_config).ram_constrained()
    persistent = _by(res, "FX-RAM-01")[0]
    assert persistent.data["level"] == "persistent"
    assert persistent.eligible_actions[0]["params"]["spec_gb"] == 32
    spike = _by(res, "FX-RAM-02")[0]
    assert spike.data["level"] == "flagged" and not spike.eligible_actions
    assert any("One RAM reading per day" in c for c in spike.caveats)
    assert not _by(res, "FX-DELL-01")


def test_low_storage(repo, app_config):
    res = Detectors(repo, app_config).low_storage()
    disk = _by(res, "FX-DISK-01")[0]
    assert disk.data["persistent"] is True
    assert disk.eligible_actions[0] == {"action": "create_upgrade_order",
                                        "params": {"device_id": "FX-DISK-01", "component": "storage", "spec_gb": 512}}
    assert "low_disk" in disk.definitions_used
    assert not _by(res, "FX-DELL-01")


def test_compliance_drift_vs_blip_vs_flapping(repo, app_config):
    res = Detectors(repo, app_config).compliance_drift()
    drift = [i for i in _by(res, "FX-DRIFT-01") if i.data["kind"] == "drift"]
    assert drift and drift[0].data["check_id"] == "screen_lock" and drift[0].data["consecutive_fails"] == 6
    assert not _by(res, "FX-BLIP-01"), "one-day blip must not count as drift"
    flap = _by(res, "FX-FLAP-01")
    assert flap and flap[0].data["kind"] == "flapping" and flap[0].severity == "low"
    # never-passing checks (os_up_to_date on old Macs) are not drift
    assert not [i for i in _by(res, "FX-BATT-01") if i.data["check_id"] == "os_up_to_date"]


def test_prohibited_software(repo, app_config):
    res = Detectors(repo, app_config).prohibited_software()
    hit = _by(res, "FX-UTOR-01")[0]
    assert hit.data["first_seen"] == "2026-06-09"
    assert hit.eligible_actions[0]["action"] == "request_software_removal"
    assert all(i.device_id != "FX-OTHER-02" for i in res.insights), "other tenant must not appear"


def test_stale_flagged_not_excluded(repo, app_config):
    det = Detectors(repo, app_config)
    assert "FX-STALE-01" in det.stale and "FX-DELL-01" not in det.stale


def test_insight_ids_stable(repo, app_config):
    a = Detectors(repo, app_config).run()
    b = Detectors(repo, app_config).run()
    ids = lambda r: sorted(i.insight_id for res in r.values() for i in res.insights)  # noqa: E731
    assert ids(a) == ids(b)
    ordered = sort_insights([i for res in a.values() for i in res.insights])
    assert ordered[0].severity == "high"


def test_failing_check_actions():
    acts = failing_check_actions("D1", "disk_encryption", "high")
    assert acts[0]["action"] == "open_remediation_ticket" and acts[0]["severity"] == "high"
