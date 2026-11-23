"""Provider-neutral message and response types, clock, and turn budget."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol

# Message format used everywhere in the agent:
#   {"role": "system"|"user"|"assistant"|"tool", "content": str,
#    "tool_calls": [{"id", "name", "args"}] (assistant), "tool_call_id": str (tool), "name": str (tool)}
Message = dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""

    def to_message(self) -> Message:
        msg: Message = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            msg["tool_calls"] = [{"id": t.id, "name": t.name, "args": t.args} for t in self.tool_calls]
        return msg

    def to_dict(self) -> dict[str, Any]:
        return {"content": self.content, "tool_calls": [t.__dict__ for t in self.tool_calls],
                "input_tokens": self.input_tokens, "output_tokens": self.output_tokens, "model": self.model}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> LLMResponse:
        return LLMResponse(d.get("content", ""), [ToolCall(**t) for t in d.get("tool_calls", [])],
                           d.get("input_tokens", 0), d.get("output_tokens", 0), d.get("model", ""))


class ChatModel(Protocol):
    model: str

    def invoke(self, messages: list[Message], tools: list[dict[str, Any]] | None, timeout_s: float,
               effort: str | None = None) -> LLMResponse: ...


class Clock:
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds))


class FakeClock(Clock):
    def __init__(self, start: float = 0.0):
        self.t = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += max(0.0, seconds)

    def advance(self, seconds: float) -> None:
        self.t += seconds


class BudgetExceeded(RuntimeError):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


class DeadlineExceeded(BudgetExceeded):
    def __init__(self, message: str):
        super().__init__("time", message)


@dataclass
class TurnBudget:
    max_input_tokens: int
    max_output_tokens: int
    deadline_s: float
    clock: Clock
    started: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    def __post_init__(self) -> None:
        self.started = self.clock.now()

    @property
    def deadline(self) -> float:
        return self.started + self.deadline_s

    def remaining(self) -> float:
        return self.deadline - self.clock.now()

    def elapsed(self) -> float:
        return self.clock.now() - self.started

    def charge(self, response: LLMResponse) -> None:
        self.calls += 1
        self.input_tokens += response.input_tokens
        self.output_tokens += response.output_tokens

    def check(self) -> None:
        if self.input_tokens > self.max_input_tokens:
            raise BudgetExceeded("input_tokens", f"Input-token budget exceeded ({self.input_tokens} > {self.max_input_tokens}).")
        if self.output_tokens > self.max_output_tokens:
            raise BudgetExceeded("output_tokens", f"Output-token budget exceeded ({self.output_tokens} > {self.max_output_tokens}).")
        if self.remaining() <= 0:
            raise DeadlineExceeded(f"Turn deadline of {self.deadline_s:.0f} s reached.")


def cost_usd(model: str, input_tokens: int, output_tokens: int, pricing: Any) -> float | None:
    price = (pricing or {}).get(model)
    if not price:
        return None
    return round(input_tokens / 1e6 * float(price["input"]) + output_tokens / 1e6 * float(price["output"]), 6)
