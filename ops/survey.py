"""Find everything in the account belonging to a stage.

Used two ways: to prove a destroyed stage really is gone (`verify_destroyed.py`),
and to show what a stage costs before removing it. Read-only — nothing here
deletes anything.

Every lookup is best-effort: a missing permission or an unavailable service
degrades to "found nothing of that kind" rather than failing the whole survey,
because a partial answer is still useful and an exception here would block a
teardown.
"""

from __future__ import annotations

from typing import Any

from ops.admin import AdminSession, Stage

#: Anything that costs money while it merely exists. `verify_destroyed.py`
#: reports these first because they are the ones that matter after a teardown.
BILLABLE_KINDS = frozenset(
    {"nat_gateway", "vpc_endpoint", "dynamodb_table", "s3_bucket", "vector_bucket"}
)


def survey_stage(stage: Stage, session: AdminSession | None = None) -> dict[str, list[str]]:
    """Return {kind: [names]} for everything carrying the stage prefix."""
    session = session or AdminSession(stage, assume=False)
    prefix = f"{stage.name}-"
    found: dict[str, list[str]] = {}

    def record(kind: str, names: list[str]) -> None:
        if names:
            found[kind] = sorted(names)

    record("cloudformation_stack", _stacks(session, stage))
    record("dynamodb_table", _tables(session, prefix))
    record("s3_bucket", _buckets(session, stage))
    record("vector_bucket", _vector_buckets(session, stage))
    record("lambda_function", _functions(session, prefix))
    record("sqs_queue", _queues(session, prefix))
    record("ssm_parameter", _parameters(session, stage))
    record("log_group", _log_groups(session, prefix))
    record("cloudwatch_alarm", _alarms(session, prefix))
    record("event_rule", _rules(session, prefix))
    record("iam_role", _roles(session, prefix))
    record("nat_gateway", _nat_gateways(session))
    record("vpc_endpoint", _vpc_endpoints(session))
    return found


def _safe(fn: Any, default: list[str] | None = None) -> list[str]:
    try:
        return list(fn())
    except Exception:
        return default or []


def _stacks(session: AdminSession, stage: Stage) -> list[str]:
    def go() -> list[str]:
        cfn = session.client("cloudformation")
        names = []
        for page in cfn.get_paginator("list_stacks").paginate(
            StackStatusFilter=[
                "CREATE_COMPLETE",
                "UPDATE_COMPLETE",
                "ROLLBACK_COMPLETE",
                "UPDATE_ROLLBACK_COMPLETE",
                "CREATE_IN_PROGRESS",
                "DELETE_FAILED",
            ]
        ):
            names += [
                s["StackName"]
                for s in page["StackSummaries"]
                if s["StackName"] in stage.stack_names
            ]
        return names

    return _safe(go)


def _tables(session: AdminSession, prefix: str) -> list[str]:
    def go() -> list[str]:
        ddb = session.client("dynamodb")
        names = []
        for page in ddb.get_paginator("list_tables").paginate():
            names += [t for t in page["TableNames"] if t.startswith(prefix)]
        return names

    return _safe(go)


def _buckets(session: AdminSession, stage: Stage) -> list[str]:
    def go() -> list[str]:
        s3 = session.client("s3")
        return [
            b["Name"]
            for b in s3.list_buckets()["Buckets"]
            if b["Name"] == stage.bucket or b["Name"].startswith(f"{stage.name}-agent-")
        ]

    return _safe(go)


def _vector_buckets(session: AdminSession, stage: Stage) -> list[str]:
    def go() -> list[str]:
        client = session.client("s3vectors")
        buckets = client.list_vector_buckets().get("vectorBuckets", [])
        return [
            b["vectorBucketName"]
            for b in buckets
            if str(b.get("vectorBucketName", "")).startswith(f"{stage.name}-")
        ]

    return _safe(go)


def _functions(session: AdminSession, prefix: str) -> list[str]:
    def go() -> list[str]:
        lam = session.client("lambda")
        names = []
        for page in lam.get_paginator("list_functions").paginate():
            names += [
                f["FunctionName"] for f in page["Functions"] if f["FunctionName"].startswith(prefix)
            ]
        return names

    return _safe(go)


def _queues(session: AdminSession, prefix: str) -> list[str]:
    def go() -> list[str]:
        sqs = session.client("sqs")
        urls = sqs.list_queues(QueueNamePrefix=prefix).get("QueueUrls", [])
        return [u.rsplit("/", 1)[-1] for u in urls]

    return _safe(go)


def _parameters(session: AdminSession, stage: Stage) -> list[str]:
    def go() -> list[str]:
        ssm = session.client("ssm")
        names = []
        paginator = ssm.get_paginator("describe_parameters")
        for page in paginator.paginate(
            ParameterFilters=[
                {"Key": "Name", "Option": "BeginsWith", "Values": [stage.parameter_prefix]}
            ]
        ):
            names += [p["Name"] for p in page["Parameters"]]
        return names

    return _safe(go)


def _log_groups(session: AdminSession, prefix: str) -> list[str]:
    def go() -> list[str]:
        logs = session.client("logs")
        names = []
        paginator = logs.get_paginator("describe_log_groups")
        for page in paginator.paginate(logGroupNamePrefix=f"/aws/lambda/{prefix}"):
            names += [g["logGroupName"] for g in page["logGroups"]]
        return names

    return _safe(go)


def _alarms(session: AdminSession, prefix: str) -> list[str]:
    def go() -> list[str]:
        cw = session.client("cloudwatch")
        names = []
        for page in cw.get_paginator("describe_alarms").paginate(AlarmNamePrefix=prefix):
            names += [a["AlarmName"] for a in page["MetricAlarms"]]
        return names

    return _safe(go)


def _rules(session: AdminSession, prefix: str) -> list[str]:
    def go() -> list[str]:
        events = session.client("events")
        names = []
        for bus in ("default", f"{prefix}agent-events"):
            try:
                rules = events.list_rules(EventBusName=bus, NamePrefix=prefix).get("Rules", [])
            except Exception:
                continue
            names += [r["Name"] for r in rules]
        return names

    return _safe(go)


def _roles(session: AdminSession, prefix: str) -> list[str]:
    def go() -> list[str]:
        iam = session.client("iam")
        names = []
        for page in iam.get_paginator("list_roles").paginate():
            names += [r["RoleName"] for r in page["Roles"] if r["RoleName"].startswith(prefix)]
        return names

    return _safe(go)


def _nat_gateways(session: AdminSession) -> list[str]:
    """Account-wide, not stage-scoped: a NAT anywhere breaks the cost model."""

    def go() -> list[str]:
        ec2 = session.client("ec2")
        gateways = ec2.describe_nat_gateways().get("NatGateways", [])
        return [
            g["NatGatewayId"] for g in gateways if g.get("State") not in ("deleted", "deleting")
        ]

    return _safe(go)


def _vpc_endpoints(session: AdminSession) -> list[str]:
    def go() -> list[str]:
        ec2 = session.client("ec2")
        endpoints = ec2.describe_vpc_endpoints().get("VpcEndpoints", [])
        return [e["VpcEndpointId"] for e in endpoints]

    return _safe(go)


def billable(found: dict[str, list[str]]) -> dict[str, list[str]]:
    """Just the parts of a survey that cost money."""
    return {k: v for k, v in found.items() if k in BILLABLE_KINDS}
