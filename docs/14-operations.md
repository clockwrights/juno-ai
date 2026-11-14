# 14 — Operations Runbook

Two equivalent surfaces: the **control panel** (`uv run panel --stage prod`, see [15-control-panel.md](15-control-panel.md)) and the CLI scripts in `scripts/`. Both are thin layers over `ops/` and run with AWS credentials that can assume the `agent-admin-<stage>` role. They are the only path for the actions below; no Lambda can perform them. Every action writes a `supervisor_journal` entry with `by = human:<iam user>` and the surface used.

## 1. Pause and resume

```bash
uv run scripts/pause.py --stage prod --reason "investigating diary drift"
```

Disables both governor rules, sets reserved concurrency 0 on both agent Lambdas, writes `PAUSE#` item. The dispatcher answers the user with the paused message.

```bash
uv run scripts/resume.py --stage prod
```

Reverses all three, requires typing the stage name to confirm, and refuses if a supervisor Level 2 decision in the last hour has not been acknowledged (`--ack <journal sk>`).

## 2. Deploy, rollback, versions

- **Deploys are local.** `make stage-up NAME=prod` runs `scripts/deploy.py`: it refuses `prod` unless the checkout is a clean `main`, builds the Lambda assets, runs `cdk deploy --all -c stage=prod -c require_real_code=1` (adding `-c reserved_concurrency=off`, with a warning on every run, while the account's Lambda concurrency quota is below 30), then copies `agent/prompts/` to `prompts/deployed/<sha>/`. Merging to `main` deploys nothing; `ci.yml` runs on every PR and push and holds no AWS access. Run `make ci` and `make diff NAME=prod` first.
- Immediate rollback of the agent alias: `uv run scripts/rollback.py --stage prod --function agent-heartbeat` moves alias `live` to the previous version and emits `deploy.rolled_back`. CodeDeploy does this automatically during the bake window; the script is for after it.
- Prompts deployed with a version are copied to `s3://…/prompts/deployed/<version>/`; `scripts/prompt_diff.py <v1> <v2>` prints the diff.

## 3. Flags

```bash
uv run scripts/flags.py --stage prod set proposals_enabled true
uv run scripts/flags.py --stage prod list
```

Flags: `proposals_enabled`, `auto_merge_prompts`, `web_access_enabled` (reserved; no effect), `heartbeat_enabled`. Every change is journaled and posted to the ops channel.

## 4. Budgets and cost

- Token usage per role per day: `scripts/usage.py --stage prod --days 7` (reads `STATE#counters`).
- Raising a daily token budget is a CDK context change in `gates/model/policy.py` through a human PR.
- AWS Budgets thresholds at 70% and 90% of $20 notify the ops channel; 90% pauses the agent via the supervisor (or the dumb alarm if the supervisor is silent). After investigating, `resume.py`.
- Cache hit ratio is a CloudWatch metric (`cached_tokens / prompt_tokens`); if the alarm fires, check for volatile content inside the stable prefix (`scripts/inspect_request.py <trace_id>` prints the request layout from the gateway log).

## 5. Inspecting the agent

| Question | Tool |
|---|---|
| What did it do last night? | `scripts/diary.py --last 5` |
| What does it think it knows about me? | `scripts/profile_history.py --show` (current) / `--diff <n>` |
| Search memory as the agent would | `scripts/memory.py search "springfield move"` |
| What is pending? | `scripts/memory.py list --type pending` |
| Supervisor decisions | `scripts/journal.py --last 20` / `--level 2` |
| A specific trace end to end | `scripts/trace.py <trace_id>` (logs across gates, gateway, agent, supervisor) |
| Open proposals | `scripts/proposals.py list --status pr_open` |
| Replay an event to the supervisor | `scripts/replay_event.py --detail-type executor.anomaly --file evt.json` |
| Force a heartbeat now | `scripts/wake_now.py --stage dev [--mode maintenance]` (dev only by default; `--stage prod --i-know` for prod) |

## 6. Drafts and artifacts

`scripts/promote_draft.py drafts/web-project/notes.md` copies to `artifacts/` and records who promoted it. The user can also ask in conversation; the agent may then write to `artifacts/` in that turn only.

## 7. Memory hygiene

- Tombstoned memories older than 180 days: `scripts/purge_tombstones.py --older-than 180d --dry-run` then without `--dry-run`. Deletes both the item and its vector.
- Re-embedding after an embedding model change: `scripts/reindex.py --model <new>`; runs in batches, records `embedding_model` per item, safe to interrupt and resume.
- Canary leftovers (should never exist): `scripts/purge_canary.py`.

## 8. Secrets and rotation

All secrets are SSM SecureString parameters; the panel's Settings page or `scripts/params.py set <name>` writes them. Functions read parameters at cold start, so after a change bounce the readers with `scripts/bounce.py <function>` (updates an environment variable `PARAM_EPOCH`, forcing new environments).

| Parameter | Rotation |
|---|---|
| `together-api-key` | Create a new key in the Together console, set the parameter, bounce `gate-model` and `gate-canary`. Revoke the old key after 1 h. |
| `telegram-agent-bot`, `telegram-ops-bot` | BotFather `/revoke`, set the parameter, bounce the readers, re-run `scripts/set_webhook.py --stage prod`. |
| `github-proposal-token` | New fine-grained token (contents + pull requests, this repo), set the parameter, bounce `gate-proposal`. |
| `webhook-secret` | `scripts/set_webhook.py --rotate` regenerates the parameter, the URL path segment and the Telegram webhook in one step. |

## 9. Incident playbooks

**Level 2 pause fired.** Read the journal entry and the trace. If `access.denied`: find the exact action in CloudTrail; if it is a bug (typo in a new proposal), fix through a PR; if the agent attempted something it should not, review the diary and the prompt diff since the last deploy before resuming. Resume only after the cause is understood.

**Cost spike.** `scripts/usage.py`; check `governor.skipped` counts (a stuck lock keeps heartbeats off, a missing lock lets them run), cache hit ratio, and `heartbeat.completed.usage` per cycle. Lower a role budget or pause.

**User gets no replies.** Check `gate-telegram` 403 alarm (webhook secret), `inbound-messages-dlq` depth, dispatcher logs for the trace, agent concurrency (paused?). `scripts/trace.py` with the update id.

**Supervisor silent alarm.** Check the supervisor DLQ and errors; the dumb alarms still protect; fix the supervisor through a PR; replay missed events from the bus archive with `scripts/replay_archive.py --since <ts>`.

**Canary keeps failing on a healthy-looking change.** Read the assertion output in the CodeDeploy hook logs. If an assertion is flaky, make it deterministic or delete it through a PR to `contracts/canary-assertions.json`; do not retry the deploy until it passes.

## 10. New stage or new user

1. Create the Telegram bot with BotFather. Run `scripts/params.py init --stage <name>`; it reads `TOGETHER_API_KEY` from the environment, prompts for the bot token, reads the user chat id from Telegram, and writes all seven SSM parameters (five as `SecureString`). It is idempotent — re-running keeps what is already set unless `--overwrite` is given — and it never prints a secret. `scripts/params.py show` lists what is set, by length and SHA-256 fingerprint.
2. `make stage-up NAME=<name>` (all stacks; alarms only if `NAME=prod`).
3. `scripts/set_webhook.py --stage <name>`.
4. Send `/start` to the bot from the user chat; the first reply confirms the user chat id matches the parameter.
5. `scripts/flags.py --stage <name> set heartbeat_enabled true`.
6. For a throwaway stage, `make stage-down NAME=<name>` removes everything, including tables and the bucket (retention policies are `DESTROY` for non-prod stages, `RETAIN` for `prod`).

## 11. Lifecycle: backup, stop, destroy

Three commands, no runbook required. Each is a thin CLI over `ops/`, idempotent, safe to interrupt and re-run, and journaled with `by = human:<iam user>`.

```bash
make backup STAGE=prod           # uv run scripts/backup.py --stage prod
make stop   STAGE=prod           # uv run scripts/stop.py   --stage prod
make destroy STAGE=prod I_KNOW=1 # uv run scripts/destroy.py --stage prod --i-know
```

### 11.1 Backup

`scripts/backup.py --stage <name> [--out <dir>] [--include-secrets] [--verify]`

Default output `./backups/<stage>/<UTC ISO>/`. Writes:

| Path in the backup | Content |
|---|---|
| `manifest.json` | `schema_version`, stage, account, region, UTC start/end, stack names and outputs, per-table item counts, per-prefix object counts and bytes, vector count, tool versions |
| `dynamodb/<table>.jsonl` | One item per line, DynamoDB JSON, from a paginated `Scan` of every table in [09-data-model.md](09-data-model.md) §1 (both namespaces; canary items included and marked) |
| `s3/<prefix>/…` | Current versions of every durable prefix: `profile/`, `memories/`, `artifacts/`, `drafts/`, `conversations/`, `proposals/`, `prompts/deployed/`. `sessions/` only with `--include-sessions`; `exports/` and `canary/` never |
| `s3/profile/history/<version-id>.md` | All noncurrent versions of `profile/profile.md` (kept forever by lifecycle, so they belong in the backup) |
| `vectors/memory.jsonl` | `mem_id`, vector, metadata per item, and the `embedding_model` recorded on the memory |
| `params.json` | Parameter names, types and last-modified. Values only for `String` parameters; SecureStrings are listed by name with a null value unless `--include-secrets`, which requires `--i-know` and writes them to a separate `secrets.json` the script warns about |
| `state.json` | Flags, `STATE#pause`, `STATE#next_wake`, current counters — the operational state a restore must not silently carry over |

`--verify` re-reads the backup and asserts counts against a fresh count of the live stage, then prints `OK <n> items / <n> objects / <n> vectors`. Backups contain the user's personal data in the clear: the directory is chmod 700 on POSIX and the script refuses to write into a path under a synced cloud folder it recognises unless `--i-know`.

`scripts/restore.py --stage <name> --from <dir> [--dry-run] [--only dynamodb|s3|vectors|params]` restores into a **fresh** stage (deploy it first with `make stage-up`). It refuses a target whose tables are non-empty without `--overwrite`, replays items with `BatchWriteItem`, re-uploads objects, re-puts vectors (no re-embedding — vectors come from the backup; use `scripts/reindex.py` if the embedding model changed), and never restores `state.json` flags to `true`: the restored stage comes up paused with `heartbeat_enabled = false`, and the operator turns it on.

### 11.2 Stop

`scripts/stop.py --stage <name> [--reason …]` is the full stop, one step stronger than [§1](#1-pause-and-resume)'s pause:

1. Everything `pause.py` does: disable both governor rules, reserved concurrency 0 on both agent Lambdas, write `PAUSE#`.
2. Delete the Telegram webhook for the stage's agent bot, so no inbound message is even accepted.
3. Journal `stopped`, notify the ops channel.

Nothing runs and nothing bills afterwards except storage. `scripts/start.py --stage <name>` reverses all of it (re-registers the webhook via `set_webhook.py`, restores concurrency, enables the rules, clears `PAUSE#`) and, like `resume.py`, requires typing the stage name and refuses on an unacknowledged Level 2.

### 11.3 Destroy

`scripts/destroy.py --stage <name> [--i-know] [--skip-backup] [--keep-backups] [--dry-run]`

Order, with each step skipped if already done:

1. `stop.py` — no inbound, nothing scheduled, nothing in flight.
2. `backup.py --verify` into `./backups/<stage>/pre-destroy-<ts>/`. `--skip-backup` is refused for `prod`.
3. Drain and delete the queues and their DLQs, delete the EventBridge rules, the bus and its archive.
4. `cdk destroy` all stacks in reverse dependency order (supervisor, agent, gates, sandbox, storage, pipeline, foundation).
5. **Retained-resource sweep** (`prod` only, where removal policies are `RETAIN`): empty the versioned bucket including all noncurrent versions and delete markers, delete the bucket; delete the vector index and vector bucket; delete each DynamoDB table; delete the SSM parameters; delete the CloudWatch log groups, the six alarms and the custom metric filter; delete the AWS Budget. Each deletion is listed before it happens and the totals are printed.
6. Print what could not be deleted and why (most often a resource created outside the CDK app), then a final line naming the backup directory.

`--dry-run` prints the full plan and deletes nothing. For any stage the script requires the stage name typed once; for `prod` it requires `--i-know` **and** the stage name typed a second time after the deletion list is printed. `--keep-backups` is the default: the backup directory is never inside anything the script deletes. After a successful destroy the AWS bill for the stage is zero; nothing in the design leaves an orphaned always-on resource, and `make destroy STAGE=<name> --dry-run` on a already-destroyed stage exits 0 with "nothing to do".
