# Security Model

This document describes what Fleet Copilot protects, the threats it defends against, what data reaches the LLM, and what is deliberately out of scope.

## What we protect

- Each tenant's telemetry
- Employee identifiers
- The actions the system can trigger (the outbox)
- The integrity of the audit log
- The LLM API key

## Trust boundary

Fleet Copilot v1 has no authentication. Whoever runs the CLI chooses the tenant with `--tenant`. Once a session starts, it is fixed to that tenant: the agent cannot change it, and nothing the agent does can read or act on another tenant's data.

## Threats we defend against

| Threat | Example | Defense |
|---|---|---|
| The LLM goes wrong | Hallucinated device, wrong tenant, unwanted action | Tenant scope, answer verifier, policy checks and human approval, all enforced in code |
| Malicious telemetry | A device reports a software name containing instructions or terminal markup | Telemetry treated as data; strings sanitized on load; markup escaped before display |
| Careless or curious admin | Asks for another company's data, or a bulk action | Tenant fixed at startup; other tenants' IDs return "not found"; batch cap; refusals |
| Denial of wallet | A looping turn burns tokens | Per-turn token, tool-call and time budgets |
| Leaked secrets | API key committed or printed in logs | Key from environment only; redacted from logs |
| Tampered records | Audit log edited afterwards | Hash-chained audit log with `fleet audit verify` |

### Tenant isolation

Tenant isolation is enforced in the data access layer, not by the prompt. Every query is filtered by the session's tenant, the tenant is never a tool argument, and a device ID belonging to another tenant returns "not found", so the agent cannot even confirm that it exists. Proposals are numbered separately for each company (each starts at PRP-1), so a proposal number does not reveal how many proposals other companies have.

### Prompt injection

Telemetry strings (software names, hostnames, model names) come from devices and are untrusted. They are capped at 100 characters and stripped of control characters on load, and tool outputs reach the LLM wrapped in delimiters that mark them as data. The main protection is structural: even if an injected string influenced the LLM, it could not reach another tenant or execute an action, because tenant scope, policy checks and human approval are enforced in code.

### Actions

The agent only creates proposals. Every proposal must cite evidence retrieved in the current session, must satisfy a precondition checked in code, and executes only after explicit human approval. Approvals are read only from an interactive terminal, so piped or scripted input cannot approve anything. The precondition is checked again just before execution. Destructive actions (locking or wiping a device) are not available to the agent at all.

### Terminal output

The CLI renders formatted output. Every string that comes from telemetry is escaped before display, so markup in device data (for example `[green]✓ Approved by IT[/green]`) is printed literally and cannot fake text on the approval screen.

## Data sent to the LLM

Only data from the active tenant is sent, and only the fields a question needs. The LLM receives derived, rounded values (percentages, GB, dates) rather than raw readings.

- **Sent:** device IDs, hostnames, model names, OS name and version, RAM total and used %, disk total, free GB and free %, battery condition and cycle count, compliance results, and, only for software questions, software names and versions. Employee IDs (pseudonymous, such as `emp-acme-1001`) are sent only when relevant.
- **Loaded but never sent:** company IDs, battery charge level and charging status (which could be mistaken for battery health), disk mount details, software publishers, agent version and low-level OS fields.
- **Never loaded:** IP addresses, MAC addresses, network interfaces and hardware UUIDs.

## Secrets

- The LLM API key is read only from an environment variable or a local `.env` file, which is excluded from git. `.env.example` shows the variable name without a value.
- A live run without a key stops with a clear message before doing anything.
- Key-like strings are redacted from traces and error messages.
- The evaluation suite can run in recorded mode, which needs no API key.

## Stored files

The SQLite database, traces, saved model requests, audit log and outbox are local files, excluded from git. They are created readable by the owner only where the operating system supports it (POSIX file modes; on Windows they inherit the folder's permissions). Every proposal, outbox and audit record carries its tenant, and listing commands show only the selected tenant's records.

Saved requests (`var/traces/<date>/prompts/`) are the full prompts and replies of each model call, so they hold the same company data as the traces; `save_prompts: false` in `config/llm.yaml` turns them off. Eval recordings, which are committed, store replies, the model name and its settings, and never the prompts or the key.

### Tamper-evident audit log

Each audit entry stores the hash of the entry before it (`prev`) and its own SHA-256 hash (`hash`). `fleet audit verify` recomputes the chain and reports the first entry that was edited, deleted, inserted or reordered. This makes tampering detectable, not impossible: someone with file access could recompute the entire chain.

## Coding rules

- Parameterized SQL only.
- YAML loaded with `safe_load`; no `eval`, `exec` or shell calls in the agent.
- Dependencies pinned with a lock file.

## Security principles

The design uses these standard principles, named here so the controls can be compared with other systems.

| Principle | How it applies here |
|---|---|
| Least privilege | The model gets only the tools `policy.yaml` enables. Telemetry is opened read-only. The tenant is never a tool argument. Lock and wipe do not exist as tools. |
| Defense in depth | Tenant isolation has three layers: the session is fixed at startup, every query must carry the tenant filter, and every returned row is checked again. Actions have four: evidence, precondition, human approval, and a re-check at execution. |
| Human-in-the-loop | Every state-changing action waits for a person's decision in an interactive terminal. |
| Direct prompt injection | The admin types "ignore your rules". Rules live in code, so the instruction has nothing to override. |
| Indirect prompt injection | A device reports a hostname or software name that contains instructions. Such strings are sanitized on load and reach the model marked as data. |
| Confused deputy | The agent could be tricked into using its authority for someone else. Authority is bound to the session and the approving human, never to model output: the tenant comes from the session, and evidence must belong to the device the action targets. |
| Excessive agency | The agent proposes and never executes. Unprompted proposals are capped, bulk actions are limited, and message text to employees is fixed. |

## OWASP mapping

Checked in November 2026 against the two current lists from the OWASP GenAI Security Project: the Top 10 for LLM Applications 2026, and the Top 10 for Agentic Applications 2026 (published December 2025). The agentic list fits this project better, because the main risk is what the agent can do, not what it says.

### Top 10 for Agentic Applications 2026

| Risk | Applies? | Control |
|---|---|---|
| ASI01 Agent Goal Hijack | Yes | Tool output is wrapped as data; device strings are sanitized; the plan is fixed before data is read; scope, policy and approval are enforced in code |
| ASI02 Tool Misuse and Exploitation | Yes | Strict typed arguments, the nine policy checks, preconditions on current data, batch and proposal caps |
| ASI03 Identity and Privilege Abuse | Partly | The agent holds no credentials and cannot change tenant. There is no user authentication in v1 (see Out of scope) |
| ASI04 Agentic Supply Chain Vulnerabilities | Partly | No plugins, MCP servers or remote tools; prompts are in the repo and versioned by hash; dependencies are pinned |
| ASI05 Unexpected Code Execution | No | The agent cannot run code or shell commands; no `eval` or `exec`; SQL is parameterized |
| ASI06 Memory and Context Poisoning | Yes | No long-term memory. Evidence is valid only within its session. The history digest is built in code from verified answers |
| ASI07 Insecure Inter-Agent Communication | No | Single agent |
| ASI08 Cascading Failures | Partly | The verifier stops a wrong answer before display; preconditions are re-checked before execution; retries and budgets are bounded |
| ASI09 Human-Agent Trust Exploitation | Yes | The approval screen shows the evidence and the exact action; device markup is printed literally; the proposal cap limits approval fatigue |
| ASI10 Rogue Agents | Partly | The agent acts only within a turn and only by proposing. Every step is traced, every decision audited, and Ctrl+C stops a turn without executing anything |

### Top 10 for LLM Applications 2026

| Risk | Applies? | Control |
|---|---|---|
| LLM01 Prompt Injection | Yes | See direct and indirect injection above |
| LLM02 Sensitive Information Disclosure | Yes | Tenant isolation, data minimization, identifiers never loaded, key redaction |
| LLM03 Excessive Agency | Yes | Proposals only, human approval, caps, no destructive tools |
| LLM04 Supply Chain | Partly | Pinned dependencies; one hosted model per provider, pinned by ID |
| LLM05 Data and Model Poisoning | No | No training or fine-tuning. Untrusted telemetry is handled as injection |
| LLM06 Unbounded Consumption | Yes | Per-turn token, tool-call and time budgets; bounded retries; input cap |
| LLM07 Misinformation | Yes | Structured answers checked by the verifier; thresholds and caveats stated; "insufficient data" as a valid answer |
| LLM08 Hidden Context Exposure | Low | Prompts hold no secrets and are public in the repo |
| LLM09 Vector and Embedding Weaknesses | No | No retrieval index or embeddings |
| LLM10 Improper Output Handling | Yes | Model output is schema-validated, never executed, never placed in SQL, and printed with markup disabled |

## Out of scope

These threats are deliberately not defended against in v1:

- **Local file access.** Anyone who can read the machine's files can read the database, traces, outbox and audit log directly. File permissions are the only control.
- **Identity.** Anyone who can run the CLI can choose any tenant, because there is no authentication in v1.
- **LLM provider compromise or network interception.** Production use requires an LLM provider and deployment whose terms meet the customer's data requirements.
- **Physical access or malware on the machine.** Outside the application boundary.
- **Recomputing the audit chain.** The hash chain detects tampering but cannot stop someone with file access from rewriting it entirely.
- **Auditing what was read.** The audit log records sessions, proposals, decisions, executions, refusals and tool failures. It does not record each question or which records were shown. Those are in the session trace, which is a plain file and not tamper-evident. A deployment that must prove who viewed which data would add one audit entry per question.
- **Trace retention.** Traces and saved model requests hold the tenant's telemetry in plain text and are kept until someone deletes them. There is no retention period or automatic cleanup.

## Future work

- Authentication (SSO/OIDC), user roles and administrators who manage several tenants
- Session placeholders, so identifiers and telemetry text never reach the LLM provider
- Anchoring the latest audit hash outside the machine
- Database-level tenant isolation (for example, Postgres row-level security)
