# 06 — Enforcement Layer (Layer 3)

Everything in this document is immutable from the agent's point of view: it lives in `gates/`, `sandbox/`, `infra/` and `contracts/`, paths that `gate-proposal` rejects and CODEOWNERS protects. Each gate is a small Lambda with one job, one role, and unit tests that prove the policy.

## 1. `gate-telegram` — inbound webhook

- Lambda Function URL (auth type `NONE`, free; no API Gateway) at a path containing a random 32-char segment; Telegram is configured with `secret_token`, verified via the `X-Telegram-Bot-Api-Secret-Token` header before anything else. Requests failing the check get 403 and are logged; the weekly report counts them (no alarm, to stay inside the free tier).
- Only messages from `OWNER_CHAT_ID` are accepted; others get a fixed "this bot is private" reply and are dropped.
- Deduplicates on `update_id` (`STATE#tg_update#<id>` with 24 h TTL).
- Non-text updates (photos, voice) are answered with a fixed "text only" reply. The contract enum leaves room for other kinds without a breaking change.
- Enqueues to `inbound-messages` FIFO and returns 200 within the Lambda's 3 s timeout.

## 2. `gate-dispatcher` — conversation runner

- SQS FIFO consumer, batch size 1, reserved concurrency 2.
- Sets `STATE#lock#<chat_id>` with `expires_at = now + 30 min` before invoking, extends it after.
- Appends the user message to `agent_conversations`, invokes `agent-conversation` synchronously with a 100 s client timeout, appends the assistant message, and for `channel = telegram` sends the reply through the bot (splitting at 4000 chars). For `channel = panel` it stores the reply only; the control panel polls the conversation store.
- On invoke error, throttle (paused), timeout, or `error` in the response: sends the fixed fallback text for that error type, emits `conversation.failed`. Message is deleted from the queue either way; there is no retry of model work. The DLQ receives only messages the dispatcher itself could not process (e.g. Telegram API down), and a DLQ depth alarm notifies ops.
- Holds the bot token; the agent Lambda does not.

## 3. `gate-governor` — heartbeat scheduling

Triggered by EventBridge `rate(15 minutes)` and `cron(0 4 ? * SUN *)` (weekly maintenance, UTC). Algorithm:

```
flags = read FLAG#heartbeat_enabled, STATE#pause
if paused or not enabled: emit governor.skipped(paused); return
if any STATE#lock#* with expires_at > now: emit governor.skipped(conversation_active); return
today = STATE#counters#<date>
if today.heartbeats >= DAILY_CAP (48): emit governor.skipped(daily_cap); return
mode = "maintenance" if event.force_mode == "maintenance" else "free"
if mode == "free":
    wish = STATE#next_wake
    applied = clamp(wish.wish_minutes, MIN=15, MAX=1440) if wish else DEFAULT=120
    if applied != wish.wish_minutes: emit governor.clamped
    due_at = wish.written_at + applied
    if now < due_at: emit governor.skipped(not_due); return
conditional update today.heartbeats += 1 (fails if >= cap → skipped)
invoke agent-heartbeat async with mode, invocation_id, budgets
```

Budgets passed: `notifications_remaining = 2 − today.notifications`, `tokens_remaining_today` from `gate-model`'s counter, `wall_clock_seconds = 720`.

The agent's only influence is `STATE#next_wake`. It cannot invoke the heartbeat, change the rate, or raise the cap. A `MIN` below 15 is impossible because the rule fires every 15 minutes.

## 4. `gate-notify` — notification budget

- SQS standard consumer, reserved concurrency 1 (serialises the counter update; the conditional update is also atomic).
- `UpdateItem STATE#counters#<date> SET notifications = if_not_exists(notifications, 0) + 1 CONDITION notifications < 2`.
- Success → `sendMessage` through the bot with a `🔔` prefix and the priority; emit `notification.sent`.
- `ConditionalCheckFailedException` → write the text as `NOTE#` with `source = notify_overflow`, emit `notification.budget_exhausted`, do not send.
- The day boundary is UTC. The budget value is a CDK context parameter, not an agent-readable setting.

## 5. `gate-model` — model gateway

The single place where model calls happen. Holds the Together AI API key. Uses the `openai` Python SDK pointed at `https://api.together.xyz/v1`.

### 5.1 Policy (`gates/model/policy.py`)

| Role | Allowed callers (IAM principal) | Model | Request additions | Daily token budget (input + output) |
|---|---|---|---|---|
| `conversation` | agent-conversation role | `zai-org/GLM-5.3` | reasoning on | 200,000 |
| `heartbeat` | agent-heartbeat role | `zai-org/GLM-5.3-Flash` | reasoning on | 300,000 |
| `consolidation` | agent-heartbeat role | `zai-org/GLM-5.3` | reasoning on | 60,000 |
| `canary` | gate-canary role | as probed role | as probed role | 50,000 |
| `supervisor` | supervisor role | `zai-org/GLM-5.3-Flash` | `response_format: {type: "json_schema", json_schema: {…}}` = decision schema | 50,000 |
| `embed` | both agent roles | `intfloat/multilingual-e5-large-instruct` (1024 dimensions, 514-token input) | embeddings call, no system prompt or tools | 500,000 |

Caller identity is enforced by IAM, not parsed from the payload. `gate-model` has four aliases, `agent`, `embed`, `supervisor` and `canary`, each with its own resource policy: the two agent roles may invoke only the `agent` and `embed` aliases, the supervisor role only `supervisor`, `gate-canary` only `canary`. The handler reads the alias from `context.invoked_function_arn` and accepts only the roles allowed for that alias (`agent` → `conversation`, `heartbeat`, `consolidation`). A role sent through the wrong alias returns `role_not_allowed` and emits an event.

### 5.2 Behaviour

1. Validate against `model-request.schema.json`. Reject `model`, `api_key`, `base_url`, `stream`, `user` and `n` from callers.
2. Estimate input tokens locally — Together exposes no token-counting endpoint — with a conservative characters-per-token heuristic, or the last response's `usage` when the caller has one. The estimate leads the true count, never lags it, so a budget can be reached early but never overrun silently; step 5 reconciles.
3. Conditional update `STATE#counters#<date>.tokens#<role> + estimated ≤ budget`; on failure return `budget_exhausted` with `retry_after_seconds` until UTC midnight, emit `model.budget_exhausted`.
4. Call `client.chat.completions.create(...)` with streaming, `max_retries = 2`, timeout 600 s. `max_tokens` capped at 16,000. On `finish_reason = "content_filter"`, retry once against `zai-org/GLM-5.2` before returning a refusal.
5. Reconcile the counter with actual `usage` (`prompt_tokens` + `completion_tokens`; `prompt_tokens_details.cached_tokens` counted at 20%, matching Together's cached-input price ratio).
6. Log one JSON line per call: `trace_id`, role, model, `finish_reason`, usage, latency, tool names used. Never log content to CloudWatch.
7. Write the session record (full request and response, including raw `reasoning_content`) to `sessions/<date>/<trace_id>/<seq>.json` and upsert the `agent_sessions` index item. This happens before the response is returned, so a session cannot be skipped by the caller. Content lives only in S3, where the user controls retention and deletion from the control panel.
8. Return the `Message` object plus `gateway` fields.

Server tools: none. There is no path to the web: no web tool is in the registry and `web_access_enabled` is read by nothing.

Prompt caching: Together caches prefixes automatically. The gate keeps the prefix cacheable — nothing volatile is injected before the stable blocks, and the per-turn system message carrying budgets and dates goes last. The cache hit rate (`prompt_tokens_details.cached_tokens / prompt_tokens`) is in the weekly report, because a silent invalidator is a cost bug.

## 6. `sandbox-executor`

- Python 3.12 Lambda, 512 MB, 60 s timeout, in a VPC with private subnets, **no NAT, no VPC endpoints**, security group with no egress rules.
- Role: `logs:CreateLogStream`, `logs:PutLogEvents` on its own log group only. No `logs:CreateLogGroup` (created by CDK). Plus `ec2:CreateNetworkInterface`, `ec2:DescribeNetworkInterfaces` and `ec2:DeleteNetworkInterface` on `*` — AWS refuses to create a Lambda in a VPC whose execution role cannot manage its own ENI, and documents these as requiring `*`. Nothing else, and no other role in the system holds an EC2 permission.
- Those EC2 permissions are unusable from inside: the subnets have no route to the EC2 API or to anything else. The isolation is the routing, not the policy — which is why the routing assertions in `infra/tests/test_network.py` are the ones that must never be weakened.
- Handler: wipe `/tmp` on entry, write code to `/tmp/main.py`, run `subprocess.run([sys.executable, "-I", "/tmp/main.py"], timeout, capture_output, stdin)` with `resource` limits (address space 400 MB, no core dumps), truncate output to caps, return contract response. `killed = true` on timeout.
- Environment variables are cleared before the subprocess (no AWS credentials exist anyway, but the Lambda runtime env is scrubbed).
- Available packages: standard library plus `numpy`, `pandas`, `requests` (requests will fail without network; included because agent code often imports it and a clear error is better than an import error). Packages are pinned in `sandbox/requirements.txt`.
- The gate emits `executor.anomaly` on: timeout, output truncation, `stderr` containing `Network is unreachable` or `Name or service not known` (network attempt), and 5 failures in 24 h.

Design rule: the worst case of running agent code is that nothing happens.

## 7. `gate-proposal` and `gate-canary`

Specified in [08-self-modification.md](08-self-modification.md).

## 8. IAM, network and account limits

All in `infra/stacks/`, asserted by `infra/tests/test_iam_boundaries.py` on the synthesized template (see [12-testing.md](12-testing.md)).

- One role per Lambda, resource ARNs scoped by stage prefix.
- No VPC for any Lambda except `sandbox-executor`. No NAT gateways, no VPC endpoints, nothing billed by the hour anywhere in the account.
- Secrets are SSM `SecureString` parameters (standard tier, free), read at cold start with `ssm:GetParameter` scoped per role. Lambda environment variables hold no secrets, only names and ARNs.
- Reserved concurrency: `agent-conversation` 2, `agent-heartbeat` 1, `sandbox-executor` 2, `gate-model` 4, all gates ≤ 2. The account cannot fan out.
- AWS Budgets (two, free): a monthly cost budget with notifications at 70% and 90% → SNS ops topic → ops channel and event `budget.threshold`.
- CloudTrail management-events trail (free) with a metric filter on `errorCode = AccessDenied` for the agent roles → the single custom metric in the design → `access.denied` event (Level 2) and the dumb alarm below.
- Log retention 14 days, JSON lines, no content. Supervisor journal, proposal events and session records are never expired by CloudWatch; sessions have an S3 lifecycle the user controls.

## 9. Dumb alarms (no model involved)

Five CloudWatch alarms plus one delayed self-check, all on `prod` only, inside the always-free tier of ten alarms. Other signals are rows in the weekly report from the control panel.

| Alarm | Condition | Action |
|---|---|---|
| `supervisor-errors` | supervisor Lambda errors ≥ 1 in 5 min | ops channel |
| `supervisor-silent` | metric math: supervisor-queue messages received > 0 and supervisor invocations = 0 over 1 h | ops channel |
| `agent-error-rate` | errors ≥ 5 in 15 min on either agent Lambda (metric math over both) | ops channel; also a CodeDeploy rollback alarm |
| `access-denied` | CloudTrail AccessDenied metric ≥ 1 | ops channel (in addition to supervisor Level 2) |
| `dlq-depth` | metric math: sum of visible messages across the three DLQs > 0 | ops channel |
| `budget-90-no-action` | AWS Budgets 90% notification received and no `supervisor.decision` within 10 min (implemented in `ops-alarms` with a 10-min delayed self-check, not as a CloudWatch alarm) | ops channel + pause |

In the weekly report (`panel` → Reports, or `scripts/weekly_report.py`): webhook 403 counts, cache hit ratio, per-role token burn, governor skip reasons, consolidation counts, storage sizes.

`ops-alarms` is a tiny Lambda subscribed to the SNS topic that formats alarm and budget notifications and posts them to the ops channel. Its only other capability is the same pause action as the supervisor, used solely by `budget-90-no-action`.
