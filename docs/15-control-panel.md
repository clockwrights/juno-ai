# 15 — Control Panel

## 1. Purpose

One place where the user can see everything the system does, including what the agent was thinking, talk to the agent, review and diff its prompts and code changes, and delete or export any of it. It is the user's window into all three layers, and it is the primary operations surface ([14-operations.md](14-operations.md)).

## 2. Shape

- A local web app: FastAPI backend, server-rendered templates with HTMX, no build step. `uv run panel --stage prod` opens `http://127.0.0.1:8788`.
- Binds to loopback only. There is no login page because there is no network exposure; authority comes from the user's AWS credentials, which the app uses to assume `agent-admin-<stage>`. Starting it without a resolvable admin role fails immediately.
- Every read and every action goes through `ops/`, the same library the CLI scripts use. The panel adds no capability that a script does not have; it adds visibility and convenience.
- Cost: zero. Transient exports go to `exports/` in the bucket with a one-day lifecycle so large downloads stream from S3 instead of through the app.
- Mutating routes are POST with a CSRF token. Destructive routes require typing the stage name and show what will be deleted with sizes before acting. Every action is journaled with `surface = panel`.

## 3. Data sources

| Page | Reads | Writes / actions |
|---|---|---|
| Overview | `agent_state` (pause, flags, counters, next wake, locks), last `heartbeat.completed`, last supervisor decisions, open proposals, today's token usage per role, storage totals | pause, resume, flags, wake now |
| Activity | `agent_sessions` index, `supervisor_journal`, `agent_diary`, `agent_proposals` events, merged into one timeline | none |
| Sessions | `agent_sessions` + `sessions/` records | delete session(s), export |
| Chat | `agent_conversations` for `panel:owner` | enqueue inbound message (`channel = panel`) |
| Conversations | all `agent_conversations`, both channels | close into episode now, export, delete |
| Memory | `agent_memory`, `agent_memory_tags`, vector search via `MemoryStore.search` (without refreshing `last_accessed`) | forget (tombstone, reason `owner`), hard delete, export |
| Profile | `profile/profile.md` and its S3 versions | export; restore a prior version |
| Diary | `agent_diary` | export, delete range |
| Prompts | `prompts/deployed/<version>/`, `invariants/`, the live alias version | diff any two versions; open the PR that changed them |
| Proposals & code | `agent_proposals`, `proposals/<id>/diff.patch`, `deploy.*` events, CodeDeploy history | open PR link, rollback alias |
| Supervisor | `supervisor_journal`, evidence packs, the supervisor's own sessions | acknowledge a Level 2 (required before resume) |
| Logs | CloudWatch `FilterLogEvents` by `trace_id` across all log groups, gateway call log lines | export |
| Artifacts | `artifacts/`, `drafts/` | promote draft, delete |
| Storage | sizes per table and prefix, oldest/newest, counts by kind | bulk delete by age and kind, purge tombstones, purge canary leftovers |
| Reports | weekly report (cache hit ratio, token burn per role, governor skip reasons, 403 counts, consolidation stats, cost estimate) | export |
| Settings | SSM parameters (values masked), CDK context values in effect | set a parameter, bounce a function |

## 4. The session viewer

This is the page that answers "what came to the agent's mind". A session is one `trace_id`: a conversation turn, a heartbeat cycle, a supervisor decision or a canary probe. The viewer shows, in order:

1. **Header**: kind, role, model, started/ended, calls, tokens (input, output, cache read), outcome, links to the conversation or diary entry, the deployed prompt version in effect.
2. **System prompt** as sent on the first call, with the stable prefix and the volatile per-turn block marked and invariant clauses highlighted; later calls show only what changed.
3. **Per call**: the request delta (new messages since the previous call), then the response in order: `reasoning_content` rendered as a collapsed quoted block (raw reasoning, often long — collapsed by default), text, each `tool_calls` entry with pretty-printed arguments, and the `role: "tool"` result the runtime sent back on the next call, with duration and error flag.
4. **Refusals**: `finish_reason = content_filter` shows the request that triggered it and whether the single retry against `zai-org/GLM-5.2` succeeded.
5. **Footer**: gateway latency per call, budget remaining after the session.

Sessions are recorded by `gate-model` before the response reaches the caller, so the viewer is complete even for turns that crashed after the model call.

## 5. Chat

The Chat page is a second channel to the same agent. It writes an `inbound-messages` entry with `channel = panel` and `conversation_id = panel:owner`, then polls `agent_conversations` (HTMX every 2 s) until the assistant message appears or 100 s pass. The conversation is separate from the Telegram one, but memory, profile, notes and diary are shared, so the agent knows what was said on either channel once episodes close. A "close conversation now" button turns the panel conversation into an episode immediately.

## 6. Deletion and export semantics

- **Export** produces a zip in `exports/` with markdown for human reading and JSON for machines, then streams it to the browser. Scopes: one session, a day of sessions, a conversation, all memory, the diary, the profile history, the supervisor journal, or everything.
- **Delete** is always explicit about scope and shows counts and bytes first. Sessions and logs are the expected targets ("free up space"). Memory deletion is offered because it is the user's data, with the warning that the agent will not know it forgot; tombstoning is the default and hard delete is a second step.
- Deleting a session removes its S3 records and its index item. Deleting a conversation removes its messages, keeps the episode memory unless that is also selected, and leaves the sessions that produced it unless also selected.
- Nothing in the panel can delete supervisor journal entries, proposal events or deployed prompt versions. Those are the audit trail; they are small and stay.
- Every delete writes a journal entry with counts.

## 7. Implementation notes

- `ops/readers.py` returns plain dataclasses; templates never touch boto3.
- Long lists are paginated by DynamoDB cursors; the timeline merges four sources by timestamp with a bounded window (default 7 days).
- Diffs use `difflib` for prompts and render unified diffs for proposals; both are server-side, no JavaScript libraries.
- The app is tested with the FastAPI test client and moto. A `--demo` flag seeds a local moto instance with fixture data so the panel can be developed and demonstrated without AWS.
