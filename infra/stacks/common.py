"""Shared construct helpers.

Two things live here because getting them wrong is expensive and repeating them
is how they drift: how a Lambda is built (arm64, 14-day logs, no secrets in the
environment, reserved concurrency always set) and the placeholder asset used
until each VP supplies a real handler.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

ROOT = Path(__file__).resolve().parent.parent.parent
PLACEHOLDER_DIR = ROOT / "infra" / "placeholder"

#: Written by `scripts/build_lambdas.py` (ADR-26). Absent in CI and in the
#: assertion tests, which synthesize without building; see `function_code`.
BUILD_DIR = ROOT / "build" / "lambda"

#: The handler each function runs, as `module.path.function`. Nine of them share
#: one asset and differ only here, so the dependency tree is hashed and uploaded
#: once. A name absent from this table keeps the placeholder: `gate-proposal`,
#: `gate-canary` and `ops-alarms` have no handler module yet, and shipping them
#: an entry point that does not exist would turn a known gap into an import
#: error at the first invocation.
HANDLERS: dict[str, str] = {
    "agent-conversation": "agent.conversation.handler.handler",
    "agent-heartbeat": "agent.heartbeat.handler.handler",
    "gate-telegram": "gates.telegram.handler.handler",
    "gate-dispatcher": "gates.dispatcher.handler.handler",
    "gate-governor": "gates.governor.handler.handler",
    "gate-notify": "gates.notify.handler.handler",
    "gate-model": "gates.model.handler.handler",
    "supervisor": "supervisor.handler.handler",
    # Its own asset, flattened, so `handler.py` sits at the root (ADR-26).
    "sandbox-executor": "handler.handler",
}

#: docs/06 §8. The account cannot fan out, whatever the agent does.
RESERVED_CONCURRENCY: dict[str, int] = {
    "agent-conversation": 2,
    "agent-heartbeat": 1,
    "sandbox-executor": 2,
    "gate-model": 4,
    "gate-telegram": 2,
    "gate-dispatcher": 2,
    "gate-governor": 1,
    "gate-notify": 1,
    "gate-proposal": 1,
    "gate-canary": 1,
    "supervisor": 2,
    "ops-alarms": 1,
}

LOG_RETENTION = logs.RetentionDays.TWO_WEEKS


class StageStack(cdk.Stack):
    """A stack that knows its stage and whether it is the alarmed one."""

    def __init__(
        self, scope: Construct, construct_id: str, *, stage: str, is_prod: bool, **kwargs: Any
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.stage = stage
        self.is_prod = is_prod

    @property
    def availability_zones(self) -> list[str]:
        """Fixed AZs, so synthesis never needs AWS credentials.

        `ec2.Vpc` otherwise asks the account which zones exist. That is a
        context lookup: it needs credentials, it caches into `cdk.context.json`,
        and a cached answer makes the problem invisible on a developer machine
        while CI — which has no credentials on purpose (docs/12 §1) — fails.

        Two zones in every commercial region, and the sandbox VPC has no NAT
        and no endpoints, so nothing here is per-zone billed. Naming them
        explicitly also makes the synthesized template deterministic, which is
        what the assertion tests in `infra/tests/` compare against.
        """
        return [f"{self.region}a", f"{self.region}b"]

    def name(self, suffix: str) -> str:
        """Stage-prefixed resource name; every ARN in the allowlists assumes this."""
        return f"{self.stage}-{suffix}"


def placeholder_code() -> lambda_.Code:
    """The `{"not_implemented": true}` handler used until a VP supplies the real one.

    VP0 creates every function with its final role, name, concurrency and log
    group so later VPs replace only the code (docs/11 VP0).
    """
    PLACEHOLDER_DIR.mkdir(parents=True, exist_ok=True)
    handler = PLACEHOLDER_DIR / "handler.py"
    if not handler.exists():
        handler.write_text(
            '"""Placeholder handler. Replaced by the VP that owns this function."""\n\n'
            "from typing import Any\n\n\n"
            "def handler(event: Any, context: Any) -> dict[str, Any]:\n"
            '    return {"not_implemented": True}\n',
            encoding="utf-8",
        )
    return lambda_.Code.from_asset(str(PLACEHOLDER_DIR))


def reserved_concurrency(scope: Construct, short_name: str) -> int | None:
    """The function's reserved concurrency, or None when the account cannot.

    Reserving concurrency needs `sum(reserved) <= account limit - 10`: AWS keeps
    10 unreserved, always. A fresh AWS account's limit *is* 10, so nothing can be
    reserved at all until the quota is raised, and CloudFormation says so only on
    deploy — "decreases account's UnreservedConcurrentExecution below its minimum
    value of [10]".

    `-c reserved_concurrency=off` is the escape hatch, and it is deliberately
    ugly: it turns off a Layer 3 guarantee (docs/06 §8), so it should be visible
    in the command that used it and removed the moment the quota allows. The
    default keeps the reservations, which is what CI synthesizes and what the
    assertion tests read (ADR-34).
    """
    if str(scope.node.try_get_context("reserved_concurrency") or "").lower() == "off":
        return None
    return RESERVED_CONCURRENCY[short_name]


def function_code(scope: Construct, short_name: str) -> tuple[lambda_.Code, str]:
    """The asset and `Handler` for one function, built or placeholder.

    Synthesis must work with no build directory: CI synthesizes to compare
    templates (docs/12 §1) and must not depend on a 40 MB `pip install`. So a
    missing build falls back to the placeholder — which is right for a template
    diff and catastrophic for a deploy, because it would quietly replace working
    code with `{"not_implemented": true}`.

    `-c require_real_code=1` closes that gap, and `make stage-up` passes it after
    running the build. `cdk deploy` typed by hand without it is the one case this
    cannot catch, which is why the Makefile is the documented path.
    """
    built = short_name in HANDLERS
    asset = BUILD_DIR / ("sandbox" if short_name == "sandbox-executor" else "app")
    if built and asset.is_dir():
        return lambda_.Code.from_asset(str(asset)), HANDLERS[short_name]
    if built and scope.node.try_get_context("require_real_code"):
        raise RuntimeError(
            f"{short_name} has a handler but {asset} does not exist. "
            "Run `make package` (uv run python scripts/build_lambdas.py) first."
        )
    return placeholder_code(), "handler.handler"


def make_function(
    scope: Construct,
    *,
    stack: StageStack,
    short_name: str,
    role: iam.Role,
    timeout_seconds: int = 30,
    memory_mb: int = 512,
    environment: dict[str, str] | None = None,
    code: lambda_.Code | None = None,
    handler: str | None = None,
    vpc_args: dict[str, Any] | None = None,
) -> lambda_.Function:
    """Create a Lambda with the invariants every function in this system shares.

    - arm64 and Python 3.12 everywhere (docs/02 §1).
    - Its own role; never a shared or auto-generated one.
    - Reserved concurrency always set, from the table above.
    - 14-day JSON logs (docs/06 §8).
    - `STAGE` in the environment, never a secret (asserted by test_secrets.py).
    """
    env = {"STAGE": stack.stage, "LOG_LEVEL": "INFO", **(environment or {})}
    built_code, built_handler = function_code(scope, short_name)
    kwargs: dict[str, Any] = {}
    if vpc_args:
        kwargs.update(vpc_args)

    # An explicit LogGroup, not `log_retention`: that prop provisions a
    # custom-resource Lambda *per function* to set retention after the fact,
    # which would add a dozen functions and roles nobody declared and break the
    # IAM and concurrency assertions.
    log_group = logs.LogGroup(
        scope,
        f"{short_name}-logs",
        log_group_name=f"/aws/lambda/{stack.name(short_name)}",
        retention=LOG_RETENTION,
        removal_policy=cdk.RemovalPolicy.DESTROY,
    )

    return lambda_.Function(
        scope,
        short_name,
        function_name=stack.name(short_name),
        runtime=lambda_.Runtime.PYTHON_3_12,
        architecture=lambda_.Architecture.ARM_64,
        handler=handler or built_handler,
        code=code or built_code,
        role=role,
        timeout=cdk.Duration.seconds(timeout_seconds),
        memory_size=memory_mb,
        environment=env,
        reserved_concurrent_executions=reserved_concurrency(scope, short_name),
        log_group=log_group,
        **kwargs,
    )


def lambda_role(scope: Construct, *, stack: StageStack, short_name: str) -> iam.Role:
    """One role per function, named for it, with only the basic execution policy.

    `AWSLambdaBasicExecutionRole` is the CloudWatch Logs write path. Everything
    else is added explicitly by the stack and asserted against
    `infra/iam/allowlists.py`.
    """
    return iam.Role(
        scope,
        f"{short_name}-role",
        role_name=stack.name(f"{short_name}-role"),
        assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "service-role/AWSLambdaBasicExecutionRole"
            )
        ],
    )
