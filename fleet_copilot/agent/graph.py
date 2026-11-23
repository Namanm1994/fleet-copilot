"""The agent graph (LangGraph).

input_guard → planner → agent ⇄ tools → finalize (validate + verify, may loop back to agent)
            → auto_propose → respond → approval (interrupt; human decisions from the CLI only) → END

Out-of-scope, clarification, plan failure and budget stops go straight to respond.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import ValidationError

from fleet_copilot.agent.models import SUBMIT_ANSWER_DESCRIPTION, SUBMIT_ANSWER_SPEC_NAME, FinalAnswer, Plan
from fleet_copilot.agent.proposals import auto_propose
from fleet_copilot.agent.render import CAPABILITIES, device_names, render_turn
from fleet_copilot.agent.verifier import Verifier, strip_failed
from fleet_copilot.llm.base import BudgetExceeded
from fleet_copilot.llm.retry import LLMCallError
from fleet_copilot.llm.structured import RepairFailed, parse_with_repair, validation_message
from fleet_copilot.toolkit import inline_refs

if TYPE_CHECKING:
    from fleet_copilot.agent.session import CopilotSession



# Asking again in the same words would be cut off at the same place, so ask for less.
CUT_OFF_REPAIR = ("Your reply was cut off at the output limit before submit_answer was complete. Call submit_answer "
                  "now with a shorter answer: one claim per finding, devices that share a finding in a single claim, "
                  "and only the evidence IDs each claim needs.")


class TurnState(TypedDict, total=False):
    question: str
    interactive: bool
    status: str
    note: str
    plan: dict
    messages: list[dict]
    tool_calls: int
    seen_calls: list[str]
    revisions: int
    repairs: int
    answer: dict
    verification: dict
    proposals: list[dict]
    skipped: list[dict]
    text: str
    decisions: dict
    results: list[dict]


def ambiguous_refs(resolved: Any) -> list[dict[str, Any]]:
    """References that match several devices. A reference that is an employee ID is not one: it means all
    of that person's devices."""
    return [r for r in resolved or [] if isinstance(r, dict) and r.get("status") == "ambiguous"
            and not r.get("employee_ids") and len(r.get("matches") or []) > 1]


def clarification_text(ambiguous: list[dict[str, Any]]) -> str:
    lines = []
    for a in ambiguous:
        lines.append(f"\"{a['ref']}\" matches {len(a['matches'])} devices. Which one do you mean?")
        lines += [f"  - {m['device_id']}: {m.get('label') or m['device_id']}" for m in a["matches"]]
    lines.append("Name the device ID, or ask about all of them (for example \"all Dell devices\").")
    return "\n".join(lines)


def build_graph(sess: CopilotSession, checkpointer: Any):
    tk = sess.tk
    policy = tk.config.policy
    trace = sess.tracer.emit

    def stop(state: TurnState, exc: Exception, node: str) -> TurnState:
        kind = getattr(exc, "kind", "error")
        trace(node, "budget_stop" if isinstance(exc, BudgetExceeded) else "error", {"error": str(exc), "kind": kind})
        note = (f"I stopped before finishing: {exc} No verified answer was produced. Try a narrower question."
                if isinstance(exc, BudgetExceeded) else f"I couldn't complete this request ({exc}).")
        return {"status": "partial", "note": note}

    # ---- nodes ------------------------------------------------------------------------------
    def input_guard(state: TurnState) -> TurnState:
        q = state["question"]
        if len(q) > policy.input_max_chars:
            trace("input_guard", "guard_result", {"ok": False, "reason": "too_long", "chars": len(q)})
            return {"status": "refused", "note": f"Your message is {len(q)} characters; the limit is {policy.input_max_chars}. Please shorten it."}
        cap = sess.usage_cap_message()
        if cap:
            trace("input_guard", "guard_result", {"ok": False, "reason": "usage_cap"})
            tk.audit_event("budget_refused", detail={"reason": cap})
            return {"status": "refused", "note": cap}
        trace("input_guard", "guard_result", {"ok": True})
        return {"status": "running", "tool_calls": 0, "revisions": 0, "repairs": 0, "proposals": [], "skipped": [],
                "seen_calls": []}

    def planner(state: TurnState) -> TurnState:
        msgs = [{"role": "system", "content": sess.planner_prompt()}] + sess.history_messages() + [
            {"role": "user", "content": state["question"]}]
        try:
            effort = sess.settings.reasoning_effort.get("planner") or None
            plan, repairs = parse_with_repair(lambda m: sess.llm(m, None, effort), msgs, Plan, sess.settings.repair_retries,
                                              lambda e, d: trace("planner", e, d))
        except RepairFailed as exc:
            trace("planner", "plan_failed", {"error": str(exc)})
            return {"status": "plan_failed", "note": "I couldn't process that request. Please rephrase it."}
        except (BudgetExceeded, LLMCallError) as exc:
            return stop(state, exc, "planner")
        plan_d = plan.model_dump()
        trace("planner", "plan", plan_d)
        if plan.forbidden_action in policy.forbidden_actions:
            # The refusal is written here, not by the model: what is forbidden is policy, and so is how it is said.
            name = plan.forbidden_action.replace("_", " ")
            tk.audit_event("refused", detail={"reason": f"forbidden action {plan.forbidden_action}", "question": state["question"][:200]})
            return {"plan": plan_d, "status": "refused",
                    "note": f"I can't do that: \"{name}\" is not available in this assistant, and no approval can enable it. {CAPABILITIES}"}
        if not plan.in_scope:
            reason = plan.refusal_reason or "That's outside what I can help with."
            tk.audit_event("refused", detail={"reason": reason, "question": state["question"][:200]})
            return {"plan": plan_d, "status": "refused", "note": f"{reason} {CAPABILITIES}"}
        if plan.needs_clarification and plan.clarification_question:
            return {"plan": plan_d, "status": "clarify", "note": plan.clarification_question}
        system = sess.agent_prompt(plan_d)
        return {"plan": plan_d, "repairs": repairs,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": state["question"]}]}

    def agent(state: TurnState) -> TurnState:
        context, freed = sess.compact(state["messages"])
        seen = [k for k in state.get("seen_calls", []) if k not in freed]
        try:
            resp = sess.llm(context, sess.tool_specs())
        except (BudgetExceeded, LLMCallError) as exc:
            return stop(state, exc, "agent") | {"messages": context, "seen_calls": seen}
        msg = resp.to_message()
        # A reply cut off before any text or tool call is empty, and some providers reject empty messages.
        messages = context + ([msg] if resp.content or resp.tool_calls else [])
        if resp.content and resp.tool_calls:
            trace("agent", "deviation_reason", {"text": resp.content[:500]})
        if not resp.tool_calls:
            repairs = state.get("repairs", 0) + 1
            reason = "reply cut off at the output limit" if resp.truncated else "no submit_answer call"
            trace("agent", "repair_attempt", {"reason": reason, "attempt": repairs})
            if repairs > sess.settings.repair_retries:
                return {"messages": messages, "seen_calls": seen, "status": "partial",
                        "note": "I couldn't produce a verifiable answer. Please rephrase the question."}
            messages.append({"role": "user", "content": CUT_OFF_REPAIR if resp.truncated else
                             "Call submit_answer with the structured answer (do not reply in plain text)."})
            return {"messages": messages, "seen_calls": seen, "repairs": repairs}
        return {"messages": messages, "seen_calls": seen}

    def tools(state: TurnState) -> TurnState:
        messages = list(state["messages"])
        last = messages[-1]
        count = state.get("tool_calls", 0)
        seen = list(state.get("seen_calls", []))
        proposals = list(state.get("proposals", []))
        ambiguous: list[dict[str, Any]] = []
        for tc in last.get("tool_calls", []):
            name, args = tc["name"], tc.get("args") or {}
            key = f"{name}:{json.dumps(args, sort_keys=True)}"
            extra: dict[str, Any] = {}
            if count >= policy.max_tool_calls_per_turn:
                content = json.dumps({"ok": False, "error": {"code": "BUDGET_EXCEEDED",
                                      "message": f"Tool-call limit of {policy.max_tool_calls_per_turn} per turn reached. Call submit_answer now with what you have."}})
                trace("tools", "tool_call_refused", {"tool": name, "reason": "tool_call_cap"})
            elif key in seen:
                content = json.dumps({"ok": False, "error": {"code": "DUPLICATE_CALL",
                                      "message": "Duplicate call: the same tool with the same arguments already ran this turn. Use the earlier result."}})
                trace("tools", "tool_call_refused", {"tool": name, "reason": "duplicate_call"})
            else:
                count += 1
                seen.append(key)
                trace("tools", "tool_call", {"tool": name, "args": args})
                res = tk.call(name, args, origin="requested")
                trace("tools", "tool_result", {"tool": name, "ok": res.ok, "evidence_ids": res.evidence_ids[:50],
                                               "receipt_id": res.receipt_id, "total_count": res.total_count,
                                               "error": res.error.model_dump() if res.error else None})
                if name == "resolve_entities" and res.ok:
                    ambiguous += ambiguous_refs(res.data)
                if res.ok and isinstance(res.data, dict) and str(res.data.get("proposal_id", "")).startswith("PRP-"):
                    proposals.append(res.data | {"device_id": args.get("device_id")})
                    trace("tools", "proposal", {"proposal_id": res.data["proposal_id"], "origin": "requested"})
                content = res.to_llm(sess.max_tool_output_chars)
                if '"Output shortened to fit' in content or '"Result too large to show' in content:
                    trace("tools", "tool_output_shortened", {"tool": name, "chars": len(content), "limit": sess.max_tool_output_chars})
                if res.ok:  # what stays in context if this result is condensed later
                    stub = {"condensed": True, "tool": name, "receipt_id": res.receipt_id, "total_count": res.total_count,
                            "evidence_ids": res.evidence_ids[:20], "more_evidence_ids": max(0, len(res.evidence_ids) - 20),
                            "note": "Condensed to save context. The evidence IDs stay valid. Call the tool again if you need the values."}
                    extra = {"stub": f"<tool_data>{json.dumps(stub, ensure_ascii=False)}</tool_data>", "call_key": key}
            messages.append({"role": "tool", "tool_call_id": tc["id"], "name": name, "content": content} | extra)
        if ambiguous:
            # Which device the admin means is theirs to say. The turn ends here with the question, written in
            # code from the tool's own matches, so no device is read or acted on by guesswork (G11).
            trace("tools", "clarification", {"refs": [a["ref"] for a in ambiguous],
                                             "matches": [m["device_id"] for a in ambiguous for m in a["matches"]]})
            return {"messages": messages, "tool_calls": count, "seen_calls": seen, "proposals": proposals,
                    "status": "clarify", "note": clarification_text(ambiguous)}
        return {"messages": messages, "tool_calls": count, "seen_calls": seen, "proposals": proposals}

    def finalize(state: TurnState) -> TurnState:
        messages = list(state["messages"])
        last = messages[-1]
        submit = next(tc for tc in last["tool_calls"] if tc["name"] == SUBMIT_ANSWER_SPEC_NAME)
        for tc in last["tool_calls"]:
            if tc is not submit:
                messages.append({"role": "tool", "tool_call_id": tc["id"], "name": tc["name"],
                                 "content": "Not executed: submit_answer was called in the same step."})
        try:
            answer = FinalAnswer.model_validate(submit.get("args") or {})
        except ValidationError as exc:
            repairs = state.get("repairs", 0) + 1
            err = validation_message(exc)
            trace("finalize", "repair_attempt", {"schema": "FinalAnswer", "attempt": repairs, "error": err})
            if repairs > sess.settings.repair_retries:
                return {"messages": messages, "status": "partial", "note": "I could not verify an answer from the data."}
            messages.append({"role": "tool", "tool_call_id": submit["id"], "name": SUBMIT_ANSWER_SPEC_NAME,
                             "content": f"Invalid answer format: {err}. Call submit_answer again with valid fields."})
            return {"messages": messages, "repairs": repairs}
        v = Verifier(tk).verify(answer, state.get("question", ""))
        trace("verifier", "verifier_result", {"ok": v.ok, "errors": v.errors})
        if v.ok:
            messages.append({"role": "tool", "tool_call_id": submit["id"], "name": SUBMIT_ANSWER_SPEC_NAME, "content": "Accepted."})
            return {"messages": messages, "answer": answer.model_dump(), "verification": {"ok": True, "errors": []},
                    "status": "answered"}
        revisions = state.get("revisions", 0) + 1
        if revisions > policy.max_verifier_revisions:
            kept = strip_failed(answer, v)
            return {"messages": messages, "answer": kept.model_dump(), "status": "answered",
                    "verification": {"ok": False, "errors": v.errors, "fallback": True}}
        messages.append({"role": "tool", "tool_call_id": submit["id"], "name": SUBMIT_ANSWER_SPEC_NAME,
                         "content": v.feedback() + "\nFix these and call submit_answer again."})
        return {"messages": messages, "revisions": revisions}

    def propose(state: TurnState) -> TurnState:
        if state.get("status") != "answered" or (state.get("answer") or {}).get("answer_type") != "answer":
            return {}
        created, skipped = auto_propose(tk, origin="unprompted")
        for p in created:
            trace("auto_propose", "proposal", {"proposal_id": p["proposal_id"], "origin": "unprompted", "action": p["action"]})
        if skipped:
            trace("auto_propose", "skipped", {"items": skipped})
        return {"proposals": list(state.get("proposals", [])) + created, "skipped": skipped}

    def respond(state: TurnState) -> TurnState:
        proposals = [tk.store.get(p["proposal_id"]) | {"preview": p.get("preview")} for p in state.get("proposals", [])]
        text = render_turn(tenant_name=tk.ctx.tenant_name, as_of=tk.repo.as_of, status=state.get("status", ""),
                           answer=state.get("answer"), note=state.get("note"), proposals=proposals,
                           skipped=state.get("skipped", []), thresholds=tk.config.thresholds,
                           names=device_names(tk.repo.devices(), "\n".join(tk.tool_corpus)) if state.get("answer") else None)
        trace("respond", "final_answer", {"status": state.get("status"), "answer": state.get("answer"),
                                          "verification": state.get("verification"), "text": text,
                                          "proposals": [p["proposal_id"] for p in proposals]})
        return {"text": text, "proposals": proposals}

    def approval(state: TurnState) -> TurnState:
        pending = [p for p in state.get("proposals", []) if p["status"] == "proposed"]
        decisions = interrupt({"proposals": pending})
        results = []
        for pid, decision in (decisions or {}).items():
            kind, reason = (decision if isinstance(decision, (list, tuple)) else (decision, ""))
            try:
                if kind == "approve":
                    r = tk.executor.approve(pid, tk.ctx.operator)
                elif kind == "reject":
                    r = tk.executor.reject(pid, tk.ctx.operator, reason or "")
                else:
                    r = tk.executor.later(pid, tk.ctx.operator)
                results.append({"proposal_id": pid, "status": r["status"], "error": r.get("error")})
            except (KeyError, ValueError) as exc:
                results.append({"proposal_id": pid, "status": "error", "error": str(exc)})
            trace("approval", "approval", {"proposal_id": pid, "decision": kind, "result": results[-1]["status"]})
        return {"decisions": decisions or {}, "results": results}

    # ---- routing ----------------------------------------------------------------------------
    def after_guard(state: TurnState) -> str:
        return "planner" if state.get("status") == "running" else "respond"

    def after_planner(state: TurnState) -> str:
        return "agent" if state.get("messages") and state.get("status") == "running" else "respond"

    def after_agent(state: TurnState) -> str:
        if state.get("status") != "running":
            return "respond"
        last = state["messages"][-1]
        if last["role"] != "assistant":
            return "agent"
        names = [tc["name"] for tc in last.get("tool_calls", [])]
        return "finalize" if SUBMIT_ANSWER_SPEC_NAME in names else "tools"

    def after_finalize(state: TurnState) -> str:
        return "propose" if state.get("status") == "answered" else ("respond" if state.get("status") != "running" else "agent")

    def after_respond(state: TurnState) -> str:
        has_pending = any(p["status"] == "proposed" for p in state.get("proposals", []))
        return "approval" if has_pending and state.get("interactive") else END

    g = StateGraph(TurnState)
    for name, fn in [("input_guard", input_guard), ("planner", planner), ("agent", agent), ("tools", tools),
                     ("finalize", finalize), ("propose", propose), ("respond", respond), ("approval", approval)]:
        g.add_node(name, fn)
    g.add_edge(START, "input_guard")
    g.add_conditional_edges("input_guard", after_guard, ["planner", "respond"])
    g.add_conditional_edges("planner", after_planner, ["agent", "respond"])
    g.add_conditional_edges("agent", after_agent, ["tools", "finalize", "agent", "respond"])
    g.add_conditional_edges("tools", lambda s: "agent" if s.get("status") == "running" else "respond", ["agent", "respond"])
    g.add_conditional_edges("finalize", after_finalize, ["propose", "agent", "respond"])
    g.add_edge("propose", "respond")
    g.add_conditional_edges("respond", after_respond, ["approval", END])
    g.add_edge("approval", END)
    return g.compile(checkpointer=checkpointer)


def submit_answer_spec() -> dict[str, Any]:
    return {"name": SUBMIT_ANSWER_SPEC_NAME, "description": SUBMIT_ANSWER_DESCRIPTION,
            "parameters": inline_refs(FinalAnswer.model_json_schema())}
