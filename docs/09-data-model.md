# 09 — Data Model

All names are shown without the `<stage>-` prefix. All tables are on-demand capacity, point-in-time recovery enabled, encrypted with the AWS-managed key. Every item carries `namespace: live | canary`; canary items use a `CANARY#` key prefix so IAM `dynamodb:LeadingKeys` conditions can separate them.

## 1. DynamoDB tables

### `agent_memory`

| Key | Value |
|---|---|
| PK | `MEM#<ULID>` or `CANARY#MEM#<ULID>` |
| SK | `META` (current), `V#<n>` (history snapshot) |
| Attributes | see [04-memory.md](04-memory.md) §2 |
| GSI `type-last_accessed-index` | PK `type`, SK `last_accessed`, projection: keys + `importance`, `access_count`, `tombstone`, `namespace`, `tags` |
| GSI `namespace-created_at-index` | PK `namespace`, SK `created_at`, projection keys only |
| Stream | off |

Agent roles: `GetItem`, `PutItem`, `UpdateItem`, `Query`, `BatchGetItem`, `BatchWriteItem` (puts only), `TransactWriteItems`. No `DeleteItem`, no `Scan`.

### `agent_memory_tags`

| Key | Value |
|---|---|
| PK | `TAG#<tag>` |
| SK | `MEM#<ULID>` |
| Attributes | `namespace`, `created_at` |

### `agent_diary`

| Key | Value |
|---|---|
| PK | `DIARY` or `CANARY#DIARY` |
| SK | `<ISO8601 with ms>#<invocation_id>` |
| Attributes | `content` (markdown ≤ 8 KiB), `mode`, `auto: bool`, `trace_id`, `artifacts_touched`, `memory_ops`, `next_wake_wish_minutes` |

Agent roles: `PutItem` with condition `attribute_not_exists(SK)`, `Query`. No update, no delete.

### `agent_state`

Keys listed in [03-contracts.md](03-contracts.md) §6. Additional access rules:

| Item | Agent roles | Gates | Supervisor |
|---|---|---|---|
| `STATE#pause` | read | governor, dispatcher read; `ops-alarms` write | write |
| `STATE#next_wake` | write, read | governor read | read |
| `STATE#counters#*` | read | governor, notify, model conditional update | read |
| `STATE#lock#*` | read | dispatcher write, governor read | read |
| `STATE#tg_update#*` (TTL 24 h) | none | telegram write | none |
| `NOTE#*` | write (create), update `delivered_at`, read | notify write (overflow) | read |
| `FLAG#*` | read | read | read; `scripts/flags.py` writes |

IAM conditions use `dynamodb:LeadingKeys` on PK and, where a single PK has mixed SKs (`STATE`), the write paths are split by attribute using `dynamodb:Attributes` with `StringEqualsIfExists` on the SK values the role may touch. Where a condition is not expressible (SK-level split on one PK), the store is split: `STATE#pause` lives under PK `PAUSE` so the LeadingKeys condition works. The final key map is in `contracts/state-keys.json` and tested.

### `agent_conversations`

| Key | Value |
|---|---|
| PK | `CONV#<conversation_id>` |
| SK | `META` or `MSG#<ISO8601 ms>#<seq>` |
| META attributes | `status: open \| closed`, `opened_at`, `last_activity_at`, `episode_id?`, `token_estimate`, `chat_id` |
| MSG attributes | `role: user \| assistant`, `content` (JSON: the exact API content blocks, thinking included), `trace_id`, `usage?` |
| GSI `status-last_activity-index` | PK `status`, SK `last_activity_at` (idle conversation scan) |

Dispatcher writes user and assistant MSG items and META activity; the agent updates META `status`/`episode_id` in housekeeping. Bodies over 350 KiB are moved to S3 `conversations/<id>/<sk>.json` with a pointer.

### `agent_proposals`

Keys in [03-contracts.md](03-contracts.md) §7. Stream enabled (`NEW_IMAGE`) for `gate-proposal`. Agent roles may `PutItem` only with condition `attribute_not_exists(PK) AND status = submitted` and may `Query`. Diffs are stored in S3 `proposals/<id>/diff.patch`.

### `agent_sessions`

| Key | Value |
|---|---|
| PK | `SESSION#<YYYY-MM-DD>` |
| SK | `<started_at ISO>#<trace_id>` |
| Attributes | `kind`, `role`, `conversation_id?`, `invocation_id?`, `started_at`, `ended_at`, `calls`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `tool_names` (set), `outcome?`, `s3_prefix`, `size_bytes` |
| GSI `conversation-index` | PK `conversation_id`, SK `started_at` |

Written only by `gate-model` (upsert per call) and `gate-canary`/`gate-dispatcher`/`gate-governor` (outcome). Deleted only by the admin role (control panel cleanup).

### `supervisor_journal`

| Key | Value |
|---|---|
| PK | `JOURNAL` |
| SK | `<ISO8601 ms>#<ULID>` |
| Attributes | `event_type`, `trace_id`, `level`, `action`, `summary`, `probable_cause`, `evidence`, `raw_model_output?`, `preclassified: bool` |
| GSI `event_type-sk-index` | PK `event_type`, SK `SK` (24 h counts) |

Only the supervisor and `ops-alarms` write. Never expired.

## 2. S3

One bucket per stage, `agent-<stage>-<account>`, versioning on, public access blocked, SSE-S3, lifecycle: noncurrent versions expire after 365 days except under `profile/` (kept forever).

| Prefix | Content | Agent roles | Others |
|---|---|---|---|
| `profile/profile.md` | Hot memory | Get, Put (with `If-Match`) | supervisor read |
| `memories/<MEM id>.md` | Bodies > 4 KiB | Get, Put | supervisor read |
| `artifacts/**` | Canonical work | Get; Put only from `agent-conversation` | `scripts/promote_draft.py` writes |
| `drafts/**` | Staging work | Get, Put, List | — |
| `conversations/**` | Overflow message bodies | Get, Put | dispatcher Put |
| `proposals/<id>/diff.patch` | Proposal diffs | Put (create only) | gate-proposal Get |
| `canary/**` | Canary-mode artifacts and bodies | Get, Put (canary mode only) | gate-canary Delete |
| `prompts/deployed/<version>/` | Copy of deployed prompts | none | `scripts/deploy.py` Put (via the admin role), supervisor Get, panel Get |
| `sessions/<date>/<trace_id>/<seq>.json` | Full model request/response records | none | gate-model Put; panel Get/Delete |
| `exports/<ULID>.zip` | Panel-generated downloads (transient) | none | panel Put/Get/Delete; lifecycle 1 day |

No role has `s3:DeleteObject` on the bucket except `gate-canary` on `canary/*` and the admin role used by `scripts/` and the control panel. Lifecycle: `sessions/` expire after 180 days (CDK context `sessions_retention_days`), `exports/` after 1 day.

## 3. S3 Vectors

Vector bucket `agent-<stage>-vectors`, index `memory`, dimension 1024, distance cosine, metadata keys filterable: `namespace`, `type`, `tombstone`, `tags` (list). Non-filterable: `mem_id`. Agent roles: `PutVectors`, `QueryVectors`, `GetVectors`. Delete only for `gate-canary` and the admin role (tombstoned items keep their vector with `tombstone = true`; `scripts/purge_tombstones.py` deletes both).

The index sits behind the `VectorIndex` interface (upsert, query, delete), so a region without S3 Vectors can use another backend without changes above the interface.

## 4. Queues, buses, rules

| Resource | Type | Producer → Consumer | DLQ |
|---|---|---|---|
| `inbound-messages.fifo` | SQS FIFO, content-based dedup off | gate-telegram → gate-dispatcher | `inbound-messages-dlq.fifo` |
| `outbound-notifications` | SQS standard | agent Lambdas → gate-notify | `outbound-notifications-dlq` |
| `supervisor-events` | SQS standard | EventBridge rule → supervisor | `supervisor-events-dlq` |
| `agent-events` | EventBridge bus | everyone → rules | archive 90 days for replay |
| `governor-rate` | EventBridge rule `rate(15 minutes)` | → gate-governor | — |
| `governor-weekly` | EventBridge rule cron Sunday 04:00 UTC | → gate-governor `{force_mode: maintenance}` | — |
| `ops-alerts` | SNS topic | alarms → ops-alarms Lambda | — |

## 5. Secrets and parameters (all SSM Parameter Store, standard tier, free)

| Parameter | Type | Readers |
|---|---|---|
| `/agent/<stage>/together-api-key` | SecureString | gate-model, gate-canary (chat and embeddings) |
| `/agent/<stage>/telegram-agent-bot` | SecureString | gate-telegram, gate-dispatcher, gate-notify |
| `/agent/<stage>/telegram-ops-bot` | SecureString | ops-alarms, supervisor, gate-proposal |
| `/agent/<stage>/github-proposal-token` | SecureString | gate-proposal |
| `/agent/<stage>/webhook-secret` | SecureString | gate-telegram |
| `/agent/<stage>/owner-chat-id` | String | gate-telegram, gate-dispatcher, gate-notify |
| `/agent/<stage>/ops-chat-id` | String | ops-alarms, supervisor, gate-proposal |

SecureStrings use the AWS-managed `aws/ssm` key (free). Agent roles have no `ssm:GetParameter*`. The admin role can read and write all of them (the control panel's Settings page).

## 6. Retention summary

| Data | Retention |
|---|---|
| Memory (live) | Forever; tombstones purged by human script after 180 days |
| Diary | Forever |
| Conversations | Forever in DynamoDB (closed ones are small); consider archiving after 1 year |
| Proposals and events | Forever |
| Supervisor journal | Forever |
| Canary namespace | Purged after each run |
| Session records | 180 days by lifecycle; deletable earlier from the control panel |
| Logs | 14 days (content-free) |
| Telegram dedup keys | 24 h TTL |
| Daily counters | 90 days TTL |
