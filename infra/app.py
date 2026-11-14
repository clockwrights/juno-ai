#!/usr/bin/env python3
"""CDK application entry point.

One app, one stage per synthesis. `-c stage=<name>` selects it; there is no
default, because a bare `cdk deploy` that silently meant `prod` would be a
footgun. Alarms exist only on `prod` (ADR-23), which is the one behavioural
difference between stages.

`build_app` is importable so the tests synthesize *this* app rather than a
reconstruction of it — a copy in a test fixture would drift from the real one
exactly when it mattered.
"""

from __future__ import annotations

import sys
from pathlib import Path

import aws_cdk as cdk

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.stacks.agent import AgentStack
from infra.stacks.foundation import FoundationStack
from infra.stacks.gates import GatesStack
from infra.stacks.sandbox import SandboxStack
from infra.stacks.storage import StorageStack
from infra.stacks.supervisor import SupervisorStack
from ops import localenv

# One account, one region (ADR-31, ADR-37, ADR-38). Both belong to whoever
# deploys this clone, so neither is committed. In order of precedence each comes
# from `-c account=` / `-c region=`, the `JUNO_AWS_ACCOUNT` / `JUNO_AWS_REGION`
# environment variables, or the same keys in the gitignored `.env` (template:
# `.env.example`), read by `ops/localenv.py` so `make`, a bare `npx cdk` and the
# lifecycle scripts all agree. The region defaults to `eu-central-1`.
# `CDK_DEFAULT_*` is never read: it once meant a shell configured for another
# region would have deployed there without saying so.
#
# With no account set, synthesis still works — tests and CI need no credentials
# and no account — against a placeholder no deploy can reach: CDK refuses a
# deploy whose credentials belong to another account, and a deploy built with
# `-c require_real_code=1` refuses the placeholder before that.
ACCOUNT_ENV = localenv.ACCOUNT_KEY
REGION_ENV = localenv.REGION_KEY
PLACEHOLDER_ACCOUNT = "000000000000"
REGION = localenv.DEFAULT_REGION


def resolve_account(app: cdk.App) -> str:
    account = str(app.node.try_get_context("account") or localenv.setting(ACCOUNT_ENV))
    if not account:
        if app.node.try_get_context("require_real_code"):
            raise SystemExit(
                f"no AWS account: set {ACCOUNT_ENV} in .env (see .env.example) "
                "or pass -c account=<id>"
            )
        return PLACEHOLDER_ACCOUNT
    if not (account.isdigit() and len(account) == 12):
        raise SystemExit(f"AWS account id must be 12 digits, got {account!r}")
    return account


def resolve_environment(app: cdk.App) -> cdk.Environment:
    return cdk.Environment(
        account=resolve_account(app),
        region=str(app.node.try_get_context("region") or localenv.region()),
    )


def build_app(app: cdk.App) -> cdk.App:
    """Add every stack for the stage in the app's context."""
    stage = app.node.try_get_context("stage")
    if not stage:
        raise SystemExit("pass -c stage=<name>, e.g. `cdk synth -c stage=dev`")

    common = {
        "env": resolve_environment(app),
        "stage": stage,
        # Only prod gets alarms, so the account stays inside the free tier of ten.
        "is_prod": stage == "prod",
    }

    foundation = FoundationStack(app, f"{stage}-foundation", **common)
    storage = StorageStack(app, f"{stage}-storage", foundation=foundation, **common)
    sandbox = SandboxStack(app, f"{stage}-sandbox", foundation=foundation, **common)
    gates = GatesStack(app, f"{stage}-gates", foundation=foundation, storage=storage, **common)
    agent = AgentStack(
        app,
        f"{stage}-agent",
        foundation=foundation,
        storage=storage,
        gates=gates,
        sandbox=sandbox,
        **common,
    )
    SupervisorStack(
        app,
        f"{stage}-supervisor",
        foundation=foundation,
        storage=storage,
        gates=gates,
        agent=agent,
        **common,
    )
    cdk.Tags.of(app).add("project", "autonomous-agent")
    cdk.Tags.of(app).add("stage", stage)
    return app


if __name__ == "__main__":
    build_app(cdk.App()).synth()
