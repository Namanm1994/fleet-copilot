# Fleet Copilot

An agentic AI copilot that lets IT administrators query fleet telemetry in natural language, surface insights and trends, and propose evidence-backed operational actions that a human approves before anything runs.

> **Status:** early development. The design is settled; implementation is in progress.

## What it does

- **Grounded Q&A:** answers fleet health and compliance questions from telemetry only, citing evidence for every claim.
- **Insights and trends:** detects battery end-of-life, sustained RAM pressure, low storage, compliance drift and prohibited software across 30 days of snapshots.
- **Action proposals:** proposes remediation tickets, upgrade orders, replacement flags, software removal and employee notifications. Every proposal needs supporting evidence and explicit human approval.
- **Guardrails:** tenant isolation enforced in code, policy-driven tool allowlist, evidence-gated actions, deduplication, and an append-only audit log.

## Design principles

1. **LLM for judgment, code for truth.** Detectors, verification and policy checks are deterministic code; the LLM plans and explains.
2. **Autonomy scales with reversibility.** Reads are free, proposals are checked, execution needs a human, and destructive actions are never available.
3. **Every claim has a receipt.** Answers carry evidence IDs that trace back to individual telemetry snapshots.
4. **The tenant boundary comes before capability.** A session can never see another company's data.
5. **"Nothing needs attention" is a valid answer.** The copilot prioritizes rather than alarms.

## Planned architecture

- **Python + LangGraph**: a single agent graph with input guard → planner → tool loop → verifier → responder, plus a policy check → human approval → executor path for actions.
- **Data**: raw NDJSON telemetry is the immutable source of truth; a loader builds a normalized SQLite database.
- **Tools**: typed read tools (device queries, history, compliance, software, insights) and proposal-only action tools.
- **Config**: `policy.yaml` (what the agent may do), `thresholds.yaml` (definitions), `blocklist.yaml` (prohibited software).
- **Evaluation**: unit tests plus an agent eval suite with ground truth computed independently from the raw data.

## Getting started

Setup, usage and evaluation instructions will be added as the implementation lands.

## License

Licensed under the [Apache License 2.0](LICENSE).
