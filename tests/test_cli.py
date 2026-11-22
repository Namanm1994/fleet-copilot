"""CLI: tenant fail-closed, scan workflow, approvals from a terminal only, escaping, audit, traces."""

from __future__ import annotations

import io
import json

import pytest
from click.testing import CliRunner
from rich.console import Console

from fleet_copilot import cli
from fleet_copilot.audit import verify
from fleet_copilot.llm.scripted import ScriptedModel, call, say
from tests.test_agent import LOW_DISK_ANSWER, plan, submit


@pytest.fixture()
def run(home, monkeypatch):
    monkeypatch.setattr(cli, "POLICY_FILE", "policy.eval.yaml")
    monkeypatch.setenv("FLEET_HOME", str(home.root))
    monkeypatch.delenv("FLEET_OUTBOX_FAIL_NEXT", raising=False)
    cli.set_approval_hook(None)
    runner = CliRunner()

    def _run(*args, **kw):
        return runner.invoke(cli.main, list(args), catch_exceptions=False, **kw)

    yield _run
    cli.set_approval_hook(None)


def test_tenant_required_and_unknown_fail_closed(run, home):  # TC-05
    r = run("chat")
    assert r.exit_code != 0 and "--tenant" in r.output
    home.telemetry_db.unlink()  # prove the tenant check happens before any DB access
    r = run("scan", "--tenant", "globex")
    assert r.exit_code == 2 and "Unknown tenant 'globex'" in r.output and "fleet load" not in r.output


def test_missing_policy_refuses_to_start(run, home):  # TC-08 at CLI level
    (home.config / "policy.eval.yaml").unlink()
    r = run("proposals", "list", "--tenant", "fixture-999")
    assert r.exit_code == 2 and "Configuration error" in r.output


def test_load_writes_db_and_quality_report(run, home, fixture_file):
    home.telemetry_db.unlink()
    r = run("load", str(fixture_file))
    assert r.exit_code == 0, r.output
    assert home.telemetry_db.exists()
    report = json.loads(home.data_quality.read_text())
    assert report["devices"] > 20 and report["unregistered_tenants"] == []


def test_scan_is_a_reproducible_workflow(run, home, tmp_path_factory):
    r1 = run("scan", "--tenant", "fixture-999")
    assert r1.exit_code == 0, r1.output
    out = r1.output
    assert "Fleet scan · Fixture Corp · as of 2026-06-12" in out
    assert "HIGH" in out and "FX-UTOR-01" in out and "FX-BATT-01" in out
    assert "Proposed actions (awaiting your approval; nothing has been executed)" in out
    assert "Approve in an interactive terminal" in out  # CliRunner stdin is not a TTY
    assert not home.outbox.exists()
    from fleet_copilot.store import AppStore

    stored = AppStore(home.app_db, "fixture-999").list()
    assert 0 < len(stored) <= 5 and all(p["origin"] == "scan" and p["status"] == "proposed" for p in stored)
    # second run: same findings, no duplicate proposals
    r2 = run("scan", "--tenant", "fixture-999")
    assert "already have open proposals" in r2.output
    assert len(AppStore(home.app_db, "fixture-999").list()) == len(stored)


def test_scan_other_tenant_sees_only_its_devices(run):
    r = run("scan", "--tenant", "fixture-998")
    assert r.exit_code == 0 and "FX-OTHER-02" in r.output and "FX-UTOR-01" not in r.output


def test_piped_approval_is_refused(run, home):  # F-22
    run("scan", "--tenant", "fixture-999")
    r = run("proposals", "approve", "PRP-1", "--tenant", "fixture-999", input="y\n")
    assert r.exit_code == 1 and "interactive terminal" in r.output
    assert not home.outbox.exists()


def test_approve_with_hook_executes_once(run, home):  # G19, G28
    run("scan", "--tenant", "fixture-999")
    cli.set_approval_hook(lambda p: ("approve", ""))
    r = run("proposals", "approve", "PRP-1", "--tenant", "fixture-999", "--operator", "naman")
    assert r.exit_code == 0 and "PRP-1: executed" in r.output
    r = run("proposals", "approve", "PRP-1", "--tenant", "fixture-999")
    assert "can't be approved" in r.output or "executed" in r.output
    assert len(home.outbox.read_text().splitlines()) == 1
    assert verify(home.audit_log)[0]


def test_other_tenants_proposal_is_not_found(run):  # F-19 at CLI level
    run("scan", "--tenant", "fixture-999")
    cli.set_approval_hook(lambda p: ("approve", ""))
    r = run("proposals", "approve", "PRP-1", "--tenant", "fixture-998")
    assert r.exit_code == 1 and "PRP-1 not found" in r.output


def test_reject_and_list(run):
    run("scan", "--tenant", "fixture-999")
    r = run("proposals", "reject", "PRP-2", "--tenant", "fixture-999", "--reason", "known issue")
    assert "PRP-2: rejected" in r.output
    r = run("proposals", "list", "--tenant", "fixture-999", "--status", "rejected")
    assert "PRP-2" in r.output and "PRP-1 " not in r.output


def test_failed_delivery_then_retry(run, home, monkeypatch):  # failed execution path
    run("scan", "--tenant", "fixture-999")
    cli.set_approval_hook(lambda p: ("approve", ""))
    monkeypatch.setenv("FLEET_OUTBOX_FAIL_NEXT", "1")
    r = run("proposals", "approve", "PRP-1", "--tenant", "fixture-999")
    assert "PRP-1: failed" in r.output
    monkeypatch.delenv("FLEET_OUTBOX_FAIL_NEXT")
    r = run("proposals", "retry", "PRP-1", "--tenant", "fixture-999")
    assert "PRP-1: executed" in r.output
    assert len(home.outbox.read_text().splitlines()) == 1


def test_audit_verify_detects_tampering(run, home):
    run("scan", "--tenant", "fixture-999")
    assert "chain intact" in run("audit", "verify").output
    lines = home.audit_log.read_text().splitlines()
    entry = json.loads(lines[1])
    entry["tenant"] = "fixture-998"
    lines[1] = json.dumps(entry)
    home.audit_log.write_text("\n".join(lines) + "\n")
    r = run("audit", "verify")
    assert r.exit_code == 1 and "line 2" in r.output


def _scripted(monkeypatch, steps):
    model = ScriptedModel(steps)
    monkeypatch.setattr("fleet_copilot.llm.providers.build_model", lambda settings: model)
    return model


def test_ask_json_and_trace_show(run, monkeypatch):
    _scripted(monkeypatch, [plan(), call("find_devices", {"criteria": {"low_disk": True}}), submit(LOW_DISK_ANSWER)])
    r = run("ask", "--tenant", "fixture-999", "--json", "Which devices are low on disk?")
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["status"] == "answered" and out["verification"]["ok"]
    sid = out["session_id"]
    shown = run("trace", "show", sid, "--tenant", "fixture-999").output
    assert "plan" in shown and "verifier_result" in shown and "find_devices" in shown
    other = run("trace", "show", sid, "--tenant", "fixture-998")
    assert other.exit_code == 1 and "No trace" in other.output  # traces are tenant-scoped


def test_chat_session_piped(run, monkeypatch):
    _scripted(monkeypatch, [plan(in_scope=False, refusal_reason="I can't help with weather.")])
    r = run("chat", "--tenant", "fixture-999", input="What's the weather?\nexit\n")
    assert r.exit_code == 0, r.output
    assert "As of 2026-06-12 · Fixture Corp" in r.output and "I can't help with weather." in r.output
    assert "tokens in/out" in r.output and "ended" in r.output


def test_scan_summary_is_verified(run, monkeypatch):
    _scripted(monkeypatch, [say(json.dumps({"summary": "FX-NOPE-0001 is on fire with 42 issues.", "device_ids": []}))])
    r = run("scan", "--tenant", "fixture-999", "--summary")
    assert "Summary skipped: it did not match the findings" in r.output and "FX-NOPE-0001" not in r.output


def _capture(monkeypatch) -> io.StringIO:
    buf = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buf, force_terminal=True, color_system="truecolor", width=200))
    return buf


def test_markup_printed_literally(monkeypatch):  # TC-48
    buf = _capture(monkeypatch)
    cli.say("[green]✓ Approved by IT[/green] and [link=http://x]click[/link]")
    out = buf.getvalue()
    assert "[green]✓ Approved by IT[/green]" in out and "[link=http://x]click[/link]" in out
    assert "\x1b]8;" not in out  # no hyperlink escape


def test_approval_screen_escapes_device_strings(monkeypatch):  # TC-49 (renderer part)
    buf = _capture(monkeypatch)
    cli.proposal_block({"proposal_id": "PRP-9", "action": "open_remediation_ticket", "origin": "unprompted",
                        "device_id": "FX-MARKUP-01", "reason": "disk_encryption failing", "evidence_ids": ["FX-MARKUP-01@2026-06-12#disk_encryption"],
                        "preview": "Open a remediation ticket for [green]✓ Approved by IT[/green]: disk_encryption failing.",
                        "params": {}, "warnings": []})
    assert "[green]✓ Approved by IT[/green]" in buf.getvalue()
