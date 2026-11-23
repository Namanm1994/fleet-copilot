"""Structured outputs: the plan and the final answer."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Scalar = str | int | float | bool | None


class PlanStep(BaseModel):
    tool: str
    purpose: str = ""


class Plan(BaseModel):
    model_config = ConfigDict(extra="ignore")
    intent_class: str = "other"
    in_scope: bool = True
    refusal_reason: str | None = None
    forbidden_action: str | None = None      # set when the admin asks for an action policy.yaml forbids
    entities: list[str] = Field(default_factory=list)
    needs_clarification: bool = False
    clarification_question: str | None = None
    steps: list[PlanStep] = Field(default_factory=list)


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(..., min_length=1, max_length=600)
    evidence_ids: list[str] = Field(default_factory=list)
    values: dict[str, Scalar | list[Scalar]] = Field(default_factory=dict)


class FinalAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(..., min_length=1, max_length=800)
    claims: list[Claim] = Field(default_factory=list, max_length=40)
    definitions_used: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    answer_type: Literal["answer", "no_matches", "insufficient_data", "clarification", "refusal"] = "answer"


SUBMIT_ANSWER_SPEC_NAME = "submit_answer"
SUBMIT_ANSWER_DESCRIPTION = (
    "Submit the final answer. Call exactly once when done. Every claim cites evidence IDs returned by tools; "
    "counts and 'no matches' cite query receipt IDs (Q-…)."
)
