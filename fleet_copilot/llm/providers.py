"""Provider adapters (Anthropic, OpenAI, OpenRouter) behind one ChatModel interface.

Imports are lazy so only the provider in use needs its package installed. Provider
SDK retries are disabled: retries are handled by RetryingModel so the turn deadline wins.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

from fleet_copilot.config import ConfigError, LLMSettings
from fleet_copilot.llm.base import LLMResponse, Message, ToolCall


def _to_langchain(messages: list[Message]):
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    out = []
    for m in messages:
        role = m["role"]
        if role == "system":
            out.append(SystemMessage(m["content"]))
        elif role == "user":
            out.append(HumanMessage(m["content"]))
        elif role == "assistant":
            calls = [{"id": t["id"], "name": t["name"], "args": t["args"], "type": "tool_call"} for t in m.get("tool_calls", [])]
            out.append(AIMessage(content=m.get("content") or "", tool_calls=calls))
        elif role == "tool":
            out.append(ToolMessage(content=m["content"], tool_call_id=m["tool_call_id"], name=m.get("name")))
        else:
            raise ValueError(f"Unknown role {role}")
    return out


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text")
    return str(content or "")


def reasoning_kwargs(provider: str, effort: str | None) -> dict[str, Any]:
    """Client settings that ask for a reasoning effort. Empty when none is asked for.

    OpenRouter takes one `reasoning` object for every model and leaves it out for models that
    do not reason. OpenAI takes `reasoning_effort`. Anthropic models reason only when asked,
    and we never ask, so nothing is sent.
    """
    if not effort:
        return {}
    if provider == "openrouter":
        return {"extra_body": {"reasoning": {"effort": effort}}}
    if provider == "openai":
        return {"reasoning_effort": effort}
    return {}


class LangChainModel:
    def __init__(self, settings: LLMSettings):
        if not settings.model:
            raise ConfigError(f"No model set for provider '{settings.provider}'. Set FLEET_LLM_MODEL or config/llm.yaml.")
        key = os.environ.get(settings.api_key_env, "")
        if not key:
            raise ConfigError(f"{settings.api_key_env} is not set. Put it in .env (live mode only; recorded mode needs no key).")
        self.settings = settings
        self.model = settings.model
        self._key = key
        self._clients: dict[tuple[float, str], Any] = {}

    def _client(self, timeout_s: float, effort: str | None = None):
        timeout_s = round(max(1.0, timeout_s), 1)
        cache_key = (timeout_s, effort or "")
        if cache_key in self._clients:
            return self._clients[cache_key]
        s = self.settings
        if s.provider == "anthropic":
            from langchain_anthropic import ChatAnthropic

            client = ChatAnthropic(model=s.model, temperature=s.temperature, max_tokens=s.max_output_tokens,
                                   timeout=timeout_s, max_retries=0, api_key=self._key)
        else:
            from langchain_openai import ChatOpenAI

            kwargs: dict[str, Any] = {"model": s.model, "temperature": s.temperature, "max_tokens": s.max_output_tokens,
                                      "timeout": timeout_s, "max_retries": 0, "api_key": self._key}
            if s.provider == "openrouter":
                kwargs["base_url"] = s.base_url
            kwargs.update(reasoning_kwargs(s.provider, effort))
            client = ChatOpenAI(**kwargs)
        self._clients[cache_key] = client
        return client

    def invoke(self, messages: list[Message], tools: list[dict[str, Any]] | None, timeout_s: float,
               effort: str | None = None) -> LLMResponse:
        client = self._client(timeout_s, effort)
        if tools:
            client = client.bind_tools([{"type": "function", "function": t} for t in tools])
        msg = client.invoke(_to_langchain(messages))
        calls = []
        for tc in getattr(msg, "tool_calls", None) or []:
            args = tc.get("args") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_unparseable": args}
            calls.append(ToolCall(id=tc.get("id") or f"call_{uuid.uuid4().hex[:8]}", name=tc["name"], args=args))
        usage = getattr(msg, "usage_metadata", None) or {}
        return LLMResponse(content=_text(msg.content), tool_calls=calls, input_tokens=int(usage.get("input_tokens", 0)),
                           output_tokens=int(usage.get("output_tokens", 0)), model=self.model)


def build_model(settings: LLMSettings) -> LangChainModel:
    return LangChainModel(settings)
