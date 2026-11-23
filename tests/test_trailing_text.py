"""Text after a complete JSON value is ignored and traced, not re-asked for."""

from __future__ import annotations

import json

import pytest

from fleet_copilot.agent.models import Plan
from fleet_copilot.llm.base import LLMResponse
from fleet_copilot.llm.recorded import CassetteModel
from fleet_copilot.llm.structured import RepairFailed, extract_json, parse_with_repair

PLAN = '{"intent_class":"compliance","in_scope":true,"steps":[{"tool":"resolve_entities","purpose":"map"}]}'


def run(*replies):
    queue, events = list(replies), []
    plan, repairs = parse_with_repair(lambda m: LLMResponse(content=queue.pop(0)), [], Plan, 2,
                                      lambda e, d: events.append((e, d)))
    return plan, repairs, events


@pytest.mark.parametrize("extra", ["\n}", "\n\n" + PLAN, "\nLet me know if you need anything else."])
def test_first_complete_object_is_used(extra):
    # "\n}" and the repeated plan are the two replies seen in live runs.
    plan, repairs, events = run(PLAN + extra)
    assert repairs == 0 and plan.steps[0].tool == "resolve_entities"
    assert [e for e, _ in events] == ["trailing_text_ignored"]
    assert events[0][1]["chars"] == len(extra.strip())


def test_clean_reply_records_nothing():
    plan, repairs, events = run(PLAN)
    assert repairs == 0 and events == []


def test_fenced_and_prefixed_replies_still_parse():
    assert extract_json("Here is the plan:\n```json\n" + PLAN + "\n```") == (json.loads(PLAN), "")


def test_cut_off_reply_is_still_repaired():
    plan, repairs, events = run(PLAN[:60], PLAN)
    assert repairs == 1 and events[0][0] == "repair_attempt"


def test_no_json_at_all_still_fails():
    with pytest.raises(RepairFailed):
        run("I think the answer is May.", "Still prose.", "And again.")


def test_new_recording_drops_entries_from_an_earlier_one(tmp_path):
    class Live:
        model = "live"

        def invoke(self, messages, tools, timeout_s, effort=None):
            return LLMResponse(content="new")

    path = tmp_path / "run-1.json"
    path.write_text(json.dumps({"model": "old", "entries": {"000-stale": {"content": "old"}}}), encoding="utf-8")
    rec = CassetteModel(path, "record", Live())
    rec.invoke([{"role": "user", "content": "hi"}], None, 5)
    rec.save()
    entries = json.loads(path.read_text(encoding="utf-8"))["entries"]
    assert len(entries) == 1 and "000-stale" not in entries
    assert CassetteModel(path, "replay").invoke([{"role": "user", "content": "hi"}], None, 5).content == "new"
