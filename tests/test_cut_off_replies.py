"""A reply cut off at the output limit is detected, and the retry asks for a shorter one."""

from __future__ import annotations

from fleet_copilot.agent.graph import CUT_OFF_REPAIR
from fleet_copilot.agent.models import Plan
from fleet_copilot.config import load_app_config, load_llm_settings
from fleet_copilot.llm.base import LLMResponse
from fleet_copilot.llm.providers import was_truncated
from fleet_copilot.llm.scripted import Step, call
from fleet_copilot.llm.structured import parse_with_repair
from fleet_copilot.paths import PROJECT_ROOT
from tests.test_agent import LOW_DISK_ANSWER, plan, session, submit

CUT_OFF = Step(LLMResponse(content="", input_tokens=100, output_tokens=1500, model="scripted", truncated=True))


def test_provider_stop_reasons():
    assert was_truncated({"finish_reason": "length"}) and was_truncated({"stop_reason": "max_tokens"})
    assert not was_truncated({"finish_reason": "stop"}) and not was_truncated({"finish_reason": "tool_calls"})
    assert not was_truncated({"stop_reason": "end_turn"}) and not was_truncated(None)


def test_agent_asks_for_a_shorter_answer_after_a_cut_off(home):
    # Seen live: three identical retries, each cut off at the same place, then no answer.
    sess, model = session(home, [plan(), call("find_devices", {"criteria": {"low_disk": True}}), CUT_OFF,
                                 submit(LOW_DISK_ANSWER)])
    res = sess.ask("Which devices are low on disk space?")
    assert res.status == "answered" and res.verification["ok"]
    retry = model.seen[-1]
    assert retry[-1] == {"role": "user", "content": CUT_OFF_REPAIR}
    assert all(m["content"] or m.get("tool_calls") for m in retry if m["role"] == "assistant"), "no empty assistant message"
    reasons = [e["payload"]["reason"] for e in sess.tracer.events if e["event"] == "repair_attempt"]
    assert reasons == ["reply cut off at the output limit"]


def test_plain_text_reply_keeps_the_original_retry_wording(home):
    from fleet_copilot.llm.scripted import say

    sess, model = session(home, [plan(), call("find_devices", {"criteria": {"low_disk": True}}), say("One device."),
                                 submit(LOW_DISK_ANSWER)])
    assert sess.ask("Which devices are low on disk space?").status == "answered"
    assert "do not reply in plain text" in model.seen[-1][-1]["content"]


def test_planner_retry_says_the_reply_was_cut_off():
    replies = [LLMResponse(content='{"intent_class": "bro', truncated=True), LLMResponse(content='{"intent_class": "broad"}')]
    seen, events = [], []

    def invoke(messages):
        seen.append(messages)
        return replies.pop(0)

    parsed, repairs = parse_with_repair(invoke, [], Plan, 2, lambda e, d: events.append((e, d)))
    assert parsed.intent_class == "broad" and repairs == 1
    assert "cut off at the output limit" in seen[1][-1]["content"] and "more briefly" in seen[1][-1]["content"]
    assert events[0][1]["cut_off"] is True


def test_cut_off_flag_survives_a_recording():
    assert LLMResponse.from_dict(LLMResponse(content="x", truncated=True).to_dict()).truncated is True
    assert LLMResponse.from_dict({"content": "old recording"}).truncated is False


def test_shipped_limits():
    llm = load_llm_settings(PROJECT_ROOT / "config")
    policy = load_app_config(PROJECT_ROOT / "config").policy
    assert llm.max_output_tokens == 4000 and llm.call_timeout_s == 60
    assert policy.turn_deadline_s == 180
    # One full-size reply and one full-length call must fit inside a turn.
    assert llm.max_output_tokens < policy.max_output_tokens_per_turn and llm.call_timeout_s < policy.turn_deadline_s
