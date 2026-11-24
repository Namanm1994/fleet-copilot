"""Reliability outside the LLM path: tool errors, oversized output, locks, usage caps."""

from __future__ import annotations

import json
import os
import sqlite3
import time

import pytest
from click.testing import CliRunner

from fleet_copilot import cli
from fleet_copilot.audit import STALE_LOCK_S, file_lock
from fleet_copilot.llm.base import LLMResponse
from fleet_copilot.llm.scripted import ScriptedModel, Step, call
from fleet_copilot.tools.envelope import ToolResult
from tests.test_agent import plan, session, submit
from tests.test_tools import make_tk


# ---- unexpected tool errors -----------------------------------------------------------------------
def test_unexpected_tool_error_becomes_a_typed_error(home):
    tk = make_tk(home)

    def boom(_args):
        raise sqlite3.OperationalError("database is locked")

    tk.read.get_device = boom
    res = tk.call("get_device", {"device_id": "FX-DELL-01"})
    assert not res.ok and res.error.code == "INTERNAL_ERROR"
    assert "locked" not in res.to_llm()  # internals are not shown to the model
    events = [e for e in tk.audit.entries("fixture-999") if e["event"] == "tool_error"]
    assert events and "OperationalError" in events[0]["detail"]["error"]


def test_turn_survives_a_failing_tool(home):
    steps = [plan(), call("get_device", {"device_id": "FX-DELL-01"}),
             submit({"summary": "The lookup failed, so I can't answer this.", "claims": [], "definitions_used": [],
                     "caveats": [], "answer_type": "insufficient_data"})]
    sess, model = session(home, steps)

    def boom(_args):
        raise RuntimeError("boom")

    sess.tk.read.get_device = boom
    res = sess.ask("show FX-DELL-01")
    assert res.status == "answered" and "lookup failed" in res.text
    assert any(m["role"] == "tool" and "INTERNAL_ERROR" in m["content"] for m in model.seen[-1])


def test_failed_turn_does_not_end_the_chat(home, monkeypatch):
    from fleet_copilot.agent.session import CopilotSession

    model = ScriptedModel([plan(in_scope=False, refusal_reason="I can't help with weather.")])
    real_ask, calls = CopilotSession.ask, []

    def flaky_ask(self, question):
        calls.append(question)
        if len(calls) == 1:
            raise RuntimeError("unexpected failure inside the turn")
        return real_ask(self, question)

    monkeypatch.setattr(CopilotSession, "ask", flaky_ask)
    monkeypatch.setattr(cli, "POLICY_FILE", "policy.eval.yaml")
    monkeypatch.setenv("FLEET_HOME", str(home.root))
    monkeypatch.setattr("fleet_copilot.llm.providers.build_model", lambda settings: model)
    r = CliRunner().invoke(cli.main, ["chat", "--tenant", "fixture-999"], input="first\nWhat's the weather?\nexit\n",
                           catch_exceptions=False)
    assert r.exit_code == 0, r.output
    assert "This turn failed (RuntimeError)" in r.output and "I can't help with weather." in r.output


# ---- oversized tool output ------------------------------------------------------------------------
def test_oversized_output_is_shortened_to_valid_json():
    rows = [{"device_id": f"DEV-{i:04d}", "hostname": f"host-{i}", "note": "x" * 40} for i in range(500)]
    res = ToolResult(data={"devices": rows}, evidence_ids=[f"DEV-{i:04d}@2026-06-12" for i in range(500)], total_count=500)
    text = res.to_llm(3000)
    assert len(text) < 3200
    payload = json.loads(text.removeprefix("<tool_data>\n").removesuffix("\n</tool_data>"))
    assert payload["truncated"] is True and payload["total_count"] == 500
    assert 0 < len(payload["data"]["devices"]) < 500
    assert any("of 500 items shown" in c for c in payload["caveats"])
    small = ToolResult(data={"devices": rows[:2]})
    assert small.to_llm(3000) == small.to_llm()


def test_one_enormous_item_still_gives_valid_json():
    text = ToolResult(data={"blob": "y" * 50_000}).to_llm(2000)
    payload = json.loads(text.removeprefix("<tool_data>\n").removesuffix("\n</tool_data>"))
    assert payload["data"] is None and payload["truncated"] is True


# ---- locks ------------------------------------------------------------------------------------------
def test_stale_lock_is_removed(tmp_path):
    target = tmp_path / "audit.jsonl"
    lock = tmp_path / "audit.jsonl.lock"
    lock.write_text("", encoding="utf-8")
    old = time.time() - STALE_LOCK_S - 5
    os.utime(lock, (old, old))  # left behind by a process that died
    with file_lock(target, timeout_s=0.5):
        assert lock.exists()
    assert not lock.exists()


def test_live_lock_is_respected(tmp_path):
    target = tmp_path / "audit.jsonl"
    (tmp_path / "audit.jsonl.lock").write_text("", encoding="utf-8")
    with pytest.raises(TimeoutError), file_lock(target, timeout_s=0.2):
        pass


# ---- usage caps -------------------------------------------------------------------------------------
def _with_caps(home, session_cap, day_cap):
    p = home.config / "policy.yaml"
    text = p.read_text(encoding="utf-8")
    text = text.replace("max_input_tokens_per_session: 500000", f"max_input_tokens_per_session: {session_cap}")
    text = text.replace("max_input_tokens_per_day: 2000000", f"max_input_tokens_per_day: {day_cap}")
    p.write_text(text, encoding="utf-8")


def test_session_cap_refuses_the_next_turn(home):
    _with_caps(home, 100, 0)
    refuse = Step(LLMResponse(content=json.dumps({"in_scope": False, "refusal_reason": "Out of scope.", "steps": []}),
                              input_tokens=150, output_tokens=5))
    sess, model = session(home, [refuse])
    assert sess.ask("first").status == "refused"
    res = sess.ask("second")
    assert res.status == "refused" and "usage cap" in res.text and len(model.seen) == 1  # no LLM call was made
    assert any(e["event"] == "budget_refused" for e in sess.tk.audit.entries("fixture-999"))


def test_daily_cap_is_per_company_and_persists(home):
    _with_caps(home, 0, 1000)
    sess, model = session(home, [])
    sess.tk.store.add_usage(sess._today(), 1500, 10)  # used earlier today, in another session
    res = sess.ask("anything")
    assert res.status == "refused" and "today's usage cap" in res.text and not model.seen
    other = make_tk(home, "fixture-998")
    assert other.store.usage(sess._today())["input_tokens"] == 0  # another company is unaffected


def test_usage_is_recorded_per_turn(home):
    step = Step(LLMResponse(content=json.dumps({"in_scope": False, "refusal_reason": "No.", "steps": []}),
                            input_tokens=120, output_tokens=7))
    sess, _ = session(home, [step])
    sess.ask("first")
    assert sess.tk.store.usage(sess._today()) == {"input_tokens": 120, "output_tokens": 7, "turns": 1}
    assert sess.session_input_tokens == 120


def test_default_caps_are_configured(app_config):
    assert app_config.policy.max_input_tokens_per_session == 500_000
    assert app_config.policy.max_input_tokens_per_day == 2_000_000
