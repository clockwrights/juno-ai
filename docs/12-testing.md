# 12 — Testing

Principle 9: every guardrail is a test. This document defines the test pyramid, the fakes, and which tests gate what.

## 1. Pyramid

| Layer | Marker | Runs where | Talks to | Gate |
|---|---|---|---|---|
| Unit | `unit` (default) | every PR, < 2 min | moto fakes, `InMemoryIndex`, `FakeModelClient` | PR merge |
| Contract | `contract` | every PR | schemas only | PR merge |
| Infra assertions | part of `unit` | every PR | `cdk synth` output | PR merge |
| Integration | `integration` | after `dev` deploy on `main` | real dev stage, real model through the gateway | `prod` deploy |
| Canary | CodeDeploy hook | every `prod` deploy | candidate version in canary namespace | alias shift |
| Live eval | `live` | manual / weekly | real model, recorded conversations | none (report) |

## 2. Fakes

### `FakeModelClient`

Cassette-based. A cassette is a JSON list of responses in OpenAI-compatible chat-completion shape (`content`, `reasoning_content`, `tool_calls`, `finish_reason`, `usage`). The fake returns them in order and records every request. Tests assert on recorded requests (prefix stability, tool lists, placement of the volatile system block) and on side effects in stores. Cassettes live in `tests/fixtures/model/<scenario>.json` and are hand-written or captured from `dev` with `scripts/capture_cassette.py` (content scrubbed).

Because the fake implements the same interface as `ModelClient` (`invoke(role, request) → Message | error`), the runtime code under test is the production code, not a test double of the loop.

### AWS

`moto` for DynamoDB (including conditional expressions, transactions and GSIs), S3 (including `If-Match`), SQS, Lambda invoke (with a Python-side dispatch table so `gate-dispatcher` unit tests call the real `agent-conversation` handler in-process), EventBridge put_events (captured), SSM.

Vectors: `InMemoryIndex` (numpy cosine, same filter semantics). A shared behaviour suite `tests/unit/storage/vector_suite.py` runs against both implementations; the S3 Vectors run is marked `integration`.

Telegram: `FakeTelegram` records `sendMessage` calls; integration tests use a real dev bot and a dev observer bot in the same chat.

## 3. Infra assertions (`infra/tests/`)

Using `aws_cdk.assertions.Template` on each synthesized stack:

- **Role allowlists**: `infra/iam/allowlists.py` is the single source of truth for what each role may do (actions, resource patterns, conditions). The test synthesizes, extracts each role's policy statements, normalises them, and asserts set equality with the allowlist. Adding a permission requires editing the allowlist in the same PR, which CODEOWNERS routes to the user.
- **Forbidden actions**: for agent roles, assert none of `secretsmanager:*`, `scheduler:*`, `events:PutRule`, `events:EnableRule`, `iam:*`, `lambda:Update*`, `lambda:PutFunctionConcurrency`, `codedeploy:*`, `dynamodb:DeleteItem`, `dynamodb:Scan`, `s3:DeleteObject`, `ec2:*`.
- **Executor**: exactly two `logs:` actions; VPC config present; subnet route tables have no NAT or IGW routes; no VPC endpoints in that VPC.
- **Zero always-on cost**: no NAT gateway, no VPC interface endpoint, no API Gateway, no Secrets Manager secret anywhere in the synthesized templates; no function other than the executor has a `VpcConfig`; at most six alarms and one custom metric, and only on `prod`.
- **Secrets**: no Lambda environment variable matches a token or key pattern; `ssm:GetParameter` on each SecureString is granted only to the listed roles; agent roles have no `ssm:*`.
- **Concurrency**: reserved concurrency per function equals the table in [06-enforcement-layer.md](06-enforcement-layer.md).
- **Aliases and deployment**: agent functions have alias `live`, a deployment group with the pre-traffic hook (`gate-canary`) and the three rollback alarms.
- **Gate aliases**: `gate-model` has aliases `agent`, `supervisor`, `canary` with resource policies naming only the specified principals.
- **Hermetic synthesis**: `infra/tests/test_synth_is_hermetic.py` builds the real `infra/app.py` and asserts CDK's own `missing` list is empty — that is, synthesis performs no context lookup and therefore needs no AWS credentials. It also asserts the account and region are the pinned ones. Without this, a lookup cached in a developer's gitignored `cdk.context.json` passes locally and fails in CI forever.
- **Snapshot**: a template snapshot per stack; changes require the snapshot to be updated in the PR, making infra diffs visible in review. *(planned)*

## 4. Contract tests (`tests/contract/`)

- Every schema loads and is draft 2020-12 valid.
- `contracts/examples/<name>/*.json` validate; at least one example per schema, including one canary-mode example where applicable.
- Generated `core/contracts.py` is up to date (regenerate and diff).
- Producer/consumer pairs: each gate's output is validated against the consumer's schema in unit tests (e.g. `gate-governor` builds a `heartbeat-request` that `agent-heartbeat` accepts).
- Tool schemas are `strict`-compatible: `additionalProperties: false` and `required` listing every property.

## 5. Unit test conventions

- One test module per production module. Behaviour names as test names (`test_third_notification_becomes_note`).
- No sleeping; time is injected (`core/clock.py` with a `FrozenClock` in tests).
- Every bug fix adds a regression test named after the incident or PR.
- Coverage gate 85% on `core/` and `agent/runtime/`, 90% on `gates/` and `supervisor/`.

## 6. Integration tests (`tests/integration/`)

- Run locally with `pytest -m integration --stage test-<id>` by whoever deploys, with AWS credentials, against an ephemeral stage they deploy and destroy (`make stage-up NAME=…`, `make stage-down NAME=…`). CI never deploys.
- Each test creates its own namespace suffix (`trace_id`) for keys where possible and cleans up what it created. Shared resources (counters, pause flag) are reset by fixtures and never run in parallel (`pytest -p no:xdist` for this marker).
- Budget for the run: the fixtures set the dev token budgets to 200k per role and restore them after.
- Timeouts are generous: heartbeat tests wait up to 16 minutes.
- Failure of any integration test blocks the `prod` deploy.

## 7. Canary

Specified in [08-self-modification.md](08-self-modification.md) §4. Canary fixtures are in `tests/canary/fixtures/` so they are versioned with the code but the assertion list is in `contracts/` (immutable).

## 8. Live eval (informative)

`tests/live/` holds 20 recorded user conversations with expected memory outcomes (what should be saved, what should be in the profile). `scripts/run_live_eval.py` replays them against `dev` with a fresh namespace and scores with an LLM judge (`zai-org/GLM-5.3`, structured output). The score is reported in the weekly soak report and used when tuning prompts. It never gates a deploy because it is nondeterministic and costs real money.

## 9. What runs when

| Trigger | Jobs |
|---|---|
| PR opened / updated | lint, mypy, unit, contract, infra assertions, invariants, proposal path check, prompt advisory (if prompts changed) |
| Push to `main` | the same CI checks on the merge commit |
| Before a `prod` deploy (user's machine) | `make ci`, `make diff`; the deploy itself runs the canary and bake |
| On demand (user's machine) | ephemeral stage → integration → destroy; `make live-eval`; `make canary-dry-run` (runs canary assertions against `prod`'s live alias in the canary namespace) |
| Weekly (Sunday after maintenance, in AWS) | weekly report to the ops channel |

The canary guards every `prod` deploy. A PR that changes `gates/`, `infra/` or `contracts/` should be followed by a local integration run before merging.
