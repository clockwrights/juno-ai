# 03 — Contracts

Every boundary in the system has a contract here, and a JSON Schema file under `contracts/` that is validated at runtime on both sides and in CI. Contracts are frozen once written. A contract change requires a new `schema_version` and consumers that accept both versions for one release.

Files: `contracts/<name>.schema.json`, draft 2020-12. Python types are generated into `core/contracts.py` with `datamodel-code-generator` in CI and committed; a test fails if they are stale.

## 1. Common envelope

Every Lambda invocation payload and every event `detail` includes:

```json
{
  "schema_version": "1",
  "trace_id": "01J...ULID",
  "stage": "dev | prod",
  "issued_at": "2026-09-06T10:00:00Z"
}
```

`trace_id` is created by the first gate that touches a flow (`gate-telegram`, `gate-governor`, `gate-canary`, or the CI job) and propagated everywhere, including model gateway logs and supervisor journal entries. Every log line is JSON with `trace_id`.

## 2. Lambda invocation contracts

### 2.1 `agent-conversation`

Request (`contracts/conversation-request.schema.json`):

```json
{
  "schema_version": "1", "trace_id": "...", "stage": "dev", "issued_at": "...",
  "mode": "live | canary",
  "channel": "telegram | panel",
  "conversation_id": "tg:<chat_id> | panel:owner",
  "message": { "message_id": "tg:<update_id> | panel:<ULID>", "text": "string", "received_at": "ISO8601" },
  "budgets": { "notifications_remaining": 2, "tokens_remaining_today": 900000 }
}
```

Response (`contracts/conversation-response.schema.json`):

```json
{
  "schema_version": "1", "trace_id": "...",
  "reply": "string, may be empty if the agent chose silence",
  "usage": { "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "model_calls": 0 },
  "tool_calls": [ { "name": "memory_search", "ok": true, "duration_ms": 120 } ],
  "memory_ops": { "saved": 0, "updated": 0, "forgotten": 0 },
  "notes_delivered": [ "note_id" ]
}
```

Errors: the handler MUST return a response with `reply` set to a fixed fallback text and an `error` object `{ "type": "model_error | tool_error | budget_exhausted | timeout", "message": "..." }` rather than raising, so `gate-dispatcher` can always answer the user. Unhandled exceptions are treated by the dispatcher as `error.type = "crash"`.

### 2.2 `agent-heartbeat`

Request (`contracts/heartbeat-request.schema.json`):

```json
{
  "schema_version": "1", "trace_id": "...", "stage": "dev", "issued_at": "...",
  "mode": "free | maintenance | canary",
  "invocation_id": "ULID",
  "budgets": { "notifications_remaining": 2, "tokens_remaining_today": 900000, "wall_clock_seconds": 720 },
  "agenda": [ "close_idle_conversations", "consolidate_memory" ]
}
```

`agenda` is empty in `free` mode. In `maintenance` mode the governor fills it. In `canary` mode `gate-canary` fills it with `["canary_probe"]`.

Response (`contracts/heartbeat-response.schema.json`):

```json
{
  "schema_version": "1", "trace_id": "...", "invocation_id": "ULID",
  "outcome": "did_nothing | worked | error",
  "summary": "one paragraph, copied from the diary entry",
  "diary_entry_sk": "ISO8601 sort key of the entry written",
  "next_wake_wish_minutes": 60,
  "usage": { "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "model_calls": 0 },
  "tool_calls": [ ... ],
  "memory_ops": { "saved": 0, "updated": 0, "forgotten": 0, "consolidated": 0 },
  "artifacts_touched": [ "drafts/web-project/notes.md" ]
}
```

### 2.3 `gate-model`

Request (`contracts/model-request.schema.json`): a subset of the OpenAI-compatible Chat Completions body plus routing fields. The gate adds `model` and auth. The caller MUST NOT send `model`.

`system` is a list of text blocks rather than a single string: the gate concatenates them into the leading `system` message in order. The split is preserved because prefix stability is what earns the cache — blocks are ordered most-stable first, and the volatile per-turn block carrying dates and budgets is always last.

```json
{
  "schema_version": "1", "trace_id": "...", "stage": "dev", "issued_at": "...",
  "role": "conversation | heartbeat | consolidation | supervisor | canary",
  "system": [ { "text": "...", "stable": true } ],
  "messages": [ ... ],
  "tools": [ { "type": "function", "function": { "name": "...", "parameters": {} } } ],
  "max_tokens": 8000,
  "response_format": { "...optional {type: json_schema, json_schema: {...}}..." : {} }
}
```

Response (`contracts/model-response.schema.json`): the assistant message as returned by the API (`content`, `reasoning_content`, `tool_calls`, `finish_reason`, `usage`, `model`), plus:

```json
{ "gateway": { "model_used": "zai-org/GLM-5.3", "tokens_remaining_today": 138204, "latency_ms": 2100 } }
```

Errors are returned, not raised: `{ "error": { "type": "budget_exhausted | role_not_allowed | upstream_error | invalid_request", "message": "...", "retry_after_seconds": 0 } }`.

`reasoning_content` is raw model reasoning, not a summary. It is stored in the session record and shown in the panel, and is treated as sensitive as message content. The gate writes every request/response pair as a session record (§9) before returning; callers cannot disable this.

Role → policy table lives in `gates/model/policy.py` and is documented in [06-enforcement-layer.md](06-enforcement-layer.md).

### 2.4 `sandbox-executor`

Request (`contracts/executor-request.schema.json`):

```json
{ "schema_version": "1", "trace_id": "...", "language": "python", "code": "string", "timeout_s": 30, "stdin": "optional string" }
```

Response (`contracts/executor-response.schema.json`):

```json
{ "schema_version": "1", "trace_id": "...", "stdout": "≤ 64 KiB", "stderr": "≤ 16 KiB", "exit_code": 0, "duration_ms": 0, "truncated": false, "killed": false }
```

Only `language = "python"` is supported. The enum exists so adding a language is a contract-compatible change.

### 2.5 `gate-canary` (CodeDeploy hook)

Input is the CodeDeploy lifecycle event. The hook reads `TARGET_VERSION_ARN` from the deployment, invokes the candidate versions of both agent Lambdas with `mode = "canary"` three times each, evaluates `contracts/canary-assertions.json`, and calls `PutLifecycleEventHookExecutionStatus` with `Succeeded` or `Failed`. Assertions are listed in [08-self-modification.md](08-self-modification.md).

## 3. Queue message contracts

### 3.1 `inbound-messages` (SQS FIFO)

`MessageGroupId = conversation_id`, `MessageDeduplicationId = message_id`. Body:

```json
{ "schema_version": "1", "trace_id": "...", "stage": "prod", "issued_at": "...",
  "channel": "telegram | panel",
  "conversation_id": "tg:123 | panel:owner",
  "message_id": "tg:456 | panel:<ULID>",
  "text": "string", "received_at": "ISO8601",
  "telegram": { "chat_id": "123", "update_id": "456", "message_id": 789 } }
```

`telegram` is present only for `channel = telegram`. The control panel writes `channel = panel` messages directly to the queue with the admin role; the dispatcher stores the reply and sends nothing.

### 3.2 `outbound-notifications` (SQS standard)

```json
{ "schema_version": "1", "trace_id": "...", "stage": "dev", "issued_at": "...",
  "kind": "notify", "priority": "low | normal | urgent", "text": "≤ 4000 chars", "source": "conversation | heartbeat" }
```

`priority` is informational for the user. It does not bypass the budget.

## 4. Event bus contracts

Bus `agent-events`. `source` is one of `agent.runtime`, `agent.gate`, `agent.pipeline`, `agent.alarm`, `agent.supervisor`. `detail-type` values and their `detail` payloads (all include the common envelope):

| detail-type | source | detail fields | Typical supervisor level |
|---|---|---|---|
| `conversation.completed` | runtime | `conversation_id`, `usage`, `memory_ops` | 0 (metrics only) |
| `conversation.failed` | gate | `conversation_id`, `error.type`, `error.message` | 0 / 1 if repeated |
| `heartbeat.completed` | runtime | `invocation_id`, `mode`, `outcome`, `usage`, `memory_ops`, `artifacts_touched`, `next_wake_wish_minutes` | 0 |
| `heartbeat.failed` | gate | `invocation_id`, `error` | 0 / 1 if repeated |
| `governor.skipped` | gate | `reason: paused \| not_due \| daily_cap \| conversation_active` | 0 |
| `governor.clamped` | gate | `wished_minutes`, `applied_minutes` | 0 |
| `notification.sent` | gate | `remaining_today` | 0 |
| `notification.budget_exhausted` | gate | `dropped_text_hash`, `exhausted_at` | 1 if within 2 h of day start |
| `model.budget_exhausted` | gate | `role`, `tokens_used_today` | 1 |
| `model.upstream_error` | gate | `role`, `status`, `message` | 0 / 1 if repeated |
| `executor.anomaly` | gate | `kind: timeout \| network_attempt \| oversize_output \| repeated_failure`, `count_24h` | 1 / 2 if repeated |
| `access.denied` | alarm (CloudTrail metric filter) | `principal_role`, `action`, `resource` | **2 always** |
| `proposal.submitted` | runtime | `proposal_id`, `target_paths`, `rationale` | 0 |
| `proposal.rejected` | gate/pipeline | `proposal_id`, `reason: path \| gate_failed \| invariant_failed \| canary_failed` | 0 / 1 if repeated |
| `proposal.created` | gate | `proposal_id`, `pr_url` | 0 |
| `deploy.completed` | pipeline | `version`, `components`, `pr_url` | 0 |
| `deploy.rolled_back` | pipeline | `version`, `alarm` | 1 |
| `budget.threshold` | alarm | `percent: 70 \| 90` | 1 / 2 |
| `supervisor.decision` | supervisor | `level`, `action`, `summary`, `evidence` | n/a (journal + watcher alarms) |

## 5. Tool contracts (agent-facing)

Every tool is defined in `core/tools/registry.py` with `strict: true` and `additionalProperties: false`. Tool descriptions live in `agent/prompts/tools.md` and are editable; schemas are not (they live in `contracts/tools/*.schema.json`).

| Tool | Input | Output | Side effect path |
|---|---|---|---|
| `memory_save` | `content: str (≤ 8 KiB)`, `type: MemoryType`, `tags: [str] (≤ 10)`, `importance: 1..5` | `{ id }` | DynamoDB + S3 + vector index |
| `memory_search` | `query: str`, `type?: MemoryType`, `tags?: [str]`, `top_k?: 1..20 (default 8)` | `{ results: [ { id, type, content, tags, importance, created_at, last_accessed, score } ] }` | Refreshes `last_accessed`, `access_count` |
| `memory_update` | `id: str`, `content: str` | `{ id, version }` | New version, re-embed |
| `memory_forget` | `id: str`, `reason: str` | `{ ok }` | Tombstone only |
| `profile_read` | none | `{ content, etag }` | none |
| `profile_write` | `content: str (≤ 32 KiB)`, `etag: str` | `{ etag }` or `{ error: "conflict", current_etag, content }` | S3 conditional PUT |
| `diary_read` | `last_n?: 1..20 (default 5)` | `{ entries: [ { sk, content } ] }` | none |
| `diary_write` | `content: str (≤ 8 KiB)` | `{ sk }` | Append-only |
| `leave_note` | `text: str (≤ 4000)` | `{ note_id }` | `agent_state` NOTE item |
| `notify_user` | `text: str (≤ 4000)`, `priority: low\|normal\|urgent` | `{ queued: true, notifications_remaining_hint }` | SQS enqueue only |
| `run_code` | `code: str`, `timeout_s?: 1..60 (default 30)`, `stdin?: str` | executor response | Invoke `sandbox-executor` |
| `artifact_read` | `path: str` | `{ content, etag }` | S3 `artifacts/` or `drafts/` |
| `artifact_write` | `path: str`, `content: str`, `etag?: str` | `{ etag }` | S3 `drafts/` only. Writes to `artifacts/` are rejected. |
| `artifact_list` | `prefix?: str` | `{ paths: [...] }` | none |
| `schedule_next_wake` | `minutes: 15..1440`, `reason: str` | `{ recorded_minutes }` | `agent_state` NEXT_WAKE wish |
| `propose_change` | `title: str`, `rationale: str`, `diff: str (unified, ≤ 200 KiB)` | `{ proposal_id }` or `{ error: "proposals_disabled" }` | `agent_proposals` item |
| `list_proposals` | `status?: str` | `{ proposals: [...] }` | none |
| `proposal_status` | `proposal_id: str` | proposal record | none |

There is no web tool: none is in the registry, and the agent has no other route to the internet.

`MemoryType` enum: `user_fact`, `user_preference`, `episode`, `agent_learning`, `project`, `pending`.

Tools available per mode: conversation has all tools except `schedule_next_wake`; heartbeat has all tools; canary mode has all tools but every store is namespaced under `CANARY#` and `notify_user` and `propose_change` return `{ error: "disabled_in_canary" }`.

## 6. State keys (`agent_state` table)

| PK | SK | Attributes | Written by | Read by |
|---|---|---|---|---|
| `STATE` | `pause` | `paused: bool`, `by`, `reason`, `at` | supervisor, `scripts/pause.py`, `scripts/resume.py` | governor, dispatcher |
| `STATE` | `next_wake` | `wish_at: ISO`, `wish_minutes`, `reason`, `written_at` | agent (`schedule_next_wake`) | governor |
| `STATE` | `counters#<YYYY-MM-DD>` | `notifications: n`, `heartbeats: n`, `tokens#<role>: n` | gate-notify, governor, gate-model (conditional updates) | governor, gates, agent (read-only for prompts) |
| `STATE` | `lock#<chat_id>` | `expires_at: ISO`, `trace_id` | dispatcher | governor |
| `NOTE` | `<ULID>` | `text`, `created_at`, `delivered_at?`, `source` | agent (`leave_note`), gate-notify (overflow) | agent-conversation |
| `FLAG` | `<name>` | `enabled: bool`, `set_by`, `at` | `scripts/flags.py` only | agent, gates |
| `CANARY#STATE` … | mirrors of the above | | canary runs | gate-canary |

Feature flags (`FLAG#<name>`): `proposals_enabled` (default false), `auto_merge_prompts` (default false), `web_access_enabled` (default **false**; reserved — no component reads it, because no web tool exists), `heartbeat_enabled` (default true). Flags are read at the start of every invocation; there is no cache.

## 7. Proposal record (`agent_proposals`)

```
PK: PROPOSAL#<ULID>   SK: META
title, rationale, diff_s3_key, target_paths: [..], status, created_at, updated_at,
pr_url?, pr_number?, rejection_reason?, deployed_version?
PK: PROPOSAL#<ULID>   SK: EVENT#<ISO>   (append-only) { status_from, status_to, by, detail }
```

Status machine: `submitted → rejected_path | pr_open`; `pr_open → gate_failed | approved | closed`; `approved → deploying`; `deploying → deployed | rolled_back`. Only `gate-proposal` and the pipeline write status transitions; the agent may only create `submitted`.

## 8. Diary and memory records

See [09-data-model.md](09-data-model.md) for full attribute lists. The agent-facing shapes above are the contract; storage layout may change without a contract bump as long as the tool outputs do not.

## 9. Session records (written by `gate-model`, read by the control panel)

A session is one invocation (`trace_id`): a conversation turn, a heartbeat cycle, a supervisor decision or a canary probe. Each model call inside it is one record.

S3 key: `sessions/<YYYY-MM-DD>/<trace_id>/<seq:04d>.json` (`contracts/session-record.schema.json`):

```json
{
  "schema_version": "1", "trace_id": "...", "stage": "prod", "seq": 3,
  "kind": "conversation | heartbeat | consolidation | supervisor | canary",
  "role": "conversation", "model": "zai-org/GLM-5.3",
  "requested_at": "ISO8601", "latency_ms": 2100,
  "request": { "system": [...], "messages": [...], "tools": [ "memory_search", "..." ], "max_tokens": 8000 },
  "response": { "content": "...", "reasoning_content": "...raw reasoning...", "tool_calls": [...], "finish_reason": "tool_calls", "usage": {...} },
  "error": null
}
```

Index item in `agent_sessions` (one per `trace_id`, upserted on every record):

```
PK: SESSION#<YYYY-MM-DD>   SK: <started_at ISO>#<trace_id>
kind, role, conversation_id?, invocation_id?, started_at, ended_at, calls, input_tokens, output_tokens,
cached_tokens, tool_names: set, outcome?, s3_prefix, size_bytes
GSI conversation-index: PK conversation_id, SK started_at
```

Rules: records are written before the response is returned to the caller, so a session cannot be hidden by agent code. Only the admin role can delete records. Default S3 lifecycle expiry is 180 days; the control panel can delete earlier.
