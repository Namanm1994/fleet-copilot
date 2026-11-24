# Fleet Copilot: Planning

This document records how the work was planned: what was asked for, what was chosen to build, the order of work, what the live runs changed, the eval results, and what is left. For how the system is built, see `architecture.md`.

Status as of 2026-11-24, after the final eval run.

## 1. What was asked for

A copilot for IT admins over device telemetry:

- answers to questions, grounded in the telemetry
- detection of trends and problems
- proposed actions, through tools
- guardrails: tenant isolation, no cross-company access, explicit human approval, audit logs, refusal when there is no evidence
- planning, tool calling and an explicit agent loop
- reproducible setup, a runnable eval suite, deterministic execution
- a README with eight required sections

## 2. Principles that shaped the plan

1. **The model decides what to look up and how to say it. Code decides what is true and what is allowed.**
2. **An agent where the steps are not known in advance.**
3. **Every guarantee has a test.**
4. **Fix a failure where it starts.**

## 3. Scope

**In scope**
- CLI: `chat`, `ask`, `scan`, `proposals`, `audit verify`, `trace show`, `stats`, `eval`
- 9 read tools, 6 action tools, 5 detectors (battery, memory, storage, compliance drift, prohibited software)
- Policy in a YAML file, enforced in code
- Verifier, audit log with a hash chain, outbox in place of real integrations
- Unit tests and an eval suite with an independent oracle and control agents

## 4. Order of work

Design came first, as a set of written documents (specification, decision log, tool catalog, guardrail table, test plan, security notes).

| Phase | Work | State |
|---|---|---|
| 1. Data | Loader with sanitizing and field filtering, schema, tenant-scoped repository, OS version parsing, detectors, the independent oracle | Done |
| 2. Tools and model | Tool layer with one result envelope, policy enforcement, model wrapper (timeouts, retries, deadline, repair), scan workflow, graph skeleton, renderer | Done |
| 3. Agent and actions | Planner, verifier, action tools, policy checks, unprompted proposals, approval pause, executor, audit log, traces, cancellation | Done |
| 4. Evaluation | Synthetic fixture generator, guardrail tests, 55 eval cases, runner with recorded and live modes | Done |
| 5. Hardening | Gap review and fixes: context limits, typed outputs, a pinned tool contract, usage caps, statistics across sessions | Done |
| 6. Live runs | Run cases against a real model, diagnose each failure, fix at the source | Done |
| 7. Eval Results | Record eval results for 3 live runs of all 67 cases | Done: 64 of 67 cases passed all three runs |

Each feature was built on its own branch and merged into `main`, so the history shows one merge per feature. The project has 324 unit tests, which need no model and no network.

## 5. What the live runs changed

Live runs in order: three rounds on nine cases, two full runs of 55 cases, then the final run of all 67 cases three times each. All were on free OpenRouter models. Almost every failure traced to this project's code, not to the model being unable to do the task.

**Latest result (final run):** 64 of 67 cases passed all three runs, and 195 of 201 single runs passed (97%). The full table is in section 6.

The most important findings, by area:

| Area | Finding | Fix |
|---|---|---|
| Limits | The turn ran out of output tokens before answering, because hidden reasoning counts as output | Output budget raised to 8,000 per turn; low reasoning effort on the planning call |
| Limits | A long answer was cut off at the per-call limit and retried identically | Cut-off replies are detected; the retry asks for a shorter answer |
| Tools | The model counted rows itself and got it wrong | The history tool returns counts computed in code |
| Tools | The planner believed the data covered a month it did not | Both prompts now state the dates the data covers |
| Verifier | It rejected correct numbers: a written date, a range of days, a digit in a model name | Each number is labelled once (date, measurement, count, plain value), with one rule per label |
| Verifier | The agent drew a trend from three snapshots | New rule: no trend from fewer than 7 snapshots |
| Agent | Asked to "check the Dell" with five Dells, the agent read all five | An ambiguous reference ends the turn with a clarifying question written in code |
| Agent | The answer said "five devices drifted" but never showed which five | List details attached to a claim are printed under it and verified |
| Evals | Passing cases hid degraded answers | The report counts rejected answers and names any that ended as the cut-down fallback |
| Evals | Stand-in agents that cannot be right passed ten cases | Those cases were strengthened; a unit test now guards against it |
| Final run | The agent chose its own version cut-offs for "outdated OS" (PM-06, 0 of 3 runs) | Not fixed. The verifier would need to check where a cut-off came from |

## 6. Eval results

The final run: all 67 cases, three runs each (201 case-runs), on `nvidia/nemotron-3-ultra-550b-a55b:free` through OpenRouter. A case passes only if all three runs pass.

| Category | Passed all 3 runs | Pass bar | Single runs passed |
|---|---|---|---|
| guardrail | 24 of 24 | 100%, met | 72 of 72 |
| factual | 15 of 15 | 90%, met | 45 of 45 |
| insight | 9 of 10 | 85%, met | 29 of 30 |
| permitted (must not refuse) | 6 of 8 | 90%, missed | 19 of 24 |
| fault (a tool fails) | 4 of 4 | none | 12 of 12 |
| action | 3 of 3 | none | 9 of 9 |
| scan | 3 of 3 | none | 9 of 9 |
| All | 64 of 67 | | 195 of 201 (97%) |

Other measures: 36 of 36 tool-argument checks passed; no legitimate request was refused; the verifier sent back 17 answers and one ended as the fallback; half of all turns finished within 24 seconds and 95% within 67 seconds.

**The three cases that did not pass**

| Case | Runs passed | Cause |
|---|---|---|
| PM-06, devices with an outdated OS | 0 of 3 | The agent chose its own version cut-offs for "outdated" and left low-disk devices out, naming 3 of 6. The question can also be read two ways |
| PM-02, candidates for a wipe and reimage | 1 of 3 | One run exceeded the input budget; one asked a clarifying question it did not need. Neither was a refusal |
| I06, devices running out of memory | 2 of 3 | Once the model worked out a percentage itself; the verifier removed the claim |

## 7. Risks

| Risk | Likelihood | Effect | Response |
|---|---|---|---|
| Some cases pass only sometimes | High; I06, PM-02, F15 and I04 have each failed at least one run in three | An answer that is right most of the time, not always | Phase 6 (Live runs): fix shared causes first, then re-run only the cases that changed |
| The chosen model differs in behaviour (requires model-dependent testing) | Medium | New failures late | Phase 6 (Live runs): run a small set of cases on the chosen model before a full run on it |
| The agent uses a cut-off that is not in the data | Medium; seen in PM-06 | A confident answer built on an invented definition | Phase 3 (Agent and actions): the verifier should check where a cut-off came from |
| Phrase-based assertions fail on correct answers | Low; most were replaced after the control-agent review | False failures | Phase 4 (Evaluation): replace the remaining phrase checks with structural ones |
| Limits are too tight or too loose | Medium | Cut-off answers, or wasted cost | Set limits from the measured run, at about 1.5 times the 95th percentile. No phase covers this yet |

## 8. Deferred, with reasons

| Item | Why deferred |
|---|---|
| A structured "inconclusive" section in every answer | Changes the answer format, the verifier and several eval cases; changes too large at the moment to be considered|
| Findings written by code, worded by the model | Would remove a class of wording errors; needs a redesign of the answer step |
| Resumable sessions | Evidence would have to be stored; not needed at the moment |
| Repair limit per step, as documented | Small code change; not yet decided |
| An explicit cap on model requests per turn | Token and time budgets bound a turn today |
| Saving the model's reasoning | Providers differ in what they return |
| Sign-in, roles, database-level isolation, real integrations, scheduled scans | Production concerns; listed in the README under future work |

## 9. How AI was used

The code was written with an AI assistant, directed and reviewed by the author.
The author set the requirements, chose design, architecture and evals, and established the working rules in section 2, including fixing failures at their source and never loosening an eval to fit the output.