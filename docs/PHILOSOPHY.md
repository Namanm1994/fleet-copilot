# Philosophy of the Fleet Copilot

This document explains the principles behind Fleet Copilot's design and the concrete decisions each one leads to. A principle that doesn't change a decision is decoration, so every principle below names the decision it caused.

## Principles for the Fleet Copilot

### 1. Telemetry is the only source of truth
The copilot answers from the telemetry and nothing else. Every claim points to the snapshots it came from, and when the data can't support an answer, the agent says so.

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
