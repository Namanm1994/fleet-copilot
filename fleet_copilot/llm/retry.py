"""Timeouts, retries and the turn deadline (G29, G30).

- 30 s per call (config), provider SDK retries off
- up to 3 retries with exponential backoff + jitter, only for 429, 5xx and timeouts
- `Retry-After` honored when the API sends it
- the turn deadline wins: a retry starts only if its wait plus a full call fits
"""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import Any

from fleet_copilot.config import LLMSettings
from fleet_copilot.llm.base import ChatModel, Clock, DeadlineExceeded, LLMResponse, Message, TurnBudget
from fleet_copilot.redact import redact

RETRYABLE_NAMES = ("ratelimit", "timeout", "apiconnection", "internalserver", "overloaded", "serviceunavailable",
                   "apitimeout", "readtimeout", "connecttimeout")


class LLMCallError(RuntimeError):
    def __init__(self, message: str, retryable: bool, attempts: int):
        super().__init__(redact(message))
        self.retryable = retryable
        self.attempts = attempts


def _status(exc: BaseException) -> int | None:
    for obj in (exc, getattr(exc, "response", None)):
        code = getattr(obj, "status_code", None) or getattr(obj, "status", None)
        if isinstance(code, int):
            return code
    return None


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    status = _status(exc)
    if status is not None:
        return status == 429 or status >= 500
    name = type(exc).__name__.lower()
    return any(n in name for n in RETRYABLE_NAMES)


def retry_after(exc: BaseException) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", None) or getattr(exc, "headers", None)
    if not headers:
        return None
    value = headers.get("retry-after") or headers.get("Retry-After")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class RetryingModel:
    def __init__(self, inner: ChatModel, settings: LLMSettings, clock: Clock | None = None,
                 on_event: Callable[[str, dict[str, Any]], None] | None = None, rng: random.Random | None = None):
        self.inner = inner
        self.model = getattr(inner, "model", "")
        self.s = settings
        self.clock = clock or Clock()
        self.on_event = on_event or (lambda *_: None)
        self.rng = rng or random.Random()

    def _backoff(self, attempt: int) -> float:
        base = self.s.backoff_base_s * (2 ** (attempt - 1))
        return base + self.rng.uniform(0, 0.25 * base)

    def invoke(self, messages: list[Message], tools: list[dict[str, Any]] | None, budget: TurnBudget) -> LLMResponse:
        budget.check()
        attempt = 0
        while True:
            remaining = budget.remaining()
            if attempt == 0:
                if remaining < 1.0:
                    raise DeadlineExceeded(f"Turn deadline of {budget.deadline_s:.0f} s reached.")
                timeout = min(self.s.call_timeout_s, remaining)
            else:
                timeout = self.s.call_timeout_s
            try:
                resp = self.inner.invoke(messages, tools, timeout)
                budget.charge(resp)
                return resp
            except Exception as exc:  # noqa: BLE001 - classified below
                retryable = is_retryable(exc)
                attempt += 1
                if not retryable or attempt > self.s.network_retries:
                    raise LLMCallError(f"LLM call failed: {type(exc).__name__}: {exc}", retryable, attempt) from exc
                wait = retry_after(exc)
                wait = self._backoff(attempt) if wait is None else wait
                if self.clock.now() + wait + self.s.call_timeout_s > budget.deadline:
                    self.on_event("deadline_reached", {"attempt": attempt, "wait_s": round(wait, 2)})
                    raise DeadlineExceeded("No time left for another LLM attempt before the turn deadline.") from exc
                self.on_event("llm_retry", {"attempt": attempt, "wait_s": round(wait, 2), "error": redact(f"{type(exc).__name__}: {exc}")})
                self.clock.sleep(wait)
