"""The destroy plan is derived from the template, not from memory (ADR-27).

The point of these tests is that a resource added in a later VP cannot be
forgotten by the sweep: if it is retained and unknown, it shows up under
`unknown` where a human will see it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops.admin import Stage, StageRefusalError, guard_prod
from ops.lifecycle import plan_from_templates

STAGE = Stage(name="prod", account="111122223333")


def write_template(tmp_path: Path, stack: str, resources: dict[str, dict]) -> None:
    (tmp_path / f"{stack}.template.json").write_text(
        json.dumps({"Resources": resources}), encoding="utf-8"
    )


def test_retained_resources_are_planned_for_sweeping(tmp_path: Path) -> None:
    write_template(
        tmp_path,
        "prod-storage",
        {
            "Table": {
                "Type": "AWS::DynamoDB::GlobalTable",
                "DeletionPolicy": "Retain",
                "Properties": {"TableName": "prod-agent_memory"},
            },
            "Bucket": {
                "Type": "AWS::S3::Bucket",
                "DeletionPolicy": "Retain",
                "Properties": {"BucketName": "prod-agent-111122223333"},
            },
        },
    )
    plan = plan_from_templates(STAGE, tmp_path)
    assert {i.name for i in plan.sweep} == {"prod-agent_memory", "prod-agent-111122223333"}
    assert plan.unknown == []


def test_non_retained_resources_are_left_to_cloudformation(tmp_path: Path) -> None:
    write_template(
        tmp_path,
        "prod-storage",
        {
            "Table": {
                "Type": "AWS::DynamoDB::GlobalTable",
                "DeletionPolicy": "Delete",
                "Properties": {"TableName": "dev-agent_memory"},
            }
        },
    )
    assert plan_from_templates(STAGE, tmp_path).sweep == []


def test_an_unknown_retained_type_is_surfaced_not_skipped(tmp_path: Path) -> None:
    """This is the test that keeps the sweep honest as the design grows."""
    write_template(
        tmp_path,
        "prod-storage",
        {
            "Mystery": {
                "Type": "AWS::Some::FutureThing",
                "DeletionPolicy": "Retain",
                "Properties": {"Name": "x"},
            }
        },
    )
    plan = plan_from_templates(STAGE, tmp_path)
    assert plan.sweep == []
    assert [i.kind for i in plan.unknown] == ["AWS::Some::FutureThing"]
    assert "NOT handled by the sweep" in plan.render()


def test_a_tokenised_name_cannot_be_swept_blindly(tmp_path: Path) -> None:
    write_template(
        tmp_path,
        "prod-storage",
        {
            "Table": {
                "Type": "AWS::DynamoDB::GlobalTable",
                "DeletionPolicy": "Retain",
                "Properties": {"TableName": {"Ref": "Something"}},
            }
        },
    )
    plan = plan_from_templates(STAGE, tmp_path)
    assert plan.sweep == []
    assert "token" in plan.unknown[0].detail


def test_stacks_are_destroyed_in_reverse_dependency_order(tmp_path: Path) -> None:
    for stack in ("prod-foundation", "prod-storage", "prod-agent", "prod-supervisor"):
        write_template(tmp_path, stack, {})
    plan = plan_from_templates(STAGE, tmp_path)
    assert plan.stacks.index("prod-supervisor") < plan.stacks.index("prod-agent")
    assert plan.stacks.index("prod-agent") < plan.stacks.index("prod-storage")
    assert plan.stacks[-1] == "prod-foundation", "foundation must go last"


def test_the_same_resource_is_planned_once(tmp_path: Path) -> None:
    for stack in ("prod-a", "prod-b"):
        write_template(
            tmp_path,
            stack,
            {
                "Bucket": {
                    "Type": "AWS::S3::Bucket",
                    "DeletionPolicy": "Retain",
                    "Properties": {"BucketName": "prod-agent-111122223333"},
                }
            },
        )
    assert len(plan_from_templates(STAGE, tmp_path).sweep) == 1


def test_prod_needs_an_explicit_flag() -> None:
    with pytest.raises(StageRefusalError, match="--i-know"):
        guard_prod(STAGE, i_know=False, action="destroy")
    guard_prod(STAGE, i_know=True, action="destroy")


def test_non_prod_needs_no_flag() -> None:
    guard_prod(Stage(name="dev", account="1"), i_know=False, action="destroy")
