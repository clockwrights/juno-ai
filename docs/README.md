# Juno — Design Documentation

**Stack:** Python 3.12, AWS CDK (Python), GitHub Actions (checks only), Together AI (OpenAI-compatible API), Telegram.

This folder is the design of the system as it is specified today. Documents marked **normative** define required behaviour: code that disagrees with them is a bug. Documents marked **informative** explain or estimate. Implementation progress is in the root [README](../README.md).

New here? Read [02-architecture.md](02-architecture.md) §0 for the system on one page, then [01-principles-and-layers.md](01-principles-and-layers.md).

## Documents

| # | Document | Kind | What it answers |
|---|---|---|---|
| 01 | [Principles and layers](01-principles-and-layers.md) | normative | Why the system is shaped this way; the three layers; glossary |
| 02 | [Architecture](02-architecture.md) | normative (§5 informative) | Every component and flow, with diagrams; cost estimate |
| 03 | [Contracts](03-contracts.md) | normative | Exact payloads, tool schemas, events, state keys |
| 04 | [Memory](04-memory.md) | normative | Tiers, schema, retrieval, decay, consolidation, concurrency |
| 05 | [Agent runtime](05-agent-runtime.md) | normative | Conversation loop, heartbeat, diary, episodes, notes, model settings |
| 06 | [Enforcement layer](06-enforcement-layer.md) | normative | Gates, governor, model gateway, sandbox, IAM, budgets, alarms |
| 07 | [Supervisor](07-supervisor.md) | normative | Observer design, rubric, pause, watcher alarms |
| 08 | [Self-modification](08-self-modification.md) | normative | Proposals, CI, deploy, canary, rollback, invariants, approvals |
| 09 | [Data model](09-data-model.md) | normative | Tables, keys, indexes, buckets, prefixes, parameters |
| 10 | [Repository layout](10-repository-layout.md) | normative | Directory ownership, CODEOWNERS, branch protection, tooling |
| 12 | [Testing](12-testing.md) | normative (§8 informative) | Test pyramid, fakes, CI jobs, what gates what |
| 14 | [Operations](14-operations.md) | normative | Runbook: deploy, pause, resume, rollback, budgets, secrets, lifecycle |
| 15 | [Control panel](15-control-panel.md) | normative | Local web UI: activity, sessions, chat, memory, prompts, proposals, logs, exports |

Numbers are stable identifiers (code comments cite them as `docs/NN §x`), so the sequence has gaps.

## Conventions

- **MUST / SHOULD / MAY** have their RFC 2119 meanings.
- `code` names refer to a file, resource, key or identifier that appears verbatim in the implementation.
- "Agent" means Layer 1 code and prompts. "Gate" means an immutable Layer 3 Lambda that mediates a side effect. "Supervisor" is Layer 2. "User" is the one person the agent serves, who also deploys and operates it.
- Resource names are shown without stage prefix. Real names are `<stage>-<name>` (e.g. `prod-agent-conversation`).
- Every payload that crosses a Lambda, queue or event boundary has a JSON Schema in `contracts/` and is validated on both sides. Docs quote the schema; the file wins if they disagree.

## Maintenance

- A change to a contract MUST update `contracts/`, the relevant doc, and the contract tests in the same PR, with a new `schema_version`.
- These documents describe the current design only. When the design changes, the text is updated in place.
