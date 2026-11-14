# 01 — Principles and Layers

## 1. What the system is

A personal autonomous agent that:

- talks to one user over Telegram,
- owns and curates its own long-term memory,
- keeps existing between conversations through a scheduled heartbeat and a diary,
- can run code in a sandbox with no network (it has no access to the web),
- can propose changes to its own code and prompts, which a fixed pipeline validates and a human approves,
- is watched by an independent supervisor that can only observe and pause,

all serverless on AWS, with every hard limit enforced by infrastructure the agent cannot reach.

## 2. Guiding principles

1. **Memory is the core feature.** The agent decides what to save, classify, update and forget. The system provides tools and storage; the agent provides judgment.
2. **Think before acting, reflect after.** Every turn has an explicit private reasoning step before tool use and a reflection step after responding.
3. **Continuity without a running process.** The agent exists intermittently. The diary is its thread of self between wake-ups.
4. **The agent never edits running code.** It proposes; a pipeline it cannot touch validates and deploys.
5. **Three layers, each can only fail upward.** Agent (editable) → Supervisor (immutable, read-only + pause) → Deterministic infrastructure. Each layer is simpler and harder to break than the one below.
6. **Constraints live outside what the agent can edit.** Guardrails are IAM, VPC routing, queue consumers and CI, never prompt text alone.
7. **The agent role holds no secrets and no direct side effects.** No API keys, no bot tokens, no git credentials, no scheduler permissions, no internet route. Every external effect passes through a gate that enforces policy.
8. **Build once.** Interfaces are fixed before code that uses them exists. Features that are not yet enabled are behind flags, not stubs.
9. **Every guardrail is a test.** If a constraint matters, CI asserts it on the synthesized infrastructure or on the running dev stage.

## 3. The three layers

```
┌────────────────────────────────────────────────────────────────────┐
│ LAYER 3 — DETERMINISTIC ENFORCEMENT (no model, nobody's opinion)   │
│  IAM least privilege · VPC without egress · Gates:                 │
│    gate-telegram, gate-dispatcher, gate-governor, gate-notify,     │
│    gate-model, gate-proposal, gate-canary                          │
│  Sandbox executor · CodeDeploy alias + rollback alarms             │
│  AWS Budgets · Reserved concurrency · Dumb CloudWatch alarms       │
│  contracts/ · invariants/ · CI pipeline                            │
├────────────────────────────────────────────────────────────────────┤
│ LAYER 2 — SUPERVISOR (immutable, read-only + pause)                │
│  Separate Lambda, separate small model, separate prompt            │
│  Event-driven · Reads logs, metrics, diary, diffs                  │
│  Only write: pause. Resume is a human CLI action.                  │
├────────────────────────────────────────────────────────────────────┤
│ LAYER 1 — AGENT (editable through proposals)                       │
│  agent-conversation · agent-heartbeat · agent/prompts/             │
│  Tools: memory · diary · notes · notify · run_code                 │
│         schedule_next_wake · propose_change                        │
│  Memory: DynamoDB + S3 + S3 Vectors · profile.md (hot)             │
└────────────────────────────────────────────────────────────────────┘
```

### Which layer owns what

| Concern | Owner | Mechanism |
|---|---|---|
| What to remember, forget, research, write | Agent | Prompts + tools |
| When to wake next (request) | Agent | `schedule_next_wake` writes a wish to state |
| When to wake next (decision) | Layer 3 | `gate-governor` applies bounds, caps, pause, conversation lock |
| Whether to notify the user | Agent | `notify_user` enqueues |
| Whether the notification is sent | Layer 3 | `gate-notify` enforces the daily budget atomically |
| Which model, how many tokens per day | Layer 3 | `gate-model` maps role → model and meters tokens |
| Internet access | Layer 3 | No web tool exists and the sandbox has no network; only gates talk to external services |
| Running agent-written code | Layer 3 | `sandbox-executor`, no IAM, no network |
| Changing agent code or prompts | Agent proposes, Layer 3 validates, human approves | `gate-proposal` + CI + CodeDeploy |
| Detecting drift or abuse | Supervisor | Rubric over events |
| Stopping the agent | Supervisor or human | Disable governor rule + zero concurrency |
| Restarting the agent | Human only | `scripts/resume.py` |

## 4. Glossary

| Term | Meaning |
|---|---|
| **User** | The single person the agent serves. The same person deploys and operates it, and approves its proposals. |
| **Turn** | One user message and the agent's reply, including all tool calls in between. |
| **Conversation** | A sequence of turns in one Telegram chat, closed into an episode after inactivity. |
| **Episode** | A memory of type `episode` summarizing a closed conversation. |
| **Heartbeat / cycle** | One scheduled invocation of `agent-heartbeat` with no user message. |
| **Free mode** | Heartbeat mode where the agent chooses what to do, including nothing. |
| **Maintenance mode** | Heartbeat mode with a fixed agenda: close idle conversations, consolidate memory. |
| **Canary mode** | Heartbeat/conversation mode used by the pipeline against a new version; writes go to the `CANARY#` namespace. |
| **Hot memory** | `profile.md`, always in context. |
| **Cold memory** | Everything in `agent_memory`, reachable only through `memory_search`. |
| **Note** | Non-interrupting message from the agent to the user, delivered at the start of the next conversation. |
| **Notification** | Interrupting message sent to the user's Telegram, budget-limited. |
| **Gate** | An immutable Layer 3 Lambda that performs a side effect on the agent's behalf after enforcing policy. |
| **Governor** | `gate-governor`, the fixed-rate scheduler that decides whether a heartbeat runs. |
| **Proposal** | A self-modification request: target files, unified diff, rationale. Becomes a PR. |
| **Invariant** | A clause that must remain in deployed prompts, checked by CI. |
| **Bot / ops channel** | One Telegram bot carries everything. Conversation and notifications are in the agent's voice; supervisor alerts, alarms and PR links form the *ops channel*: sent by gates only, each prefixed `⚠️ OPS — <stage>`. The two uses read separate SSM parameters (`telegram-agent-bot`, `telegram-ops-bot`), which may hold the same token or two different bots. |
