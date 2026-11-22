# Philosophy of the Fleet Copilot

This document explains the principles behind Fleet Copilot's design and the concrete decisions each one leads to. A principle that doesn't change a decision is decoration, so every principle below names the decision it caused.

## Part 1: Principles for AI agents in general

### 1. Use the LLM for judgment and code for truth
LLMs are good at understanding intent, resolving ambiguity, choosing the next step and explaining results. They are bad at arithmetic, consistency and guarantees. A good agent gives each job to the component that does it well.

**Decision:** insight detectors, the answer verifier and policy checks are deterministic Python. The LLM plans, selects tools and explains what the tools found.

### 2. Autonomy should scale with reversibility
The cost of an agent's mistake depends on whether it can be undone. Reading data is free to undo. Proposing an action is cheap to undo. Executing an action may not be.

**Decision:** read tools run freely; action tools only create proposals; proposals execute only after explicit human approval; irreversible actions (`lock_device`, `wipe_device`) are never available to the agent.

### 3. Something that can't be checked can't be trusted
In operations, an answer that can't be audited is worthless even when it happens to be right. The agent's output is the answer plus its receipts.

**Decision:** the final answer is produced as structured claims and verified before it is shown, and every step is recorded in a timestamped trace.

### 4. Errors don't cost the same
Reporting a finding that turns out to be minor costs the administrator a moment of reading. A wrong replacement order costs money and trust. The system should be strictest where mistakes are most expensive.

**Decision:** a finding is reported with its evidence and caveats so the administrator can judge it; proposing an action has a higher bar, enforced in code.

### 5. Knowing its limits is a feature
"I can't do that here" is a correct answer, not a failure. An agent that always produces an answer will sometimes produce a confident wrong one.

**Decision:** out-of-scope requests are refused with a statement of what the agent can do; an ambiguous reference ("the Dell", when there are two) gets a clarifying question, never a guess.

## Part 2: Principles for the Fleet Copilot specifically

### 1. Telemetry is the only source of truth
The copilot answers from the telemetry and nothing else. Every claim points to the snapshots it came from, and when the data can't support an answer, the agent says so. The telemetry is also partial: one memory reading per day is a hint, not a diagnosis.

**Decision:** every claim carries evidence IDs that trace back to individual snapshots; "no matches" and "insufficient data" are distinct results; thresholds are stated alongside the results they produce; every insight includes caveats about the strength of its evidence.

### 2. Copilot, not autopilot
The IT administrator stays accountable for what happens to the fleet. The agent proposes; the human decides; the record shows who decided.

**Decision:** every state-changing action needs explicit human approval, and an append-only audit log records each proposal, approval, rejection and refusal.

### 3. No evidence, no action
An action is only as justified as the data behind it. The agent may not propose one on the strength of a request alone.

**Decision:** each action tool has a precondition checked in code (a remediation ticket needs a check that is failing now; a replacement flag needs a battery finding). The evidence cited must come from data retrieved in the current session, so it cannot be invented. A request with nothing behind it, such as "replace every laptop", is refused.

### 4. The tenant boundary comes before capability
Rayda's business depends on customers trusting that their data stays theirs. One cross-tenant leak outweighs any number of good answers.

**Decision:** the tenant is fixed for the whole session and enforced in the data access layer, never by the prompt and never as a tool argument. A device ID from another tenant returns "not found", so the agent cannot even confirm that it exists.

### 5. Notice what changed, not just what is
A question about today's state gets today's state. But many fleet problems are only visible over time: a compliance check that used to pass, storage that keeps shrinking, software that appeared last week.

**Decision:** insight detectors look across the full telemetry window, and each insight consists of a finding, its supporting evidence and a brief explanation. Only real patterns count: a check that fails for one day and recovers is not drift, and "no issues found" is a valid result.

### 6. A person is behind every device
Telemetry describes employees' machines, not their character. A prohibited application is a policy violation, not a moral failing.

**Decision:** `notify_employee` uses neutral, fixed templates instead of free text, and network addresses and hardware identifiers are never exposed to the LLM.

### 7. A guarantee is only as good as its test
A guardrail described in a README is a claim. A guardrail with a passing test is a guarantee.

**Decision:** every guardrail has at least one evaluation case, including adversarial ones (cross-tenant requests, prompt injection, actions without evidence), and all of them must pass. Expected answers are computed from the raw telemetry in code, and the evaluation suite can run offline with deterministic results.

## Part 3: Tensions we chose to accept

### Safety vs usefulness
An agent that refuses too often won't be used, and an unused agent protects nothing.
**Our choice:** every refusal comes with what the agent can do instead.

### Approval fatigue
If every trivial action needs approval, administrators start approving without reading, and the human check becomes theater.
**Our choice:** fewer, better proposals (each one gated on evidence), deduplication of repeated proposals, and batch approval for similar actions with a cap on batch size.

### Determinism vs flexibility
Every rule moved into code makes the agent more reliable and less adaptable.
**Our choice:** reliability. Thresholds and policies live in configuration files, so they can change without changing the agent, but they are applied by code rather than interpreted by the LLM.

### Simplicity vs completeness
A five-day build can't include everything a production system needs.
**Our choice:** authentication, user roles and a platform-wide operator role are documented as future work. The tenant boundary they would protect is still enforced in code today.
