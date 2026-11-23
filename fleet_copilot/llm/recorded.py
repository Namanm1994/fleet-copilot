"""Recorded mode: replay saved LLM responses for offline, repeatable evals.

A cassette stores, per request digest, the model response only. Requests are keyed on
the messages and tool names (not on the key or headers, which are never stored).
The cassette also names the model and the settings it was recorded with. Prompts are not
stored here: cassettes are committed, and prompts hold company data.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from fleet_copilot.llm.base import ChatModel, LLMResponse, Message
from fleet_copilot.redact import redact


class CassetteMiss(RuntimeError):
    pass


def request_key(messages: list[Message], tools: list[dict[str, Any]] | None) -> str:
    payload = {"messages": messages, "tools": sorted(t["name"] for t in (tools or []))}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:24]


class CassetteModel:
    """mode = "replay" (offline) or "record" (call the inner model and save)."""

    def __init__(self, path: Path, mode: str, inner: ChatModel | None = None, model_name: str = "recorded"):
        if mode not in ("replay", "record"):
            raise ValueError(mode)
        if mode == "record" and inner is None:
            raise ValueError("record mode needs a live model")
        self.path, self.mode, self.inner = path, mode, inner
        self.model = getattr(inner, "model", model_name)
        self.entries: dict[str, Any] = {}
        self.recorded_settings: dict[str, Any] | None = None  # what the recording was made with
        if mode == "replay" and path.exists():  # a new recording starts empty, so no stale entries survive
            data = json.loads(path.read_text(encoding="utf-8"))
            self.entries = data.get("entries", {})
            self.recorded_settings = data.get("settings")
            self.model = data.get("model") or self.model
        self._seq = 0

    def invoke(self, messages: list[Message], tools: list[dict[str, Any]] | None, timeout_s: float,
               effort: str | None = None) -> LLMResponse:
        key = f"{self._seq:03d}-{request_key(messages, tools)}"
        if self.mode == "replay":
            if key not in self.entries:
                raise CassetteMiss(f"No recorded response for request {key} in {self.path.name}. Re-record with --mode live --record.")
            self._seq += 1
            return LLMResponse.from_dict(self.entries[key])
        # Numbered only once a reply arrives. A call that fails (timeout, rate limit) and is retried must not
        # use up a number: the replay has no failed call, so its numbering would no longer match.
        resp = self.inner.invoke(messages, tools, timeout_s, effort)  # type: ignore[union-attr]
        self._seq += 1
        data = resp.to_dict()
        data["content"] = redact(data["content"])
        self.entries[key] = data
        return resp

    def note_settings(self, settings: dict[str, Any]) -> None:
        if self.mode == "record":
            self.recorded_settings = settings

    def save(self) -> None:
        if self.mode != "record":
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"model": self.model, "settings": self.recorded_settings, "entries": self.entries},
                                        indent=1, ensure_ascii=False),
                             encoding="utf-8")
