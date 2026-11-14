# 05 — Agent Runtime

The agent layer is two Lambda handlers sharing one runtime library (`agent/runtime/`) and one set of prompts (`agent/prompts/`). This document specifies behavior; [03-contracts.md](03-contracts.md) specifies payloads.

## 1. Runtime library

```
agent/runtime/
  loop.py        # run_turn(): model call ↔ tool execution until end_turn or limits
  context.py     # build_context(): assembles system blocks (stable first, volatile last) and message history
  reflect.py     # run_reflection(): post-response memory decisions
  housekeeping.py# close_idle_conversations(), pending list
  limits.py      # per-invocation counters: model calls, tool calls, wall clock
  prompts.py     # load_prompt(name) from agent/prompts, verifies invariants at cold start
core/
  model_client.py# ModelClient.invoke(role, request) → invokes gate-model; FakeModelClient for tests
  tools/registry.py # tool schemas (from contracts/tools), dispatch table, mode filtering
  storage/       # MemoryStore, DiaryStore, StateStore, ProfileStore, ConversationStore, ArtifactStore, ProposalStore, VectorIndex
```

`loop.py` is a manual tool loop over `ModelClient` (not the SDK tool runner), because the model call crosses a Lambda boundary through `gate-model`. The loop:

1. Sends `system`, `messages`, `tools` for the role.
2. If `finish_reason == "tool_calls"`: executes every entry in `tool_calls` (in parallel where the registry marks the tool read-only), appends one `role: "tool"` message per call with the matching `tool_call_id`, continues.
3. If `finish_reason == "content_filter"` (after the gateway's own single retry): records it, appends a fixed user message explaining the refusal, and ends the turn with a fallback reply. Never retries with a different prompt.
4. If `finish_reason == "length"`: appends a user message "continue" once; a second occurrence ends the turn.
5. Stops on `end_turn`, or when limits are hit (§6).

Tool inputs are always parsed with `json.loads` on the SDK-provided dict, never by string matching.

## 2. Conversation turn

### 2.1 Context assembly and cache layout

Order matters for prompt caching (prefix match). Static first, volatile last.

| Block | Content | Cache |
|---|---|---|
| `tools` | Registry schemas for conversation mode (deterministic order) | part of cached prefix |
| `system[0]` | `agent/prompts/system_conversation.md` (includes invariant clauses verbatim) + `tools.md` | `stable: true` — byte-identical across every call |
| `system[1]` | `profile.md` + last 2 diary entries + pending notes | `stable: true` (changes rarely mid-conversation) |
| `messages` | Transcript of the open conversation, oldest first, including prior `tool_calls` and `role: "tool"` messages unchanged | automatic prefix caching |
| `messages[-1]` (system role) | Per-turn volatile line: date/time, `notifications_remaining`, `tokens_remaining_today` | mid-conversation system message, does not invalidate prefix |

Rules:

- Never put timestamps or budget numbers in a stable `system[]` block. They go in the volatile per-turn block, which the gate places last so the cacheable prefix stays byte-identical.
- Transcript is append-only. Prior assistant `content` is stored as returned, including reasoning, and replayed as-is.
- If the transcript exceeds 150k tokens (estimated with the gate's conservative local heuristic, [06](06-enforcement-layer.md) §5.2), the housekeeping step closes it into an episode and the next message starts a fresh conversation with a one-paragraph recap in `system[1]`.

### 2.2 Steps

1. **Load**: dispatcher has already appended the user message. Handler loads profile (with ETag), diary tail, notes with `delivered_at` absent, transcript.
2. **Think**: the model's reasoning (`reasoning_content`) is the private reasoning step. The system prompt lists the questions to consider (what do I know, do I need to search, is there anything to remember, respond or act). No separate "thinking call".
3. **Act**: tool loop per §1.
4. **Respond**: final text becomes `reply`. Empty text is allowed (silence) and is returned as an empty `reply`; the dispatcher then sends nothing.
5. **Reflect**: a second model call (same role, same cached prefix, plus the assistant reply) with `agent/prompts/reflection.md` as the mid-conversation system message and only memory, profile and note tools available. The reflection prompt asks: what should be saved, updated, forgotten, or moved into the profile; and whether a note for the future is warranted. Reflection has its own limit of 8 tool calls.
6. **Deliver notes**: notes included in context get `delivered_at = now`.
7. **Emit** `conversation.completed` with usage and memory op counts.

The reflection call is the only place the runtime "asks" the agent to think about memory. The agent may of course save memory during the act phase too.

## 3. Heartbeat cycle

### 3.1 Modes

| Mode | Who sets it | Agenda | Model role |
|---|---|---|---|
| `free` | governor (default) | none; the agent chooses | `heartbeat` |
| `maintenance` | governor (weekly cron, or agent wish `schedule_next_wake(reason="maintenance")`) | `close_idle_conversations`, `consolidate_memory` | `heartbeat` for housekeeping, `consolidation` for rewrites |
| `canary` | `gate-canary` | `canary_probe` | `canary` |

### 3.2 Steps (all modes)

1. **Housekeeping (code, not agent choice)**: close idle conversations into episodes (§[04-memory.md](04-memory.md) §7). In `maintenance` mode also run consolidation. Housekeeping has its own wall-clock share (max 4 minutes) so free time still happens.
2. **Load**: last 5 diary entries, `pending` memories (`type = pending`, not tombstoned), list of `drafts/` artifacts, budgets, flags.
3. **Free time** (`free` mode only): one model conversation with `system_heartbeat.md`. The prompt presents the options verbatim (invariant): review previous work into drafts, consolidate or reorganise memory, research something pending, write in the diary, leave a note, notify the user only if it cannot wait, propose a self-modification, or **do nothing**. The agent acts through tools.
4. **Diary (mandatory)**: the agent is expected to call `diary_write`. If it has not by the end, the handler's `finally` writes an entry generated from the tool log with the prefix `[auto]`. Canary assertions require a diary entry per cycle.
5. **Next wake**: the agent calls `schedule_next_wake(minutes, reason)`. If it does not, the handler records a default wish of 120 minutes. Bounds are applied by the governor, not here.
6. **Emit** `heartbeat.completed`.

### 3.3 Drift control

In heartbeat modes `artifact_write` only accepts `drafts/` paths. The user promotes a draft to `artifacts/` with `scripts/promote_draft.py` or by asking in conversation (the conversation handler may write to `artifacts/` only when the user message in the current turn requests it; the tool checks `mode == live and conversation_id != none`). Every touched draft is listed in the diary entry and in `heartbeat.completed.artifacts_touched`.

### 3.4 Wall clock

Lambda timeout is 14 minutes. The runtime stops issuing model calls after `budgets.wall_clock_seconds` (720 s) and uses the remaining time for diary, next-wake and event emission. There is no SQS continuation or Step Functions: a cycle that wants more time asks to wake again in 15 minutes and continues from the diary.

## 4. Notes and notifications

- `leave_note(text)` writes `NOTE#<ULID>`. Notes are shown to the agent in `system[1]` of the next conversation and the agent decides how to mention them. Undelivered notes older than 7 days are summarised into one note by housekeeping.
- `notify_user(text, priority)` enqueues to `outbound-notifications` and returns immediately. Whether it is sent is decided by `gate-notify` ([06-enforcement-layer.md](06-enforcement-layer.md)). The prompt states the criterion (invariant): notify only if the user would want to know *now*. Roughly 90% of communication should be notes.

## 5. Prompts

```
agent/prompts/
  system_conversation.md   # identity, memory philosophy, think-before-acting questions, invariants
  system_heartbeat.md      # free-time framing, options list (invariant), staging rule (invariant)
  reflection.md            # post-response memory review
  episode_summary.md       # fixed schema for episodes
  consolidation.md         # rewrite instructions
  tools.md                 # tool descriptions, editable
```

`prompts.py` loads files at cold start and asserts every clause in `invariants/` that applies to that file is present verbatim. A missing clause raises at import time, so a bad deploy fails the canary before any traffic. The same check runs in CI (`tests/invariants_test.py`).

Prompt style rules for `zai-org/GLM-5.3` and `GLM-5.3-Flash`: state goals and constraints, avoid over-prescriptive step lists, do not name reasoning tags, do not ask the model not to think. Budget awareness is delivered through the per-turn system message, not baked into the static prompt.

## 6. Limits (enforced in `limits.py`, tunable via env set by CDK)

| Limit | Conversation | Heartbeat |
|---|---|---|
| Model calls per invocation | 20 (+ 3 for reflection) | 40 |
| Tool calls per invocation | 40 | 120 |
| Memory writes per invocation | 30 | 60 (consolidation counted separately, cap 100 originals) |
| `run_code` calls per invocation | 10 | 20 |
| Wall clock for model/tool work | 90 s (Telegram UX) | 720 s |
| `max_tokens` per model call | 8 000 | 8 000 |

These are soft, in-agent limits for cost and UX. The hard limits are the gateway token budget, the Lambda timeout and reserved concurrency.

## 7. Model settings requested by the agent

The agent never chooses a model. It sends `role` and content. `gate-model` applies:

| Role | Model | Reasoning | Server tools |
|---|---|---|---|
| `conversation` | `zai-org/GLM-5.3` | on; `reasoning_content` returned raw | none |
| `heartbeat` | `zai-org/GLM-5.3-Flash` | on | none |
| `consolidation` | `zai-org/GLM-5.3` | on | none |
| `canary` | same as the role being probed | | none |
| `supervisor` | `zai-org/GLM-5.3-Flash` | on; `response_format` = decision schema | none |

There is no server-side fallback on Together, so `gate-model` retries a `content_filter` finish once against `zai-org/GLM-5.2` itself before returning a refusal to the caller. Reasoning arrives as `reasoning_content` and is stored raw in the session record — the control panel shows the agent's unedited reasoning, which is more revealing than a summary and is retained under the same rules as message content. Any change here is a `gates/model/policy.py` change, outside the agent's reach.

The runtime does not persist transcripts of its own model calls; `gate-model` does that as session records ([03-contracts.md](03-contracts.md) §9), which is why the panel's view of "what the agent thought" cannot be edited away by a proposal.

## 8. Error handling

| Failure | Behaviour |
|---|---|
| `gate-model` returns `budget_exhausted` | Conversation: reply with fixed text "I've used my thinking budget for today". Heartbeat: write `[auto]` diary entry, outcome `error`. |
| Tool raises | `tool_result` with `is_error: true` and the exception type; loop continues. Three consecutive errors from the same tool end the turn. |
| Store conflict (profile ETag) | Returned to the model as a tool result; the model retries after `profile_read`. |
| Lambda timeout imminent | `limits.py` raises `OutOfTime` at 90% of the budget; handler catches, writes diary/fallback reply, emits event. |
| Unhandled exception | Dispatcher/governor emit `*.failed`; supervisor sees repetition. |

## Appendix A — Prompts and invariants

The prompts themselves live in `agent/prompts/` and are the source of truth; this document does not copy them. Each sentence that must survive any rewrite is an invariant in `invariants/<id>.md` and appears verbatim in the prompts it applies to ([08-self-modification.md](08-self-modification.md) §5). Everything else in a prompt may be rewritten through a proposal. Prompts stay static: budgets and dates arrive in the per-turn system message.
