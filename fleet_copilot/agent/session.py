"""A chat session: one tenant, one toolkit, one trace, one graph run per turn."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from fleet_copilot.agent.graph import build_graph, submit_answer_spec
from fleet_copilot.config import LLMSettings
from fleet_copilot.llm.base import ChatModel, Clock, LLMResponse, Message, TurnBudget, cost_usd
from fleet_copilot.llm.retry import RetryingModel
from fleet_copilot.toolkit import Toolkit
from fleet_copilot.tracing import Tracer


def _clip(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def sqlite_checkpointer(path: Path) -> Any:
    """Durable checkpoints (var/checkpoints.db) so a pending approval survives a crash mid-turn."""
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    from fleet_copilot.paths import touch_private

    touch_private(path)
    return SqliteSaver(sqlite3.connect(str(path), check_same_thread=False))


def load_prompts(prompts_dir: Path) -> tuple[dict[str, str], str]:
    prompts = {p.stem: p.read_text(encoding="utf-8") for p in sorted(prompts_dir.glob("*.md"))}
    version = hashlib.sha256("".join(prompts[k] for k in sorted(prompts)).encode()).hexdigest()[:8]
    return prompts, version


@dataclass
class TurnResult:
    turn: int
    status: str
    text: str
    answer: dict | None
    verification: dict | None
    proposals: list[dict]
    pending_approval: bool
    usage: dict[str, Any]
    plan: dict | None = None
    results: list[dict] = field(default_factory=list)


class CopilotSession:
    def __init__(self, tk: Toolkit, model: ChatModel, settings: LLMSettings, prompts_dir: Path,
                 clock: Clock | None = None, checkpointer: Any = None, interactive: bool = True):
        self.tk = tk
        self.settings = settings
        self.clock = clock or Clock()
        self.prompts, self.prompt_version = load_prompts(prompts_dir)
        self.tracer = Tracer(tk.paths.traces, tk.ctx.session_id, tk.ctx.tenant_id, self.prompt_version)
        self.model = RetryingModel(model, settings, self.clock, on_event=lambda e, d: self.tracer.emit("llm", e, d))
        self.interactive = interactive
        self.history: list[dict[str, Any]] = []
        self.session_input_tokens = 0
        ctx = tk.config.policy.context  # limits live in policy.yaml (context:)
        self.history_turns = ctx["history_turns"]
        self.digest_max_chars = ctx["digest_max_chars"]
        self.context_max_chars = ctx["max_chars_per_turn"]
        self.keep_recent_tool_results = ctx["keep_recent_tool_results"]
        self.max_tool_output_chars = ctx["max_tool_output_chars"]
        self.focus_evidence_ids = ctx["focus_evidence_ids"]
        self.focus_queries = ctx["focus_queries"]
        self.budget: TurnBudget | None = None
        self.graph = build_graph(self, checkpointer or InMemorySaver())
        self._thread: dict[str, Any] | None = None
        tk.audit_event("session_started", detail={"prompt_version": self.prompt_version, "model": getattr(model, "model", "")})

    # ---- prompts and context ---------------------------------------------------------------
    def tool_specs(self) -> list[dict[str, Any]]:
        return self.tk.tool_specs() + [submit_answer_spec()]

    def planner_prompt(self) -> str:
        tools = "\n".join(f"- {s['name']}: {s['description']}" for s in self.tk.tool_specs())
        older = "\n".join(self.history_digest()) or "(none)"
        return self.prompts["planner"].format(tenant_name=self.tk.ctx.tenant_name, as_of=self.tk.repo.as_of[:10],
                                              tool_list=tools, older=older)

    def agent_prompt(self, plan: dict) -> str:
        return self.prompts["agent"].format(tenant_name=self.tk.ctx.tenant_name, as_of=self.tk.repo.as_of[:10],
                                            plan=json.dumps(plan, ensure_ascii=False), history=self.history_text())

    def history_digest(self) -> list[str]:
        """Turns older than the recent window, one line each (built in code, no LLM call)."""
        older = self.history[:-self.history_turns]
        lines: list[str] = []
        used = 0
        for h in reversed(older):  # newest of the old turns first; the oldest fall off the cap
            line = f"turn {h['turn']}: asked \"{_clip(h['question'], 90)}\" -> {_clip(h['summary'], 140)}"
            if h.get("focus"):
                line += f" [in focus: {', '.join(h['focus'][:5])}]"
            if h.get("proposals"):
                line += f" [proposals: {', '.join(h['proposals'])}]"
            if used + len(line) > self.digest_max_chars:
                lines.append(f"({len(older) - len(lines)} earlier turn(s) omitted)")
                break
            lines.append(line)
            used += len(line)
        return list(reversed(lines))

    def compact(self, messages: list[Message]) -> tuple[list[Message], list[str]]:
        """In-turn context management: when the message list is too large, replace the oldest tool
        results with a short stub (receipt, count, evidence IDs). Returns the new list and the call
        keys of condensed results, so the same query may be run again if its values are needed."""
        size = sum(len(m.get("content") or "") for m in messages)
        if size <= self.context_max_chars:
            return messages, []
        tool_idx = [i for i, m in enumerate(messages) if m["role"] == "tool" and m.get("stub") and not m.get("compacted")]
        candidates = tool_idx[:-self.keep_recent_tool_results] if self.keep_recent_tool_results else tool_idx
        out = list(messages)
        freed: list[str] = []
        before = size
        for i in candidates:
            if size <= self.context_max_chars:
                break
            m = out[i]
            size -= len(m["content"]) - len(m["stub"])
            out[i] = {**m, "content": m["stub"], "compacted": True}
            if m.get("call_key"):
                freed.append(m["call_key"])
        if freed or size != before:
            self.tracer.emit("agent", "context_compacted", {"results_condensed": len(freed), "chars_before": before,
                                                             "chars_after": size, "limit": self.context_max_chars})
        return out, freed

    def history_text(self) -> str:
        if not self.history:
            return "(nothing yet)"
        lines = []
        digest = self.history_digest()
        if digest:
            lines += ["Older turns (condensed):"] + [f"  {d}" for d in digest] + ["Recent turns:"]
        for h in self.history[-self.history_turns:]:
            lines.append(f"Q: {h['question']}\nA: {h['summary']}")
            if h.get("focus"):
                lines.append(f"   in focus: {', '.join(h['focus'][:25])}")
            if h.get("evidence"):
                lines.append(f"   evidence cited: {', '.join(h['evidence'])}")
            for q in h.get("queries", []):
                lines.append(f"   query run: {q}")
            if h.get("proposals"):
                lines.append(f"   proposals: {', '.join(h['proposals'])}")
        return "\n".join(lines)

    def history_messages(self) -> list[Message]:
        out: list[Message] = []
        for h in self.history[-self.history_turns:]:
            answer = h["summary"] + (f" [in focus: {', '.join(h['focus'][:25])}]" if h.get("focus") else "")
            out += [{"role": "user", "content": h["question"]}, {"role": "assistant", "content": answer}]
        return out

    def _focus(self, answer: dict | None, turn: int) -> dict[str, list[str]]:
        """Structured focus state for follow-ups, taken only from the verified answer:
        the devices its claims are about, the evidence they cite, and the queries that were run."""
        ev = self.tk.evidence
        devices: list[str] = []
        cited: list[str] = []
        for claim in (answer or {}).get("claims", []):
            for eid in claim.get("evidence_ids", []):
                if eid not in cited:
                    cited.append(eid)
                receipt = ev.receipts.get(eid)
                members = receipt["evidence_ids"] if receipt else [eid]
                for m in members:
                    rec = ev.get(m)
                    if rec and rec.device_id and rec.device_id not in devices:
                        devices.append(rec.device_id)
        queries = [f"{r['tool']} {json.dumps(r['args'], ensure_ascii=False, sort_keys=True)} ({rid}, {r['result_count']} rows)"
                   for rid, r in ev.receipts.items() if r["turn"] == turn]
        return {"focus": devices, "evidence": cited[:self.focus_evidence_ids],
                "queries": [_clip(q, 200) for q in queries[:self.focus_queries]]}

    def llm(self, messages: list[Message], tools: list[dict[str, Any]] | None) -> LLMResponse:
        assert self.budget is not None
        start = self.clock.now()
        resp = self.model.invoke(messages, tools, self.budget)
        self.tracer.emit("llm", "llm_call", {"tools": bool(tools), "tool_calls": [t.name for t in resp.tool_calls]},
                         tokens={"input": resp.input_tokens, "output": resp.output_tokens},
                         latency_ms=round((self.clock.now() - start) * 1000))
        self.budget.check()
        return resp

    # ---- usage caps (across turns) ----------------------------------------------------------
    @staticmethod
    def _today() -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def usage_cap_message(self) -> str | None:
        policy = self.tk.config.policy
        if policy.max_input_tokens_per_session and self.session_input_tokens >= policy.max_input_tokens_per_session:
            return (f"This session has reached its usage cap ({policy.max_input_tokens_per_session:,} input tokens). "
                    "Start a new session to continue.")
        if policy.max_input_tokens_per_day:
            used = self.tk.store.usage(self._today())["input_tokens"]
            if used >= policy.max_input_tokens_per_day:
                return (f"{self.tk.ctx.tenant_name} has reached today's usage cap ({policy.max_input_tokens_per_day:,} "
                        "input tokens). It resets at midnight UTC; the cap is set in config/policy.yaml.")
        return None

    # ---- turns ------------------------------------------------------------------------------
    def ask(self, question: str) -> TurnResult:
        turn = self.tk.new_turn()
        self.tracer.turn = turn
        policy = self.tk.config.policy
        self.budget = TurnBudget(policy.max_input_tokens_per_turn, policy.max_output_tokens_per_turn,
                                 policy.turn_deadline_s, self.clock)
        self.tracer.emit("input", "input", {"question": question})
        self._thread = {"configurable": {"thread_id": f"{self.tk.ctx.session_id}-{turn}"}}
        try:
            state = self.graph.invoke({"question": question, "interactive": self.interactive}, self._thread)
        except KeyboardInterrupt:
            self.tracer.emit("session", "cancelled", {"turn": turn})
            raise
        except Exception as exc:
            self.tracer.emit("session", "error", {"turn": turn, "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
            raise
        return self._result(turn, state)

    def decide(self, decisions: dict[str, Any]) -> list[dict[str, Any]]:
        """Resume the approval interrupt with the human's decisions (CLI only)."""
        if self._thread is None:
            return []
        state = self.graph.invoke(Command(resume=decisions), self._thread)
        return state.get("results", [])

    def _result(self, turn: int, state: dict[str, Any]) -> TurnResult:
        b = self.budget
        usage = {"input_tokens": b.input_tokens, "output_tokens": b.output_tokens, "llm_calls": b.calls,
                 "latency_s": round(b.elapsed(), 2), "tool_calls": state.get("tool_calls", 0),
                 "cost_usd": cost_usd(self.model.model, b.input_tokens, b.output_tokens, self.settings.pricing)}
        self.tracer.emit("session", "usage", usage)
        self.session_input_tokens += b.input_tokens
        if b.input_tokens or b.output_tokens:
            self.tk.store.add_usage(self._today(), b.input_tokens, b.output_tokens)
        answer = state.get("answer")
        summary = answer["summary"] if answer else (state.get("note") or "")
        focus = self._focus(answer, turn)
        self.history.append({"turn": turn, "question": _clip(state.get("question", ""), 300), "summary": _clip(summary, 400), **focus,
                             "proposals": [p["proposal_id"] for p in state.get("proposals", [])]})
        snapshot = self.graph.get_state(self._thread)
        pending = bool(snapshot.next)
        return TurnResult(turn=turn, status=state.get("status", ""), text=state.get("text", ""), answer=answer,
                          verification=state.get("verification"), proposals=state.get("proposals", []),
                          pending_approval=pending, usage=usage, plan=state.get("plan"))
