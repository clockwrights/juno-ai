"""The single source of truth for what every role may do.

`infra/tests/test_iam_boundaries.py` synthesizes each stack, extracts the policy
statements attached to each role, normalises them and asserts **set equality**
with the entries here (docs/12 §3). Adding a permission therefore means editing
this file, which CODEOWNERS routes to the owner — the point being that a
widened IAM boundary can never be an invisible side effect of a code change.

Resource strings use `{stage}`, `{account}` and `{region}` placeholders and are
compared after substitution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

# Actions no agent role may ever hold, whatever else changes (docs/12 §3).
FORBIDDEN_AGENT_ACTIONS: Final[tuple[str, ...]] = (
    "secretsmanager:*",
    "scheduler:*",
    "events:PutRule",
    "events:EnableRule",
    "events:DisableRule",
    "iam:*",
    "lambda:Update*",
    "lambda:PutFunctionConcurrency",
    "codedeploy:*",
    "dynamodb:DeleteItem",
    "dynamodb:Scan",
    "s3:DeleteObject",
    "ec2:*",
    "ssm:GetParameter",
    "ssm:GetParameters",
    "sts:AssumeRole",
)


@dataclass(frozen=True)
class Grant:
    """One allowed (actions, resources) pair, optionally conditioned."""

    actions: tuple[str, ...]
    resources: tuple[str, ...]
    conditions: tuple[str, ...] = ()
    why: str = ""


@dataclass(frozen=True)
class RoleAllowlist:
    name: str
    grants: tuple[Grant, ...] = field(default_factory=tuple)


# ----------------------------------------------------------------- resources
T = "arn:aws:dynamodb:{region}:{account}:table/{stage}-"
B = "arn:aws:s3:::{stage}-agent-{account}"
F = "arn:aws:lambda:{region}:{account}:function:{stage}-"
Q = "arn:aws:sqs:{region}:{account}:{stage}-"
BUS = "arn:aws:events:{region}:{account}:event-bus/{stage}-agent-events"
PARAM = "arn:aws:ssm:{region}:{account}:parameter/agent/{stage}/"

AGENT_TABLE_ACTIONS = (
    "dynamodb:GetItem",
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:Query",
    "dynamodb:BatchGetItem",
    "dynamodb:BatchWriteItem",
    "dynamodb:TransactWriteItems",
)

# Both agent roles share the same shape; they differ only in which functions
# they may invoke and (in a later VP) which S3 prefixes they may write.
_AGENT_GRANTS: tuple[Grant, ...] = (
    Grant(
        actions=AGENT_TABLE_ACTIONS,
        resources=(
            T + "agent_memory",
            T + "agent_memory/index/*",
            T + "agent_memory_tags",
            T + "agent_diary",
            T + "agent_conversations",
            T + "agent_conversations/index/*",
            T + "agent_proposals",
        ),
        why="Memory, diary, conversation and proposal writes. No DeleteItem, no Scan (docs/09 §1).",
    ),
    Grant(
        actions=("dynamodb:GetItem", "dynamodb:Query", "dynamodb:UpdateItem", "dynamodb:PutItem"),
        resources=(T + "agent_state",),
        conditions=("dynamodb:LeadingKeys",),
        why="STATE#next_wake and NOTE# only. PAUSE# is a separate partition "
        "the agent cannot reach.",
    ),
    Grant(
        actions=("s3:GetObject", "s3:PutObject"),
        resources=(
            B + "/profile/*",
            B + "/memories/*",
            B + "/drafts/*",
            B + "/conversations/*",
            B + "/proposals/*",
            B + "/canary/*",
        ),
        why="Bodies and staging work. No DeleteObject anywhere (docs/09 §2).",
    ),
    Grant(
        actions=("s3:ListBucket",),
        resources=(B,),
        conditions=("s3:prefix",),
        why="artifact_list over drafts/ and artifacts/ only.",
    ),
    Grant(
        actions=("s3vectors:PutVectors", "s3vectors:QueryVectors", "s3vectors:GetVectors"),
        resources=(
            "arn:aws:s3vectors:{region}:{account}:bucket/{stage}-agent-vectors/index/memory",
        ),
        why="Embedding write and search. Delete belongs to gate-canary and admin only.",
    ),
    Grant(
        actions=("sqs:SendMessage",),
        resources=(Q + "outbound-notifications",),
        why="notify_user enqueues; gate-notify decides whether it is sent.",
    ),
    Grant(
        actions=("events:PutEvents",),
        resources=(BUS,),
        conditions=("events:source",),
        why="source must equal agent.runtime; the agent cannot impersonate a gate.",
    ),
)

AGENT_CONVERSATION = RoleAllowlist(
    name="agent-conversation",
    grants=(
        *_AGENT_GRANTS,
        Grant(
            actions=("lambda:InvokeFunction",),
            resources=(
                F + "gate-model:agent",
                F + "gate-model:embed",
                F + "sandbox-executor",
            ),
            why="The only functions the agent may call, and only through those "
            "two aliases. No web path exists (ADR-28).",
        ),
        Grant(
            actions=("s3:PutObject",),
            resources=(B + "/artifacts/*",),
            why="Canonical writes are allowed in a conversation turn only (docs/09 §2).",
        ),
    ),
)

AGENT_HEARTBEAT = RoleAllowlist(
    name="agent-heartbeat",
    grants=(
        *_AGENT_GRANTS,
        Grant(
            actions=("lambda:InvokeFunction",),
            resources=(
                F + "gate-model:agent",
                F + "gate-model:embed",
                F + "sandbox-executor",
            ),
            why="The same aliases. artifacts/ is deliberately absent: idle work stages to drafts/.",
        ),
    ),
)

SANDBOX_EXECUTOR = RoleAllowlist(
    name="sandbox-executor",
    grants=(
        Grant(
            actions=("logs:CreateLogStream", "logs:PutLogEvents"),
            resources=(
                "arn:aws:logs:{region}:{account}:log-group:/aws/lambda/{stage}-sandbox-executor:*",
            ),
            why="It runs agent-written code (docs/02 §2), so it gets the two log "
            "actions and nothing it could use.",
        ),
        Grant(
            actions=(
                "ec2:CreateNetworkInterface",
                "ec2:DescribeNetworkInterfaces",
                "ec2:DeleteNetworkInterface",
            ),
            resources=("*",),
            why="Forced by AWS: a Lambda in a VPC cannot be created unless its "
            "execution role can manage its own ENI, and Lambda requires these on "
            "`*`. Harmless here because the sandbox subnets have no route to the "
            "EC2 API — or anywhere else — so the credentials cannot be used from "
            "inside the sandbox (ADR-33).",
        ),
    ),
)

SUPERVISOR = RoleAllowlist(
    name="supervisor",
    grants=(
        Grant(
            actions=("dynamodb:GetItem", "dynamodb:Query"),
            resources=(
                T + "agent_memory",
                T + "agent_diary",
                T + "agent_state",
                T + "agent_conversations",
                T + "agent_proposals",
                T + "agent_sessions",
                T + "supervisor_journal",
                T + "supervisor_journal/index/*",
            ),
            why="Read-only evidence gathering.",
        ),
        Grant(
            actions=("dynamodb:PutItem",),
            resources=(T + "supervisor_journal",),
            why="The journal is append-only and the supervisor is its only writer.",
        ),
        Grant(
            actions=("dynamodb:PutItem",),
            resources=(T + "agent_state",),
            conditions=("dynamodb:LeadingKeys",),
            why="PAUSE# partition only. This is the supervisor's entire "
            "write power over the agent.",
        ),
        Grant(
            actions=("s3:GetObject",),
            resources=(B + "/profile/*", B + "/memories/*", B + "/sessions/*", B + "/prompts/*"),
            why="Evidence: what the agent knows and what it was told.",
        ),
        Grant(
            actions=("events:DisableRule",),
            resources=(
                "arn:aws:events:{region}:{account}:rule/{stage}-governor-rate",
                "arn:aws:events:{region}:{account}:rule/{stage}-governor-weekly",
            ),
            why="Pause, half one. There is deliberately no EnableRule anywhere in the account.",
        ),
        Grant(
            actions=("lambda:PutFunctionConcurrency",),
            resources=(F + "agent-conversation", F + "agent-heartbeat"),
            why="Pause, half two: concurrency to zero.",
        ),
        Grant(
            actions=("lambda:InvokeFunction",),
            resources=(F + "gate-model:supervisor",),
            why="Its own alias, its own budget.",
        ),
        Grant(
            actions=("sns:Publish",),
            resources=("arn:aws:sns:{region}:{account}:{stage}-ops-alerts",),
            why="Ops messages.",
        ),
        Grant(
            actions=("ssm:GetParameter",),
            resources=(PARAM + "telegram-ops-bot",),
            why="Ops bot token (one bot for now, ADR-30).",
        ),
        Grant(
            actions=("logs:FilterLogEvents", "logs:GetLogEvents"),
            resources=("arn:aws:logs:{region}:{account}:log-group:/aws/lambda/{stage}-*",),
            why="Evidence from logs.",
        ),
        Grant(
            actions=("cloudwatch:GetMetricData", "cloudwatch:GetMetricStatistics"),
            resources=("*",),
            why="CloudWatch metrics have no resource ARNs; this is the one allowed wildcard.",
        ),
    ),
)

GATE_MODEL = RoleAllowlist(
    name="gate-model",
    grants=(
        Grant(
            actions=("ssm:GetParameter",),
            resources=(PARAM + "together-api-key",),
            why="The only holder of the provider key, with gate-canary (ADR-28).",
        ),
        Grant(
            actions=("dynamodb:UpdateItem", "dynamodb:GetItem"),
            resources=(T + "agent_state",),
            conditions=("dynamodb:LeadingKeys",),
            why="Conditional token counters under STATE#counters#<date>.",
        ),
        Grant(
            actions=("dynamodb:PutItem", "dynamodb:UpdateItem"),
            resources=(T + "agent_sessions",),
            why="Session index upsert, written before the response is returned.",
        ),
        Grant(
            actions=("s3:PutObject",),
            resources=(B + "/sessions/*",),
            why="Session records. The agent cannot write or delete these.",
        ),
        Grant(
            actions=("events:PutEvents",),
            resources=(BUS,),
            conditions=("events:source",),
            why="model.budget_exhausted, model.upstream_error.",
        ),
    ),
)

# Roles whose full grant sets arrive with their handlers in later VPs. They are
# declared now so VP0 creates them with their final names and trust policies.
PLACEHOLDER_ROLES: Final[tuple[str, ...]] = (
    "gate-telegram",
    "gate-dispatcher",
    "gate-governor",
    "gate-notify",
    "gate-proposal",
    "gate-canary",
    "ops-alarms",
)

ALL: Final[tuple[RoleAllowlist, ...]] = (
    AGENT_CONVERSATION,
    AGENT_HEARTBEAT,
    SANDBOX_EXECUTOR,
    SUPERVISOR,
    GATE_MODEL,
)

BY_NAME: Final[dict[str, RoleAllowlist]] = {r.name: r for r in ALL}


def render(value: str, *, stage: str, account: str, region: str) -> str:
    """Substitute the placeholders used in resource patterns."""
    return value.format(stage=stage, account=account, region=region)


def expected_actions(role: str) -> set[str]:
    """Every action the named role is allowed to hold."""
    allow = BY_NAME.get(role)
    if allow is None:
        return set()
    return {a for g in allow.grants for a in g.actions}
