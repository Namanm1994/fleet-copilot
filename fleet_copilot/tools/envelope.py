"""Common response envelope for every tool, and typed errors the agent can act on."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

ERROR_CODES = (
    "NOT_FOUND", "AMBIGUOUS", "INVALID_ARGUMENT", "INSUFFICIENT_DATA", "PRECONDITION_FAILED", "DUPLICATE",
    "REJECTED_THIS_SESSION", "AUTO_PROPOSAL_CAP", "BATCH_LIMIT", "FORBIDDEN_ACTION", "BUDGET_EXCEEDED",
    "DUPLICATE_CALL", "INTERNAL_ERROR",
)


class ToolErrorInfo(BaseModel):
    code: str
    message: str
    hint: str | None = None


class ToolResult(BaseModel):
    ok: bool = True
    data: Any = None
    total_count: int | None = None
    truncated: bool = False
    evidence_ids: list[str] = Field(default_factory=list)
    receipt_id: str | None = None
    as_of: str | None = None
    definitions_used: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    error: ToolErrorInfo | None = None

    def to_llm(self, max_chars: int | None = None) -> str:
        """Serialized for the model, wrapped as data (never instructions).

        With max_chars, an oversized result is shortened by dropping list items, never by cutting
        text, so the model always receives valid JSON and is told what was left out."""
        payload = self.model_dump(exclude_none=True)
        text = json.dumps(payload, ensure_ascii=False, default=str)
        if max_chars is not None and len(text) > max_chars:
            payload = shorten_payload(payload, max_chars)
            text = json.dumps(payload, ensure_ascii=False, default=str)
        return "<tool_data>\n" + text + "\n</tool_data>"


def _lists(node: Any, path: str = "") -> list[tuple[str, list]]:
    out: list[tuple[str, list]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            out += _lists(v, f"{path}.{k}" if path else str(k))
    elif isinstance(node, list):
        out.append((path, node))
        for i, v in enumerate(node):
            out += _lists(v, f"{path}[{i}]")
    return out


def shorten_payload(payload: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """Halve the largest list until the payload fits. Records how many items each list kept."""
    payload = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
    original: dict[str, int] = {}

    def size(obj: Any) -> int:
        return len(json.dumps(obj, ensure_ascii=False))

    for _ in range(64):
        if size(payload) <= max_chars:
            break
        candidates = [(size(lst), path, lst) for path, lst in _lists(payload) if len(lst) > 1]
        if not candidates:
            break
        _, path, lst = max(candidates, key=lambda c: c[0])
        original.setdefault(path, len(lst))
        del lst[max(1, len(lst) // 2):]
    kept = {path: len(lst) for path, lst in _lists(payload) if path in original}
    notes = [f"{path}: {kept.get(path, 0)} of {n} items shown" for path, n in original.items() if path in kept]
    payload["truncated"] = True
    payload.setdefault("caveats", []).append(
        "Output shortened to fit (" + "; ".join(notes) + "). Narrow the query to see the rest.")
    if size(payload) > max_chars:  # one enormous item: return the envelope without the data
        payload["data"] = None
        payload["evidence_ids"] = payload.get("evidence_ids", [])[:20]
        payload["caveats"] = ["Result too large to show. Narrow the query."]
    return payload


class ToolError(Exception):
    def __init__(self, code: str, message: str, hint: str | None = None):
        assert code in ERROR_CODES, code
        super().__init__(message)
        self.code, self.message, self.hint = code, message, hint

    def result(self, as_of: str | None = None) -> ToolResult:
        return ToolResult(ok=False, as_of=as_of, error=ToolErrorInfo(code=self.code, message=self.message, hint=self.hint))
