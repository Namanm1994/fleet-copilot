"""LLM client: retries and backoff (TC-44), turn deadline and Retry-After (TC-53),
repair retries (TC-52), budgets (TC-42), recorded mode, redaction."""

from __future__ import annotations

import random

import pytest
from pydantic import BaseModel

from fleet_copilot.config import LLMSettings
from fleet_copilot.llm.base import BudgetExceeded, DeadlineExceeded, FakeClock, LLMResponse, TurnBudget
from fleet_copilot.llm.recorded import CassetteMiss, CassetteModel
from fleet_copilot.llm.retry import LLMCallError, RetryingModel, is_retryable
from fleet_copilot.llm.scripted import ScriptedModel, Step, say
from fleet_copilot.llm.structured import RepairFailed, parse_with_repair
from fleet_copilot.redact import redact

SETTINGS = LLMSettings(provider="anthropic", model="m", base_url=None, api_key_env="X", temperature=0,
                       max_output_tokens=100, call_timeout_s=30, network_retries=3, backoff_base_s=1.0, repair_retries=2)


class HTTPError(Exception):
    def __init__(self, status: int, headers: dict | None = None):
        super().__init__(f"HTTP {status}")
        self.status_code = status
        self.response = type("R", (), {"headers": headers or {}, "status_code": status})()


def budget(clock, deadline=90, tin=60000, tout=3000):
    return TurnBudget(tin, tout, deadline, clock)


def make(steps, clock):
    events = []
    model = RetryingModel(ScriptedModel(steps, clock=clock), SETTINGS, clock=clock,
                          on_event=lambda e, d: events.append((e, d)), rng=random.Random(0))
    return model, events


def test_retry_classification():
    assert is_retryable(HTTPError(429)) and is_retryable(HTTPError(503)) and is_retryable(TimeoutError())
    assert not is_retryable(HTTPError(400)) and not is_retryable(ValueError("bad"))


def test_429_four_times_then_error(tmp_path):  # TC-44
    clock = FakeClock()
    model, events = make([Step(raise_=HTTPError(429), elapsed_s=0.1) for _ in range(4)], clock)
    with pytest.raises(LLMCallError) as err:
        model.invoke([{"role": "user", "content": "hi"}], None, budget(clock))
    assert err.value.attempts == 4
    waits = [d["wait_s"] for e, d in events if e == "llm_retry"]
    assert len(waits) == 3 and 1 <= waits[0] < 1.3 and 2 <= waits[1] < 2.6 and 4 <= waits[2] < 5.1


def test_400_not_retried():
    clock = FakeClock()
    model, events = make([Step(raise_=HTTPError(400))], clock)
    with pytest.raises(LLMCallError) as err:
        model.invoke([], None, budget(clock))
    assert err.value.attempts == 1 and not events


def test_deadline_wins_over_retries():  # TC-53 (a)
    clock = FakeClock()
    steps = [Step(raise_=TimeoutError(), elapsed_s=30) for _ in range(4)]
    model, events = make(steps, clock)
    with pytest.raises(DeadlineExceeded):
        model.invoke([], None, budget(clock))
    assert [e for e, _ in events] == ["llm_retry", "deadline_reached"]
    assert 60 < clock.now() < 64  # call 1 (0-30), wait ~1, retry 1 (31-61); retry 2 not started


def test_retry_after_honored():  # TC-53 (b)
    clock = FakeClock()
    model, events = make([Step(raise_=HTTPError(429, {"retry-after": "5"})), say("ok")], clock)
    resp = model.invoke([], None, budget(clock))
    assert resp.content == "ok" and events[0][1]["wait_s"] == 5.0


def test_first_call_timeout_shrinks_near_deadline():
    clock = FakeClock()
    inner = ScriptedModel([say("ok")], clock=clock)
    model = RetryingModel(inner, SETTINGS, clock=clock)
    b = budget(clock)
    clock.advance(80)
    model.invoke([], None, b)
    assert inner.timeouts[0] == pytest.approx(10, abs=0.01)


def test_token_budget():  # TC-42
    clock = FakeClock()
    model, _ = make([Step(LLMResponse(content="x", input_tokens=2500, output_tokens=10)), say("y")], clock)
    b = budget(clock, tin=2000)
    model.invoke([], None, b)
    with pytest.raises(BudgetExceeded) as err:
        model.invoke([], None, b)
    assert err.value.kind == "input_tokens"


class Plan(BaseModel):
    intent: str
    steps: list[str]


def test_repair_twice_then_valid():  # TC-52 (a)
    scripted = ScriptedModel([say("not json"), say('{"intent": "x"}'), say('{"intent": "x", "steps": ["a"]}')])
    events = []
    plan, repairs = parse_with_repair(lambda m: scripted.invoke(m, None, 30), [{"role": "user", "content": "q"}],
                                      Plan, 2, lambda e, d: events.append(d))
    assert plan.steps == ["a"] and repairs == 2
    assert "steps: Field required" in scripted.seen[2][-1]["content"]
    assert len(events) == 2


def test_repair_gives_up():  # TC-52 (b)
    scripted = ScriptedModel([say("nope") for _ in range(3)])
    with pytest.raises(RepairFailed):
        parse_with_repair(lambda m: scripted.invoke(m, None, 30), [], Plan, 2)
    assert not scripted.steps


def test_cassette_record_and_replay(tmp_path):
    path = tmp_path / "case.json"
    live = ScriptedModel([say("hello"), say("again")])
    rec = CassetteModel(path, "record", live)
    msgs = [{"role": "user", "content": "hi"}]
    assert rec.invoke(msgs, None, 30).content == "hello"
    assert rec.invoke(msgs, None, 30).content == "again"
    rec.save()
    rep = CassetteModel(path, "replay")
    assert rep.invoke(msgs, None, 30).content == "hello"
    assert rep.invoke(msgs, None, 30).content == "again"
    with pytest.raises(CassetteMiss):
        CassetteModel(path, "replay").invoke([{"role": "user", "content": "other"}], None, 30)


def test_redaction(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secretvalue1234567890")
    text = "auth failed for sk-ant-secretvalue1234567890; api_key=abcdef1234567890xyz"
    out = redact(text)
    assert "secretvalue" not in out and "abcdef1234567890xyz" not in out and "[REDACTED]" in out


def test_a_failed_call_does_not_use_up_a_recording_number(tmp_path):
    """Seen live (F02): a call timed out and was retried while recording; the retry was saved as request 2,
    but a replay has no failed call and asks for request 1, so the case reported "cassette out of date"."""
    path = tmp_path / "c.json"
    live = ScriptedModel([say("one"), Step(raise_=HTTPError(503)), say("two")])
    rec = CassetteModel(path, "record", live)
    first, second = [{"role": "user", "content": "a"}], [{"role": "user", "content": "b"}]
    assert rec.invoke(first, None, 30).content == "one"
    with pytest.raises(HTTPError):
        rec.invoke(second, None, 30)
    assert rec.invoke(second, None, 30).content == "two"
    rec.save()
    rep = CassetteModel(path, "replay")
    assert [rep.invoke(first, None, 30).content, rep.invoke(second, None, 30).content] == ["one", "two"]
