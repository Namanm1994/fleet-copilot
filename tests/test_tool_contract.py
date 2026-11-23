"""The tool contract is pinned: tool names, input fields, output fields, envelope fields, error codes.

If this test fails you changed what the model and the evals depend on. If the change is intended:
bump TOOL_CONTRACT_VERSION in fleet_copilot/tools/outputs.py, then run
    UPDATE_TOOL_CONTRACT=1 pytest tests/test_tool_contract.py
and re-record the eval cassettes.
"""

from __future__ import annotations

import json
import os
import typing
from pathlib import Path

import pytest
from pydantic import BaseModel

from fleet_copilot.tools.envelope import ERROR_CODES, ToolResult
from fleet_copilot.tools.outputs import ACTION_OUTPUT, READ_OUTPUTS, TOOL_CONTRACT_VERSION, output_model
from fleet_copilot.tools.schemas import ACTION_ARGS, READ_ARGS
from tests.test_tools import make_tk

SNAPSHOT = Path(__file__).parent / "snapshots" / "tool_contract.json"


def _models_in(annotation) -> list[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return [m for arg in typing.get_args(annotation) for m in _models_in(arg)]


def shape(model: type[BaseModel]) -> dict:
    """Field names and whether each is required, recursively. Independent of library versions."""
    out = {}
    for name, f in model.model_fields.items():
        entry: dict = {"required": f.is_required()}
        nested = _models_in(f.annotation)
        if nested:
            entry["fields"] = shape(nested[0])
        out[f.alias or name] = entry
    return out


def current_contract() -> dict:
    tools = {}
    for name, args in {**READ_ARGS, **ACTION_ARGS}.items():
        tools[name] = {"kind": "read" if name in READ_ARGS else "action", "input": shape(args), "output": shape(output_model(name))}
    return {"version": TOOL_CONTRACT_VERSION, "error_codes": sorted(ERROR_CODES), "envelope": shape(ToolResult), "tools": tools}


def test_tool_contract_is_pinned():
    contract = current_contract()
    if os.environ.get("UPDATE_TOOL_CONTRACT") == "1":
        SNAPSHOT.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    pinned = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    if contract != pinned:
        changed = sorted(t for t in set(contract["tools"]) | set(pinned["tools"]) if contract["tools"].get(t) != pinned["tools"].get(t))
        other = [k for k in ("version", "error_codes", "envelope") if contract[k] != pinned[k]]
        pytest.fail(f"Tool contract changed (tools: {changed or 'none'}; also: {other or 'none'}). " + __doc__.split("\n\n", 1)[1])


def test_every_tool_has_typed_input_and_output():
    assert set(READ_OUTPUTS) == set(READ_ARGS)
    assert ACTION_OUTPUT.model_config.get("extra") == "forbid"
    for model in READ_OUTPUTS.values():
        assert shape(model) or issubclass(model, BaseModel)


def test_policy_tools_match_the_contract(home):
    tk = make_tk(home)
    assert set(tk.tool_names()) <= set(current_contract()["tools"])


def test_output_mismatch_fails_in_tests_and_is_audited_in_production(home):
    tk = make_tk(home)
    with pytest.raises(AssertionError, match="declared contract"):
        tk.check_output("get_device", {"device_id": "X", "surprise": 1})
    tk.strict_contract = False  # how the agent runs outside tests
    tk.check_output("get_device", {"device_id": "X", "surprise": 1})
    events = [e for e in tk.audit.entries("fixture-999") if e["event"] == "contract_violation"]
    assert events and events[0]["detail"]["tool"] == "get_device"


def test_find_devices_pages_through_every_match(home):
    tk = make_tk(home)
    seen, offset, pages = [], 0, 0
    while offset is not None:
        res = tk.call("find_devices", {"limit": 10, "offset": offset})
        seen += [d["device_id"] for d in res.data["devices"]]
        assert res.truncated is (res.next_offset is not None)
        offset, pages = res.next_offset, pages + 1
        assert pages < 10
    assert len(seen) == res.total_count > 20 and len(set(seen)) == len(seen)
    assert pages == -(-res.total_count // 10)
