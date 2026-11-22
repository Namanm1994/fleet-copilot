"""Scripted model for tests: returns or raises a predefined sequence of steps."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fleet_copilot.llm.base import FakeClock, LLMResponse, Message, ToolCall


@dataclass
class Step:
    response: LLMResponse | None = None
    raise_: BaseException | None = None
    elapsed_s: float = 0.5


def say(text: str, tokens: tuple[int, int] = (100, 50)) -> Step:
    return Step(LLMResponse(content=text, input_tokens=tokens[0], output_tokens=tokens[1], model="scripted"))


def call(name: str, args: dict[str, Any], call_id: str | None = None, tokens: tuple[int, int] = (100, 30)) -> Step:
    return Step(LLMResponse(tool_calls=[ToolCall(call_id or f"call-{name}", name, args)],
                            input_tokens=tokens[0], output_tokens=tokens[1], model="scripted"))


def calls(*items: tuple[str, dict[str, Any]]) -> Step:
    return Step(LLMResponse(tool_calls=[ToolCall(f"call-{i}-{n}", n, a) for i, (n, a) in enumerate(items)],
                            input_tokens=100, output_tokens=30, model="scripted"))


@dataclass
class ScriptedModel:
    steps: list[Step | Callable[[list[Message]], Step]]
    clock: FakeClock | None = None
    model: str = "scripted"
    seen: list[list[Message]] = field(default_factory=list)
    timeouts: list[float] = field(default_factory=list)

    def invoke(self, messages: list[Message], tools: list[dict[str, Any]] | None, timeout_s: float) -> LLMResponse:
        self.seen.append([dict(m) for m in messages])
        self.timeouts.append(timeout_s)
        if not self.steps:
            raise AssertionError("ScriptedModel ran out of steps")
        step = self.steps.pop(0)
        if callable(step):
            step = step(messages)
        if self.clock is not None:
            self.clock.advance(step.elapsed_s)
        if step.raise_ is not None:
            raise step.raise_
        assert step.response is not None
        return step.response
