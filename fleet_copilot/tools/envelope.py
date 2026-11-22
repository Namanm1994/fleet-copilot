"""Common response envelope for every tool, and typed errors the agent can act on."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

ERROR_CODES = (
    "NOT_FOUND", "AMBIGUOUS", "INVALID_ARGUMENT", "INSUFFICIENT_DATA", "PRECONDITION_FAILED", "DUPLICATE",
    "REJECTED_THIS_SESSION", "AUTO_PROPOSAL_CAP", "BATCH_LIMIT", "FORBIDDEN_ACTION", "BUDGET_EXCEEDED",
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

    def to_llm(self) -> str:
        """Serialized for the model, wrapped as data (never instructions)."""
        payload = self.model_dump(exclude_none=True)
        return "<tool_data>\n" + json.dumps(payload, ensure_ascii=False, default=str) + "\n</tool_data>"


class ToolError(Exception):
    def __init__(self, code: str, message: str, hint: str | None = None):
        assert code in ERROR_CODES, code
        super().__init__(message)
        self.code, self.message, self.hint = code, message, hint

    def result(self, as_of: str | None = None) -> ToolResult:
        return ToolResult(ok=False, as_of=as_of, error=ToolErrorInfo(code=self.code, message=self.message, hint=self.hint))
