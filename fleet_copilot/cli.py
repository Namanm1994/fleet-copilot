"""Command-line interface.

Every command that touches tenant data takes --tenant first and fails closed (G3).
Telemetry and model text is printed with markup disabled, so device strings can't fake the UI (G33).
Approvals are read only from an interactive terminal (F-22); tests and the eval runner use the
named hook `set_approval_hook`, never piped input.
"""

from __future__ import annotations

import getpass
import json
import sys
import time
from collections.abc import Callable
from typing import Any

import click
from rich.console import Console
from rich.text import Text

from fleet_copilot.config import ConfigError, load_app_config, load_dotenv, load_llm_settings
from fleet_copilot.paths import PROJECT_ROOT, Paths, default_paths, ensure_private_dir

POLICY_FILE = "policy.yaml"
console = Console(highlight=False)

# ---- approval input ---------------------------------------------------------------------------
ApprovalHook = Callable[[dict[str, Any]], tuple[str, str]]
_approval_hook: ApprovalHook | None = None


def set_approval_hook(hook: ApprovalHook | None) -> None:
    """Named test hook: answers approval prompts in tests and evals instead of a terminal."""
    global _approval_hook
    _approval_hook = hook


def approvals_available() -> bool:
    return _approval_hook is not None or (sys.stdin.isatty() and sys.stdout.isatty())


APPROVE_WORDS = ("y", "yes", "a", "approve")
REJECT_WORDS = ("n", "no", "r", "reject")
LATER_WORDS = ("l", "later", "skip", "")
DECISION_PROMPT = "  [y] approve  [n] reject  [l] later › "


def read_line(prompt: str) -> str:
    """Read one line. The prompt is printed literally: square brackets are not styling."""
    return console.input(Text(prompt), markup=False).strip()


def parse_decision(answer: str) -> str | None:
    word = answer.strip().lower()
    if word in APPROVE_WORDS:
        return "approve"
    if word in REJECT_WORDS:
        return "reject"
    if word in LATER_WORDS:
        return "later"
    return None


def _ask_decision(p: dict[str, Any]) -> tuple[str, str]:
    if _approval_hook is not None:
        return _approval_hook(p)
    while True:
        decision = parse_decision(read_line(DECISION_PROMPT))
        if decision == "reject":
            return "reject", read_line("  Reason (optional) › ")
        if decision is not None:
            return decision, ""
        say("  Not recognised. Type y (or approve), n (or reject), or l (or later). Enter alone means later.", "yellow")


# ---- output -----------------------------------------------------------------------------------
def say(text: str, style: str | None = None) -> None:
    """Print untrusted text literally: no markup, no auto-highlighting, no links."""
    console.print(Text(text, style=style or ""), markup=False, highlight=False, soft_wrap=True)


def fail(message: str, code: int = 2) -> None:
    console.print(Text(message, style="bold red"), markup=False)
    sys.exit(code)


def proposal_block(p: dict[str, Any]) -> None:
    head = Text(f"{p['proposal_id']} · {p['action']}", style="bold")
    if p.get("origin") and p["origin"] != "requested":
        head.append(f" ({p['origin']})", style="dim")
    console.print(head)
    target = p.get("device_id") or ""
    if p.get("employee_id"):
        target += f" · employee {p['employee_id']}"
    say(f"  target:   {target}")
    say(f"  reason:   {p.get('reason') or ''}")
    say(f"  evidence: {', '.join(p.get('evidence_ids', [])[:4])}")
    say(f"  {'message' if p['action'] == 'notify_employee' else 'action'}:  {p.get('preview') or ''}")
    for w in p.get("warnings") or []:
        say(f"  warning:  {w}", "yellow")


def run_approvals(proposals: list[dict[str, Any]]) -> dict[str, Any]:
    """Approval screen. Similar proposals (same action and check) can be approved together."""
    decisions: dict[str, Any] = {}
    groups: dict[tuple, list[dict]] = {}
    for p in proposals:
        key = (p["action"], (p.get("params") or {}).get("check_id"))
        groups.setdefault(key, []).append(p)
    console.print(Text("\nApproval needed. Nothing runs until you approve.", style="bold"))
    for (action, check), items in groups.items():
        for p in items:
            proposal_block(p)
        if len(items) > 1:
            say(f"  {len(items)} similar proposals ({action}{', ' + check if check else ''}): one answer applies to all.", "dim")
        decision = _ask_decision(items[0] if len(items) == 1 else {"group": [p["proposal_id"] for p in items], **items[0]})
        for p in items:
            decisions[p["proposal_id"]] = decision
    return decisions


def print_results(results: list[dict[str, Any]]) -> None:
    for r in results:
        style = {"executed": "green", "rejected": "yellow", "pending": "dim"}.get(r["status"], "red")
        say(f"  {r['proposal_id']}: {r['status']}" + (f" ({r['error']})" if r.get("error") else ""), style)


# ---- context ----------------------------------------------------------------------------------
class Ctx:
    def __init__(self, paths: Paths):
        self.paths = paths
        load_dotenv(paths.root / ".env")
        try:
            self.config = load_app_config(paths.config, POLICY_FILE)
        except ConfigError as exc:
            fail(f"Configuration error: {exc}")

    def tenant(self, ref: str | None):
        if not ref:
            fail("--tenant is required (no default).")
        t = self.config.policy.resolve_tenant(ref)
        if t is None:
            known = ", ".join(sorted(self.config.policy.tenants))
            fail(f"Unknown tenant '{ref}'. Registered tenants: {known}.")
        return t

    def require_db(self) -> None:
        if not self.paths.telemetry_db.exists():
            fail("No telemetry database. Run `fleet load` first.")

    def toolkit(self, tenant_ref: str, operator: str | None, as_of: str | None):
        from fleet_copilot.data.repository import dataset_as_of
        from fleet_copilot.session import SessionContext
        from fleet_copilot.toolkit import Toolkit

        t = self.tenant(tenant_ref)  # fail closed before any DB read
        self.require_db()
        ensure_private_dir(self.paths.var)
        stamp = f"{as_of}T23:59:59Z" if as_of else dataset_as_of(self.paths.telemetry_db)
        ctx = SessionContext.new(t.tenant_id, t.name, operator or getpass.getuser(), stamp)
        return Toolkit(ctx, self.config, self.paths)


pass_ctx = click.make_pass_decorator(Ctx)
tenant_opt = click.option("--tenant", "tenant", required=True, help="Company ID or alias (required).")
operator_opt = click.option("--operator", default=None, help="Name recorded in the audit log (default: OS user).")
as_of_opt = click.option("--as-of", "as_of", default=None, help="Treat this date (YYYY-MM-DD) as now.")


@click.group()
@click.pass_context
def main(click_ctx: click.Context) -> None:
    """Fleet Copilot: grounded Q&A, insights and approved actions over device telemetry."""
    click_ctx.obj = Ctx(default_paths())


# ---- data -------------------------------------------------------------------------------------
@main.command()
@click.argument("raw", nargs=-1, type=click.Path(dir_okay=False))
@click.option("--rebuild", is_flag=True, help="Discard the database and build it again from the files.")
@pass_ctx
def load(ctx: Ctx, raw: tuple[str, ...], rebuild: bool) -> None:
    """Load raw NDJSON into the telemetry database (default: data/raw/*.ndjson).

    Appends by default: snapshots already loaded are skipped. Use --rebuild to start fresh."""
    from pathlib import Path

    from fleet_copilot.data.loader import LoadError
    from fleet_copilot.data.loader import load as load_raw

    paths = [Path(p) for p in raw] or sorted(ctx.paths.raw_data.parent.glob("*.ndjson"))
    if not paths:
        fail(f"No raw data in {ctx.paths.raw_data.parent}. Copy the NDJSON file there.")
    ensure_private_dir(ctx.paths.var)
    try:
        report = load_raw(paths, ctx.paths.telemetry_db, ctx.config.policy.tenants.keys(), rebuild=rebuild)
    except LoadError as exc:
        fail(str(exc))
    ctx.paths.data_quality.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    skipped = f" ({report['already_loaded']} already loaded, skipped)" if report["already_loaded"] else ""
    say(f"Loaded {report['new_snapshots']} new snapshots{skipped}; {report['devices']} devices in total; "
        f"data as of {report['as_of']}.", "green")
    say(f"Data-quality report: {ctx.paths.data_quality}")
    for key in ("duplicates", "cycle_count_decreases", "encrypted_flag_mismatches"):
        if report.get(key):
            say(f"  {key}: {report[key] if not isinstance(report[key], list) else len(report[key])}", "yellow")
    if report.get("unregistered_tenants"):
        say(f"  Companies in the data but not in policy.yaml (inaccessible): {', '.join(report['unregistered_tenants'])}", "yellow")


@main.command()
@pass_ctx
def profile(ctx: Ctx) -> None:
    """Print value distributions of the loaded data (used to set thresholds)."""
    from fleet_copilot.data.profile import profile as run_profile

    ctx.require_db()
    say(json.dumps(run_profile(ctx.paths.telemetry_db), indent=2, default=str))


# ---- chat -------------------------------------------------------------------------------------
def _session(ctx: Ctx, tk, interactive: bool):
    from fleet_copilot.agent.session import CopilotSession, sqlite_checkpointer
    from fleet_copilot.llm.providers import build_model

    try:
        settings = load_llm_settings(ctx.paths.config)
        model = build_model(settings)
    except ConfigError as exc:
        fail(f"LLM configuration error: {exc}")
    return CopilotSession(tk, model, settings, PROJECT_ROOT / "prompts",
                          checkpointer=sqlite_checkpointer(ctx.paths.checkpoints_db), interactive=interactive)


def _usage_line(u: dict[str, Any]) -> str:
    cost = f"${u['cost_usd']:.4f}" if u.get("cost_usd") is not None else "cost n/a (model not in pricing.yaml)"
    return (f"[{u['llm_calls']} LLM calls · {u['tool_calls']} tool calls · {u['input_tokens']}/{u['output_tokens']} "
            f"tokens in/out · {u['latency_s']}s · {cost}]")


def _turn(sess, question: str) -> None:
    res = sess.ask(question)
    say(res.text)
    say(_usage_line(res.usage), "dim")
    pending = [p for p in res.proposals if p["status"] == "proposed"]
    if res.pending_approval and pending:
        print_results(sess.decide(run_approvals(pending)))
    elif pending:
        say("Approve in an interactive terminal: `fleet proposals approve <id> --tenant ...`.", "dim")


@main.command()
@tenant_opt
@operator_opt
@as_of_opt
@pass_ctx
def chat(ctx: Ctx, tenant: str, operator: str | None, as_of: str | None) -> None:
    """Interactive session for one company. exit/quit/Ctrl+D ends; Ctrl+C cancels the current turn."""
    tk = ctx.toolkit(tenant, operator, as_of)
    sess = _session(ctx, tk, interactive=approvals_available())
    console.print(Text(f"Fleet Copilot · {tk.ctx.tenant_name} · data as of {tk.repo.as_of[:10]} · session {tk.ctx.session_id}",
                       style="bold"))
    say("Ask about devices, trends or actions. Type exit to leave.", "dim")
    last_interrupt = 0.0
    while True:
        try:
            question = read_line("\nYou › ")
        except EOFError:
            break
        except KeyboardInterrupt:
            break
        if question.lower() in ("exit", "quit"):
            break
        if not question:
            continue
        try:
            _turn(sess, question)
        except Exception as exc:  # keep the session alive; the trace has the details
            tk.audit_event("turn_failed", detail={"error": type(exc).__name__})
            say(f"This turn failed ({type(exc).__name__}). Nothing was executed. "
                f"Details: fleet trace show {tk.ctx.session_id} --tenant {tenant}", "red")
        except KeyboardInterrupt:
            now = time.monotonic()
            tk.audit_event("cancelled", detail={"turn": tk.evidence.turn})
            if now - last_interrupt < 2:
                break
            last_interrupt = now
            say("Turn cancelled. Nothing was executed. (Ctrl+C again to exit.)", "yellow")
    tk.audit_event("session_ended")
    say(f"Session {tk.ctx.session_id} ended. Trace: fleet trace show {tk.ctx.session_id} --tenant {tenant}", "dim")


@main.command()
@tenant_opt
@operator_opt
@as_of_opt
@click.option("--json", "as_json", is_flag=True, help="Print the structured result as JSON.")
@click.argument("question")
@pass_ctx
def ask(ctx: Ctx, tenant: str, operator: str | None, as_of: str | None, as_json: bool, question: str) -> None:
    """One question, one answer. Proposals stay pending; approve them with `fleet proposals approve`."""
    tk = ctx.toolkit(tenant, operator, as_of)
    sess = _session(ctx, tk, interactive=False)
    try:
        res = sess.ask(question)
    except Exception as exc:
        tk.audit_event("turn_failed", detail={"error": type(exc).__name__})
        fail(f"The question failed ({type(exc).__name__}). Details: fleet trace show {tk.ctx.session_id} --tenant {tenant}", 1)
    if as_json:
        click.echo(json.dumps({"session_id": tk.ctx.session_id, "status": res.status, "answer": res.answer,
                               "verification": res.verification, "proposals": res.proposals, "usage": res.usage},
                              indent=2, default=str))
        return
    say(res.text)
    say(_usage_line(res.usage), "dim")


# ---- scan -------------------------------------------------------------------------------------
@main.command()
@tenant_opt
@operator_opt
@as_of_opt
@click.option("--summary", is_flag=True, help="Add a one-paragraph LLM summary (verified against the findings).")
@pass_ctx
def scan(ctx: Ctx, tenant: str, operator: str | None, as_of: str | None, summary: bool) -> None:
    """One-shot briefing: run every detector, propose eligible actions, ask for approval, exit."""
    from fleet_copilot.scan import render_scan, run_scan, summarize

    tk = ctx.toolkit(tenant, operator, as_of)
    result = run_scan(tk)
    if summary:
        sess = _session(ctx, tk, interactive=False)
        from fleet_copilot.llm.base import TurnBudget

        p = tk.config.policy
        sess.budget = TurnBudget(p.max_input_tokens_per_turn, p.max_output_tokens_per_turn, p.turn_deadline_s, sess.clock)
        summarize(result, sess.llm, sess.prompts["scan_summary"], sess.settings.repair_retries,
                  lambda e, d: sess.tracer.emit("scan", e, d))
    say(render_scan(result, tk.config.thresholds))
    if result.proposals and approvals_available():
        decisions = run_approvals(result.proposals)
        print_results(_apply(tk, decisions))
    elif result.proposals:
        say("Approve in an interactive terminal: `fleet proposals approve <id> --tenant ...`.", "dim")


def _apply(tk, decisions: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for pid, (kind, reason) in decisions.items():
        try:
            if kind == "approve":
                r = tk.executor.approve(pid, tk.ctx.operator)
            elif kind == "reject":
                r = tk.executor.reject(pid, tk.ctx.operator, reason)
            else:
                r = tk.executor.later(pid, tk.ctx.operator)
            out.append({"proposal_id": pid, "status": r["status"], "error": r.get("error")})
        except (KeyError, ValueError) as exc:
            out.append({"proposal_id": pid, "status": "error", "error": str(exc)})
    return out


# ---- proposals --------------------------------------------------------------------------------
@main.group()
def proposals() -> None:
    """List and decide on proposed actions."""


@proposals.command("list")
@tenant_opt
@click.option("--status", default=None, help="Filter by status (proposed, pending, executed, failed, ...).")
@pass_ctx
def proposals_list(ctx: Ctx, tenant: str, status: str | None) -> None:
    tk = ctx.toolkit(tenant, None, None)
    rows = tk.store.list(status=status)
    if not rows:
        say("No proposals.")
        return
    for p in rows:
        say(f"{p['proposal_id']:<8} {p['status']:<18} {p['action']:<28} {p.get('device_id') or p.get('employee_id') or ''}")
        say(f"         {p.get('preview') or ''}", "dim")


def _decide(ctx: Ctx, tenant: str, operator: str | None, proposal_id: str, kind: str, reason: str = "") -> None:
    tk = ctx.toolkit(tenant, operator, None)
    p = tk.store.get(proposal_id)
    if p is None:
        fail(f"{proposal_id} not found.", 1)  # other tenants' proposals look the same as missing ones
    if kind in ("approve", "retry"):
        if not approvals_available():
            fail("Approvals need an interactive terminal; piped or scripted input is not accepted.", 1)
        proposal_block(p)
        confirm = _ask_decision(p)[0] if _approval_hook else (
            "approve" if read_line(f"  {kind.capitalize()} {proposal_id}? [y/N] › ").lower() in APPROVE_WORDS else "later")
        if confirm != "approve":
            say("Not approved; nothing changed.", "dim")
            return
    try:
        if kind == "approve":
            r = tk.executor.approve(proposal_id, tk.ctx.operator)
        elif kind == "retry":
            r = tk.executor.retry(proposal_id, tk.ctx.operator)
        else:
            r = tk.executor.reject(proposal_id, tk.ctx.operator, reason)
    except ValueError as exc:
        fail(str(exc), 1)
    print_results([{"proposal_id": proposal_id, "status": r["status"], "error": r.get("error")}])


@proposals.command("approve")
@click.argument("proposal_id")
@tenant_opt
@operator_opt
@pass_ctx
def proposals_approve(ctx: Ctx, proposal_id: str, tenant: str, operator: str | None) -> None:
    _decide(ctx, tenant, operator, proposal_id, "approve")


@proposals.command("reject")
@click.argument("proposal_id")
@tenant_opt
@operator_opt
@click.option("--reason", default="", help="Why (recorded in the audit log).")
@pass_ctx
def proposals_reject(ctx: Ctx, proposal_id: str, tenant: str, operator: str | None, reason: str) -> None:
    _decide(ctx, tenant, operator, proposal_id, "reject", reason)


@proposals.command("retry")
@click.argument("proposal_id")
@tenant_opt
@operator_opt
@pass_ctx
def proposals_retry(ctx: Ctx, proposal_id: str, tenant: str, operator: str | None) -> None:
    _decide(ctx, tenant, operator, proposal_id, "retry")


# ---- audit and traces -------------------------------------------------------------------------
@main.group()
def audit() -> None:
    """Audit log tools."""


@audit.command("verify")
@pass_ctx
def audit_verify(ctx: Ctx) -> None:
    """Check the hash chain of the audit log."""
    from fleet_copilot.audit import verify

    if not ctx.paths.audit_log.exists():
        say("No audit log yet.")
        return
    ok, n, msg = verify(ctx.paths.audit_log)
    if ok:
        say(f"Audit log OK: {n} entries, chain intact.", "green")
    else:
        fail(f"Audit log FAILED after {n} entries: {msg}", 1)


@main.group()
def trace() -> None:
    """Session traces."""


@trace.command("show")
@click.argument("session_id")
@tenant_opt
@pass_ctx
def trace_show(ctx: Ctx, session_id: str, tenant: str) -> None:
    """Print a session's trace (plan, tool calls, verifier results, usage)."""
    from fleet_copilot.tracing import find_trace, read_trace

    t = ctx.tenant(tenant)
    path = find_trace(ctx.paths.traces, session_id)
    if path is None:
        fail(f"No trace for session {session_id}.", 1)
    try:
        events = read_trace(path, t.tenant_id)
    except PermissionError:
        fail(f"No trace for session {session_id}.", 1)
    for e in events:
        payload = json.dumps(e.get("payload", {}), ensure_ascii=False, default=str)
        extra = ""
        if e.get("tokens"):
            extra += f" tokens={e['tokens']['input']}/{e['tokens']['output']}"
        if e.get("latency_ms") is not None:
            extra += f" {e['latency_ms']}ms"
        say(f"{e['ts'][11:23]} t{e['turn']} {e['node']:<12} {e['event']:<16}{extra} {payload[:300]}")


@main.command()
@tenant_opt
@click.option("--days", default=7, show_default=True, help="How many days back, counting today (UTC).")
@click.option("--json", "as_json", is_flag=True, help="Print the numbers as JSON.")
@pass_ctx
def stats(ctx: Ctx, tenant: str, days: int, as_json: bool) -> None:
    """Usage and answer quality across sessions: tokens, cost, latency, verifier rejections, errors."""
    from fleet_copilot.stats import collect, render

    t = ctx.tenant(tenant)
    result = collect(ctx.paths.traces, t.tenant_id, days)
    if as_json:
        click.echo(json.dumps(result, indent=2))
    else:
        say(render(result))


# ---- evals -------------------------------------------------------------------------------------
@main.command("eval")
@click.option("--mode", type=click.Choice(["recorded", "live"]), default="recorded", show_default=True,
              help="recorded: replay saved model responses (offline). live: call the configured provider.")
@click.option("--record", is_flag=True, help="With --mode live: save responses as cassettes for recorded runs.")
@click.option("--runs", default=1, show_default=True, help="Runs per case (stability check).")
@click.option("--case", "only", multiple=True, help="Case ID or category to run (repeatable).")
@click.option("--control", type=click.Choice(["refuse_all", "do_nothing", "list_everything", "all"]), default=None,
              help="Score a stand-in agent that cannot be right, to check the scoring itself. No model is called.")
@pass_ctx
def eval_cmd(ctx: Ctx, mode: str, record: bool, runs: int, only: tuple[str, ...], control: str | None) -> None:
    """Run the eval suite (evals/cases/*.yaml) and write eval_report.md + eval_results.json under var/eval/."""
    from evals.runner import run_eval, write_report

    if control:
        from evals.controls import CONTROLS, run_control

        weak = False
        for name in (CONTROLS if control == "all" else (control,)):
            out, md = run_control(ctx.paths, name, list(only) or None)
            s = out["summary"]
            cells = ", ".join(f"{cat} {b['passed']}/{b['run']}" for cat, b in sorted(s["categories"].items()) if b["run"])
            say(f"{name:<16} {cells}")
            if s["unexpected_passes"]:
                weak = True
                say(f"  passes without being on the allowed list: {', '.join(s['unexpected_passes'])}", "red")
            say(f"  report: {md}")
        say("Run `fleet eval --mode recorded` to include these in the main report.")
        sys.exit(1 if weak else 0)
    if record and mode != "live":
        fail("--record needs --mode live.")
    out = run_eval(ctx.paths, mode=mode, record=record, runs=runs, only=list(only) or None)
    md, js = write_report(out, ctx.paths.var / "eval")
    s = out["summary"]
    for cat, b in sorted(s["categories"].items()):
        bar = s["bars"].get(cat)
        verdict = "" if not bar else {True: " (bar met)", False: " (BAR MISSED)", None: " (not run)"}[bar["met"]]
        say(f"{cat:<10} {b['passed']}/{b['run']} passed, {b['skipped']} skipped{verdict}",
            "red" if bar and bar["met"] is False else None)
    if not s["has_real_data"]:
        say("Real data not found in data/raw/: real-data cases were skipped.", "yellow")
    change = out.get("comparison")
    if change and change["comparable"]:
        worse, better = len(change["now_failing"]), len(change["now_passing"])
        say(f"Since the previous run: {worse} case(s) now fail, {better} now pass."
            + (f" Now failing: {', '.join(change['now_failing'])}" if worse else ""), "red" if worse else None)
    say(f"Report: {md}")
    if not s["all_bars_met"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
