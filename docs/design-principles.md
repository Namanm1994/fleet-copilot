# Fleet Copilot: Design Principles

These are the engineering rules the system is built by. Each one names what it means in practice, where it shows in the code, and what it costs.

`docs/PHILOSOPHY.md` explains the values behind the product (why a copilot and not an autopilot, why the tenant boundary comes first). This document is one level down: the rules that decide how a component is written. Where the two overlap, PHILOSOPHY.md gives the reason and this document gives the rule.

## Summary

| # | Principle | In one line |
|---|---|---|
| 1 | The model proposes; code decides | Nothing true or safe depends on the model behaving |
| 2 | Safety lives in structure, not in the prompt | A rule the model could talk its way past is not a rule |
| 3 | An agent only where the steps are unknown | Fixed steps are a workflow |
| 4 | Tools compute; the model reports | The model never counts, sums or compares dates |
| 5 | One function decides each thing | Fix a problem once, where it starts |
| 6 | Every fact has a receipt | A claim cites evidence a tool returned in this session |
| 7 | "Don't know" is a typed answer | Nothing found, cannot tell and failed lookup are three different results |
| 8 | Everything is bounded | Tokens, time, tool calls, retries, batch size, context |
| 9 | Fail closed | When unsure, stop or refuse |
| 10 | Same input, same output | No system clock, temperature 0, fixed steps where possible |
| 11 | The checker inside is not the judge outside | The verifier triggers rewrites; the evals never do |
| 12 | Record what produced a result | A result without its model and settings cannot be compared |
| 13 | Say what is unverified | Unmeasured values and untested paths are labelled as such |

## 1. The model proposes; code decides

The model chooses what to look up, in what order, and how to word the answer. Code reads the data, detects problems, checks the answer, decides whether an action is allowed and carries it out.

- **In the code:** detectors, verifier, policy checks, unprompted proposals and the executor contain no model call.
- **Test:** for any guarantee, ask "does this hold if the model does the worst possible thing?" Tenant isolation, approval and evidence for actions all pass that test.
- **Cost:** the system only finds problems someone wrote a rule for.

## 2. Safety lives in structure, not in the prompt

A prompt instruction is a request. A missing capability is a fact.

- The company is not a tool argument, so the model has no field through which to ask for another company's data.
- Lock and wipe are not offered as tools, so no wording can invoke them.
- Action tools create proposals; a separate component executes, and only after a person approves from an interactive terminal.
- Tool results reach the model marked as data. An injected instruction can at most change how a sentence is worded.
- **Cost:** less flexibility. Adding a capability means changing code and configuration, not a prompt.

## 3. An agent only where the steps are unknown

An agent is the right tool when the next step depends on what the last one found. When the steps are known in advance, a workflow is cheaper, repeatable and testable.

- **In the code:** chat is an agent (plan, tool loop, check). The scan is fixed steps with no planner and no loop, and needs no model.
- **Also:** one agent, not several. More agents add ways to fail without a matching benefit for this task.
- **Cost:** two code paths to maintain.

## 4. Tools compute; the model reports

Models are unreliable at counting rows, arithmetic and date comparison. So no answer should need the model to do them.

- A device's history comes with a summary computed in code: number of points, passes and fails, first and last failure, minimum and maximum.
- Queries return totals and a receipt of the whole numbers in the result.
- Findings come from detectors, with their evidence and caveats attached.
- **How this rule was learned:** in a live run the model counted rows itself and got it wrong. The verifier rejected the answer correctly. The fix was to give the model the count, not to relax the check.
- **Cost:** tool outputs grow, and every new kind of question may need a new computed field.

## 5. One function decides each thing

When the same decision is made in two places, the two will disagree. Each decision has one owner.

| Decision | Single owner |
|---|---|
| How a device is named in output | The renderer adds the ID after a hostname |
| What kind of number a figure is | One labelling function in the verifier: date, measurement, count or plain value |
| What a software entry contains | One builder shared by both lookup routes |
| Whether an action is allowed | The policy engine, at proposal time and again before execution |
| What "now" is | The session's as-of date |

- **Consequence for fixes:** when something fails, find the one place the decision is made and fix it there. Do not patch the symptom, and do not loosen a test to fit the output.
- **Cost:** a fix sometimes takes longer, because the right place has to be found first.

## 6. Every fact has a receipt

An answer is a set of claims, and each claim cites evidence IDs that a tool returned in this session. The verifier checks IDs, device names, numbers, counts, dates and stated thresholds against that evidence before anything is shown.

- History between turns is a pointer to what was discussed, never a source of facts. A fact from an earlier turn must be fetched again.
- **Limit:** the verifier checks facts, not meaning. It cannot tell whether a correctly cited number is described with the wrong word.
- **Cost:** answers are structured and less free in form, and a strict check sometimes forces a rewrite of a correct answer.

## 7. "Don't know" is a typed answer

Three situations look alike and must not be mixed:

| Situation | Result |
|---|---|
| The query ran and found nothing | `no_matches` |
| The question cannot be settled from the data | `insufficient_data`, with what is available |
| The lookup failed | A typed error; the model is told not to guess |

An ambiguous reference gets a question back, not a guess. An out-of-scope request gets a refusal that says what the agent can do.

- **Limit today:** a partial gap (most items assessed, a few not) is a free-text caveat. Making it a structured part of the answer is future work.

## 8. Everything is bounded

Any loop that a model drives needs a limit that code enforces.

- Per call: output tokens, timeout.
- Per turn: input and output tokens, tool calls, verifier revisions, repair attempts, a deadline that wins over retries.
- Per session and per company per day: input tokens.
- Per request: devices in one action, unprompted proposals.
- Context: recent turns kept, size of older history, size of one tool result.

When a limit is reached the turn returns what it has and says why it stopped.

- **Cost:** a limit set too low cuts off a valid answer. All current values are starting points, not measurements.

## 9. Fail closed

When something is wrong or unknown, the safe outcome is the default.

- A missing or invalid policy file stops the CLI.
- An unknown company stops the session before any data is read.
- A row that does not match the session's company is an error, not a filtered result.
- A piped (non-interactive) approval is refused.
- A precondition that no longer holds stops execution.
- **Cost:** some legitimate use is blocked until configuration is fixed.

## 10. Same input, same output

A result that cannot be reproduced cannot be debugged or trusted.

- "Now" is the newest snapshot in the data or an explicit date, never the system clock.
- Temperature is 0. Detectors, verifier and policy are plain code.
- The raw data is never changed; the database is derived from it and can be rebuilt.
- Recorded model replies let the evals run offline with identical results.
- **Limit:** a live model is still not fully repeatable. Live evals run each case three times and a case passes only if every run passes.

## 11. The checker inside is not the judge outside

Two different things check an answer, and they are kept apart.

| | Verifier | Evals |
|---|---|---|
| Runs | Inside the agent, on every answer | Outside, in testing |
| Knows the right answer | No; only what the tools returned | Yes; from an independent implementation over the raw data |
| Can trigger a rewrite | Yes | Never |

- No retry is driven by an eval, and a failed case is not re-run until it passes. Either would make the suite measure persistence, not correctness.
- Evals are scored by code, not by a model.
- When an eval catches something the verifier missed, the response is to make it detectable by the verifier or impossible by construction, not to add a second chance.
- The oracle shares no code with the agent, so one bug cannot hide in both.

## 12. Record what produced a result

Every session and every eval result carries the model, its settings, the limits in force and the prompt version, with a short fingerprint. A report flags when it is compared with a run made under different settings.

- Every plan, tool call, verifier result and decision is traced. Proposals, approvals, rejections and executions go to an append-only audit log in which each entry holds the hash of the one before.
- **How this rule was learned:** an early report compared two runs made with different models and showed the difference as a regression.

## 13. Say what is unverified

A document that claims more than was tested loses the reader's trust in everything else it says.

- The README states how many eval cases have actually run against a model and which have not.
- Limits and thresholds are labelled as starting values where they were not measured.
- Known weaknesses are listed in `architecture.md`, not left for a reviewer to find.
- What is out of scope (sign-in, roles, real integrations) is named, with the boundary that still holds without it.

## When principles conflict

| Conflict | Choice |
|---|---|
| Strict checking versus fluent answers | Strict checking. A rejected correct answer costs a rewrite; an accepted wrong one costs trust |
| Reliability versus flexibility | Reliability. Rules are in configuration and applied by code |
| Showing everything versus not misleading | Not misleading. For a fleet-wide question, devices that could not be assessed are given as a count, not listed beside real findings |
| Passing the eval versus being right | Being right. The eval is changed only when it tests wording that does not matter |
| Finishing features versus proving the ones built | Proving. An untested guarantee is a claim |
