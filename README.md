# Juno — a personal autonomous agent

A serverless, memory-centric autonomous agent on AWS, with an independent
supervisor that can pause it, a self-modification pipeline no part of the agent
can reach, and a control panel that runs on your own laptop. You talk to it over
Telegram.

This repository is a **design and an implementation to run yourself**, in your
own AWS account, with your own model key. It is not a hosted service, and
nobody operates a production copy of it for you.

> **Status: work in progress.** Storage, the model gateway, the sandbox, the
> conversation and heartbeat paths, the supervisor and alarms, memory
> maintenance and the control panel are written, and their unit, contract and
> infrastructure tests pass. A full deploy and teardown has been done once.
> Not done yet: integration tests, the proposal and canary handlers with the
> CodeDeploy rollout ([docs/08](docs/08-self-modification.md) §3.4), and some
> operations scripts named in [docs/14](docs/14-operations.md).

## Read this before deploying

- **It is an autonomous agent.** It wakes itself up on a schedule, keeps long-term
  memory about you, runs code in a sandbox and messages you. The guardrails are
  real (below), but you are responsible for what you deploy and for watching it.
- **You pay for it.** AWS is designed to cost close to nothing (see Cost). The
  model provider (Together AI) bills separately and is capped only by the daily
  token budgets in `gates/model/policy.py` and the spend limit **you** set in
  the provider's console. Set one.
- **No warranty and no support.** MIT-licensed, as is. See [LICENSE](LICENSE).

## What it is

Three layers, each simpler and harder to break than the one below:

| Layer | What | Can it be changed by the agent? |
|---|---|---|
| 1 — Agent | `agent/`, `core/`, prompts | Yes, by proposal + human approval |
| 2 — Supervisor | `supervisor/` | No. Read-only plus one power: pause |
| 3 — Enforcement | `infra/`, `gates/`, `contracts/`, `invariants/`, CI | No |

Every hard limit — token budgets, notification budgets, wake scheduling, network
access, what may be deployed — lives in Layer 3, as IAM, VPC routing, queue
consumers or CI. Never in prompt text alone.

**The design in `docs/` is normative.** Start at
[docs/README.md](docs/README.md) for the index, or
[docs/02-architecture.md](docs/02-architecture.md) §0 for the whole system on
one page. Models run on Together AI (GLM-5.3 and GLM-5.3-Flash), sized to a
$20/month total budget.

## Try it without AWS

```bash
uv sync            # Python 3.12 toolchain
npm ci             # the pinned CDK CLI
make ci            # lint, types, every test, both synths — no AWS credentials needed
```

On Windows without `make`, the Makefile header lists the `uv run` equivalent of
every target.

## Run your own copy

You need: an AWS account and an IAM identity with administrator access, a
region that offers S3 Vectors and Lambda arm64, a Together AI API key, and a
Telegram bot (create one with BotFather).

1. **Configure.** Copy `.env.example` to `.env` (gitignored) and set
   `JUNO_AWS_ACCOUNT`, and `JUNO_AWS_REGION` if not `eu-central-1`. Nothing
   deployment-specific is committed except the handle in `.github/CODEOWNERS`.
2. **Bootstrap CDK** once per account and region:
   `npx cdk bootstrap aws://<account>/<region>`.
3. **Check, then deploy** from your own machine — CI never deploys:

   ```bash
   make ci
   make diff NAME=prod
   make stage-up NAME=prod       # refuses anything but a clean `main`
   ```

4. **Write the secrets** into SSM. Send `/start` to your bot first, so the
   script can read your chat id:

   ```bash
   export TOGETHER_API_KEY=...   # read from the environment, never typed into a command line
   make params STAGE=prod        # prompts for the bot token without echoing it
   ```

5. **Read [docs/14-operations.md](docs/14-operations.md)** for pausing, flags,
   budgets, backups and incidents.

If your account's Lambda concurrency quota is below 30 (new accounts often have
10), the deploy still works but without per-function ceilings, and says so every
time. Ask Service Quotas for more.

### Using your own fork

CI (`.github/workflows/ci.yml`) works in any fork or copy with no configuration:
it holds no secrets and no AWS access. If you want the self-modification
pipeline (off by default), also:

- replace the handle in `.github/CODEOWNERS` with your own, and
- turn on branch protection for `main` (required PR, required CI, required
  CODEOWNER review, no force push) **before** creating the GitHub token or
  setting `proposals_enabled`. It is the third of three enforcements on what the
  agent may change ([docs/10](docs/10-repository-layout.md)).

## Lifecycle

Backing up, stopping and removing a stage are each one command:

```bash
make backup  STAGE=prod           # full offline export, verified
make stop    STAGE=prod           # pause and remove the Telegram webhook
make destroy STAGE=prod DRY_RUN=1 # print the plan, delete nothing
make destroy STAGE=prod I_KNOW=1  # stop, back up, destroy, sweep retained resources
```

`scripts/verify_destroyed.py` proves afterwards that nothing carrying the stage
prefix survives. Integration tests (not written yet, see Status) are meant to
run against a throwaway stage: `make stage-up NAME=test-1`,
`make integration NAME=test-1`, `make destroy STAGE=test-1`.

## Cost

The design targets effectively zero infrastructure cost: no NAT gateway, no VPC
endpoint, no API Gateway, no Secrets Manager, nothing provisioned. The tests
assert this on every synthesis (`infra/tests/test_network.py`). The model bill
is the only real cost, capped by daily per-role token budgets, an AWS Budget at
$20/month that pauses the agent at 90%, and your provider-side spend limit.
[docs/02-architecture.md](docs/02-architecture.md) §5 has the itemised estimate.

## Development

```bash
uv sync && npm ci
make ci            # exactly what CI runs: lint, types, contracts, tests, synth
```

Conventions: Python 3.12, `uv`, `ruff`, `mypy --strict`, `pytest`; conventional
commit prefixes (`feat`, `fix`, `test`, `docs`, `infra`). Contracts in
`contracts/` are frozen: a change needs a new `schema_version` and consumers
that accept both. Enforcement logic (budgets, bounds, permissions, secrets)
belongs in `gates/`, `infra/` or `supervisor/`, never in `agent/` or `core/`,
and every guardrail has a test ([docs/12](docs/12-testing.md)). Security issues:
see [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE) © [clockwrights](https://clockwrights.com/).
