"""Structured output with repair retries (G35).

The model is asked for JSON. If it doesn't validate, it is re-asked with the exact
validation error, up to `repair_retries` times. Separate from network retries; every
attempt counts toward the turn budget. Text after the first complete JSON value is ignored
and traced (`trailing_text_ignored`), because re-asking for it costs a call and gains nothing.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from fleet_copilot.llm.base import LLMResponse, Message

T = TypeVar("T", bound=BaseModel)


class RepairFailed(RuntimeError):
    def __init__(self, message: str, attempts: int):
        super().__init__(message)
        self.attempts = attempts


def extract_json(text: str) -> tuple[Any, str]:
    """Returns (first JSON value in the text, whatever followed it).

    Models sometimes add text after a complete object: a stray brace, a closing remark or the
    same object again. The first complete value is used and the rest is handed back, so the
    caller can record that something was ignored. Text that is cut off still fails.
    """
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start > 0:
        text = text[start:]
    value, end = json.JSONDecoder().raw_decode(text)
    return value, text[end:].strip()


def validation_message(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = [f"{'.'.join(str(x) for x in e['loc']) or '(root)'}: {e['msg']}" for e in exc.errors()[:5]]
        return "; ".join(parts)
    return str(exc)


def parse_with_repair(invoke: Callable[[list[Message]], LLMResponse], messages: list[Message], model_cls: type[T],
                      max_repairs: int, on_event: Callable[[str, dict[str, Any]], None] | None = None) -> tuple[T, int]:
    """Returns (parsed object, number of repair attempts used)."""
    on_event = on_event or (lambda *_: None)
    convo = list(messages)
    last_error = ""
    for attempt in range(max_repairs + 1):
        resp = invoke(convo)
        try:
            value, extra = extract_json(resp.content)
            parsed = model_cls.model_validate(value)
            if extra:
                on_event("trailing_text_ignored", {"schema": model_cls.__name__, "chars": len(extra), "text": extra[:80]})
            return parsed, attempt
        except (ValueError, ValidationError) as exc:
            last_error = validation_message(exc)
            if attempt == max_repairs:
                break
            on_event("repair_attempt", {"schema": model_cls.__name__, "attempt": attempt + 1, "error": last_error,
                                        "cut_off": resp.truncated})
            problem = ("Your previous output was cut off at the output limit. Reply again, more briefly, "
                       if resp.truncated else f"Your previous output failed validation: {last_error}. Reply ")
            convo = convo + [
                {"role": "assistant", "content": resp.content},
                {"role": "user", "content": f"{problem}with only a JSON object that matches the {model_cls.__name__} schema."},
            ]
    raise RepairFailed(f"{model_cls.__name__} still invalid after {max_repairs} repairs: {last_error}", max_repairs)
