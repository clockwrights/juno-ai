"""Destroy planning and the retained-resource sweep (ADR-27).

`cdk destroy` is not enough on `prod`, where every stateful resource carries a
`RETAIN` removal policy so that no ordinary deploy can delete the owner's
memory. Removing a stage therefore has a second half: deliberately deleting what
CloudFormation deliberately left behind.

The plan is derived from the *synthesized template*, not from a hand-written
list, so a resource added in a later VP cannot be forgotten here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ops.admin import AdminSession, Stage

#: Resource types the sweep knows how to delete, mapped to the template property
#: holding the physical name.
SWEEPABLE: dict[str, str] = {
    "AWS::DynamoDB::GlobalTable": "TableName",
    "AWS::DynamoDB::Table": "TableName",
    "AWS::S3::Bucket": "BucketName",
    "AWS::SSM::Parameter": "Name",
    "AWS::Logs::LogGroup": "LogGroupName",
    "AWS::CloudWatch::Alarm": "AlarmName",
    "AWS::SQS::Queue": "QueueName",
    "AWS::S3Vectors::VectorBucket": "VectorBucketName",
    # Swept with its bucket rather than by name — a vector bucket refuses to be
    # deleted while it holds an index — but listed here so the plan does not
    # report it as something the operator must go and delete by hand.
    "AWS::S3Vectors::Index": "IndexName",
}


@dataclass
class PlanItem:
    kind: str
    name: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.kind:<28} {self.name}{('  # ' + self.detail) if self.detail else ''}"


@dataclass
class DestroyPlan:
    stage: str
    stacks: list[str] = field(default_factory=list)
    sweep: list[PlanItem] = field(default_factory=list)
    unknown: list[PlanItem] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"destroy plan for stage {self.stage!r}", "", "stacks, in reverse order:"]
        lines += [f"  {s}" for s in self.stacks] or ["  (none found)"]
        lines += ["", f"retained-resource sweep ({len(self.sweep)} items):"]
        lines += [f"  {item}" for item in self.sweep] or ["  (nothing retained)"]
        if self.unknown:
            lines += ["", "NOT handled by the sweep — delete by hand if they survive:"]
            lines += [f"  {item}" for item in self.unknown]
        return "\n".join(lines)


def plan_from_templates(stage: Stage, cdk_out: Path) -> DestroyPlan:
    """Build a destroy plan by reading every synthesized template for the stage.

    Deriving the plan from the template is what makes the sweep total: a
    resource that exists in the app but not in this function's knowledge shows
    up under `unknown` rather than being silently skipped.
    """
    plan = DestroyPlan(stage=stage.name)
    templates = sorted(cdk_out.glob(f"{stage.name}-*.template.json"))
    stack_names = [p.name.split(".")[0] for p in templates]
    # Reverse dependency order: supervisor and agent first, foundation last.
    order = {name: i for i, name in enumerate(stage.stack_names)}
    plan.stacks = sorted(stack_names, key=lambda s: order.get(s, 99), reverse=True)

    seen: set[tuple[str, str]] = set()
    for path in templates:
        body = json.loads(path.read_text(encoding="utf-8"))
        for logical_id, resource in body.get("Resources", {}).items():
            rtype = resource["Type"]
            retained = resource.get("DeletionPolicy") == "Retain"
            if not retained:
                continue
            props = resource.get("Properties", {})
            if rtype in SWEEPABLE:
                name = props.get(SWEEPABLE[rtype])
                if not isinstance(name, str):
                    plan.unknown.append(
                        PlanItem(rtype, logical_id, "name is a CloudFormation token")
                    )
                    continue
                key = (rtype, name)
                if key not in seen:
                    seen.add(key)
                    plan.sweep.append(PlanItem(rtype, name, path.name.split(".")[0]))
            else:
                plan.unknown.append(PlanItem(rtype, logical_id, "no sweep implementation"))
    plan.sweep.sort(key=lambda i: (i.kind, i.name))
    return plan


def empty_versioned_bucket(session: AdminSession, bucket: str, *, dry_run: bool) -> int:
    """Delete every version and delete-marker so the bucket can be removed.

    A versioned bucket cannot be deleted while any noncurrent version or delete
    marker remains, which is the usual reason a "successful" teardown leaves a
    bill behind.
    """
    s3 = session.client("s3")
    paginator = s3.get_paginator("list_object_versions")
    deleted = 0
    for page in paginator.paginate(Bucket=bucket):
        objects = [
            {"Key": item["Key"], "VersionId": item["VersionId"]}
            for key in ("Versions", "DeleteMarkers")
            for item in page.get(key, [])
        ]
        if not objects:
            continue
        deleted += len(objects)
        if not dry_run:
            for chunk in (objects[i : i + 1000] for i in range(0, len(objects), 1000)):
                s3.delete_objects(Bucket=bucket, Delete={"Objects": chunk, "Quiet": True})
    return deleted


def sweep(session: AdminSession, plan: DestroyPlan, *, dry_run: bool) -> list[str]:
    """Delete the retained resources in `plan`, returning a line per action.

    Every failure is reported rather than raised: a half-swept stage should tell
    the operator exactly what is left, not stop at the first surprise.
    """
    report: list[str] = []
    for item in plan.sweep:
        try:
            report.append(_sweep_one(session, item, dry_run=dry_run))
        except Exception as exc:
            report.append(f"FAILED {item.kind} {item.name}: {exc}")
    return report


def sweep_orphan_log_groups(session: AdminSession, stage: str, *, dry_run: bool) -> list[str]:
    """Delete `/aws/lambda/<stage>-*` log groups the template never mentioned.

    The plan is derived from the synthesized templates, which is what makes it
    total for resources *we* declare. It cannot see the ones AWS creates on our
    behalf: CDK's own custom-resource Lambdas (auto-delete-objects, the OIDC
    provider) write log groups the first time they run, and those outlive the
    stack. They cost almost nothing and they never expire, which is exactly the
    kind of leftover a teardown is supposed to remove.

    The stage prefix is the ownership contract everywhere else in this system
    (`Stage.resource`), and `scripts/verify_destroyed.py` already treats it as
    the definition of "belongs to this stage".
    """
    client = session.client("logs")
    prefix = f"/aws/lambda/{stage}-"
    report: list[str] = []
    paginator = client.get_paginator("describe_log_groups")
    for page in paginator.paginate(logGroupNamePrefix=prefix):
        for group in page.get("logGroups", []):
            name = group["logGroupName"]
            try:
                if not dry_run:
                    client.delete_log_group(logGroupName=name)
                report.append(f"{'would delete' if dry_run else 'deleted'} log group {name}")
            except Exception as exc:
                report.append(f"FAILED log group {name}: {exc}")
    return report


def empty_vector_bucket(session: AdminSession, bucket: str, *, dry_run: bool) -> int:
    """Delete every index in a vector bucket. Returns how many there were."""
    client = session.client("s3vectors")
    names = [
        i["indexName"] for i in client.list_indexes(vectorBucketName=bucket).get("indexes", [])
    ]
    if not dry_run:
        for name in names:
            client.delete_index(vectorBucketName=bucket, indexName=name)
    return len(names)


def _sweep_one(session: AdminSession, item: PlanItem, *, dry_run: bool) -> str:
    prefix = "would delete" if dry_run else "deleted"
    if item.kind in ("AWS::DynamoDB::Table", "AWS::DynamoDB::GlobalTable"):
        if not dry_run:
            session.client("dynamodb").delete_table(TableName=item.name)
        return f"{prefix} table {item.name}"
    if item.kind == "AWS::S3::Bucket":
        count = empty_versioned_bucket(session, item.name, dry_run=dry_run)
        if not dry_run:
            session.client("s3").delete_bucket(Bucket=item.name)
        return f"{prefix} bucket {item.name} ({count} object versions)"
    if item.kind == "AWS::SSM::Parameter":
        if not dry_run:
            session.client("ssm").delete_parameter(Name=item.name)
        return f"{prefix} parameter {item.name}"
    if item.kind == "AWS::Logs::LogGroup":
        if not dry_run:
            session.client("logs").delete_log_group(logGroupName=item.name)
        return f"{prefix} log group {item.name}"
    if item.kind == "AWS::CloudWatch::Alarm":
        if not dry_run:
            session.client("cloudwatch").delete_alarms(AlarmNames=[item.name])
        return f"{prefix} alarm {item.name}"
    if item.kind == "AWS::SQS::Queue":
        if not dry_run:
            queue_url = session.client("sqs").get_queue_url(QueueName=item.name)["QueueUrl"]
            session.client("sqs").delete_queue(QueueUrl=queue_url)
        return f"{prefix} queue {item.name}"
    if item.kind == "AWS::S3Vectors::VectorBucket":
        # Indexes first: a vector bucket refuses to be deleted while it holds
        # any, exactly like a non-empty S3 bucket. The index is a separate
        # template resource, so the plan lists it too; deleting it here is what
        # makes one sweep enough.
        indexes = empty_vector_bucket(session, item.name, dry_run=dry_run)
        if not dry_run:
            session.client("s3vectors").delete_vector_bucket(vectorBucketName=item.name)
        return f"{prefix} vector bucket {item.name} ({indexes} indexes)"
    if item.kind == "AWS::S3Vectors::Index":
        # Deleted by the vector-bucket case, which has to empty the bucket
        # before it can delete it. Reported rather than skipped, but without
        # claiming it is already gone — the bucket may come later in the plan.
        return f"vector index {item.name}: left to its vector bucket"
    return f"SKIPPED {item.kind} {item.name}: no sweep implementation"
