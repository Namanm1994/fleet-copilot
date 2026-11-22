# Fleet Copilot

An agentic AI copilot that lets IT administrators query fleet telemetry in natural language, surface insights and trends, and propose evidence-backed operational actions that a human approves before anything runs.

> **Status:** early development. The design is settled; implementation is in progress.

## What it does

- **Grounded Q&A:** answers fleet health and compliance questions from telemetry only, citing evidence for every claim.
- **Insights and trends:** detects battery end-of-life, sustained RAM pressure, low storage, compliance drift and prohibited software across 30 days of snapshots.
- **Action proposals:** proposes remediation tickets, upgrade orders, replacement flags, software removal and employee notifications. Every proposal needs supporting evidence and explicit human approval.
- **Guardrails:** tenant isolation enforced in code, policy-driven tool allowlist, evidence-gated actions, deduplication, and an append-only audit log.

## Design principles

1. **Telemetry is the only source of truth.** Every claim cites the snapshots it came from, and weak signals are labeled as weak.
2. **Copilot, not autopilot.** The agent proposes, a human approves, and the audit log records who decided.
3. **No evidence, no action.** Every proposal must be backed by evidence retrieved in the current session.
4. **The tenant boundary comes before capability.** A session can never see another company's data.
5. **Notice what changed, not just what is.** Detectors look across the full telemetry window and report only real patterns.
6. **A person is behind every device.** Notifications use neutral templates, and network and hardware identifiers stay away from the LLM.
7. **A guarantee is only as good as its test.** Every guardrail has an evaluation case that must pass.

See [docs/PHILOSOPHY.md](docs/PHILOSOPHY.md) for the reasoning behind these principles and the trade-offs they involve.

## Planned architecture

- **Python + LangGraph**: chat runs as an agent: input guard → planner (explicit plan first) → ReAct tool loop → verifier → responder, plus a policy check → human approval → executor path for actions.
- **`fleet scan`**: a deterministic workflow that runs every detector for one tenant and prints a briefing with pending proposals. Fixed steps run as code; open-ended questions go to the agent.
- **Data**: raw NDJSON telemetry is the immutable source of truth; a loader builds a normalized SQLite database.
- **Tools**: typed read tools (device queries, history, compliance, software, insights) and proposal-only action tools.
- **Config**: `policy.yaml` (what the agent may do), `thresholds.yaml` (definitions), `blocklist.yaml` (prohibited software).
- **Evaluation**: unit tests plus an agent eval suite with ground truth computed independently from the raw data.

## Security model

- **Tenant isolation in code:** each session is fixed to one tenant at startup; every query is filtered by it, and other tenants' devices return "not found".
- **Proposals, not actions:** the agent only proposes; every proposal needs evidence from the current session, a precondition checked in code, and human approval.
- **Untrusted telemetry:** device strings are sanitized on load, treated as data, and escaped before display.
- **Minimal data to the LLM:** one tenant at a time, only the fields a question needs; IPs, MACs and hardware IDs are never loaded.
- **Tamper-evident audit log:** hash-chained entries, checked with `fleet audit verify`.
- **No authentication in v1:** whoever runs the CLI chooses the tenant. This and other deliberate limits are listed in the out-of-scope section.

See [docs/SECURITY.md](docs/SECURITY.md) for the threat model, data flow and out-of-scope list.

## Getting started

Setup, usage and evaluation instructions will be added as the implementation lands.

## License

Licensed under the [Apache License 2.0](LICENSE).
