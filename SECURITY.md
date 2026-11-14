# Security policy

Juno is an autonomous agent whose safety rests on boundaries it cannot cross:
IAM, network routing, the model gateway's budgets, the supervisor, and the
three enforcements on what a self-modification proposal may touch
([docs/01](docs/01-principles-and-layers.md), [docs/06](docs/06-enforcement-layer.md),
[docs/08](docs/08-self-modification.md)). A flaw in any of them is a security
issue, even if it needs the agent's own cooperation to exploit.

## Reporting a vulnerability

**Do not open a public issue or pull request.** Report privately through
GitHub: the repository's **Security** tab → **Report a vulnerability**.

Please include what you can of:

- the boundary affected and how it can be crossed,
- the commit you tested against and the steps to reproduce,
- whether it needs a deployed stage, and what access the attacker starts with.

This is a personal project maintained in spare time, with no bug bounty. Expect
an acknowledgement within about a week. Fixes land on `main`; there are no
release branches to backport to.

## In scope

- Anything that lets the agent, or a message sent to it, reach outside its
  layer: modify `gates/`, `infra/`, `supervisor/`, `contracts/` or
  `invariants/`; get a proposal merged past the path checks; read a secret;
  reach the internet; exceed a token or notification budget; avoid being paused.
- IAM policies, resource policies and network rules synthesized by `infra/`
  that are broader than the docs specify.
- The Telegram webhook (Function URL) accepting unauthenticated or
  unauthorised input.
- The local control panel (`panel/`) being reachable from another machine or
  acting beyond the user's request.
- Secrets or personal data written somewhere the docs say they must not be
  (logs, CloudWatch, the repository, CI).

## Out of scope

- Your own deployment's configuration: an AWS account, provider key or
  Telegram bot you have shared or exposed yourself.
- The model saying something unwanted without crossing a boundary. Prompts are
  not a security control here by design; report it as an ordinary issue.
- Costs within the documented budgets, and AWS or Together AI service issues.
- Vulnerabilities in dependencies with no demonstrated path through this code
  (Dependabot-style alerts are welcome as ordinary issues).

## If you run a copy

You operate your deployment; nobody else can patch it for you. Keep `main`
pulled, re-run `make ci` and `make diff` before `make stage-up`, set a spend
limit with your model provider, and use `make stop STAGE=prod` to pause the
agent if you suspect it is misbehaving ([docs/14](docs/14-operations.md)).
