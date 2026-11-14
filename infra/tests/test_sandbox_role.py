"""The sandbox's isolation, asserted on the template (docs/11 VP3)."""

from __future__ import annotations

from typing import Any

from infra.tests.conftest import ours, resources

#: The two log actions the design gives it, plus the three AWS forces on any
#: Lambda in a VPC (ADR-33). Written out so that a fourth cannot appear quietly.
ALLOWED_SANDBOX_ACTIONS = {
    "logs:CreateLogStream",
    "logs:PutLogEvents",
    "ec2:CreateNetworkInterface",
    "ec2:DescribeNetworkInterfaces",
    "ec2:DeleteNetworkInterface",
}


def test_sandbox_role_holds_only_the_actions_it_cannot_avoid(dev: dict[str, Any]) -> None:
    """It runs agent-written code, so its credentials must be worthless.

    The three EC2 actions are worthless *here* specifically because the subnets
    have no route to the EC2 API; `test_network.py` is what keeps that true, so
    these two tests have to be read together.
    """
    role_names = {
        lid: r["Properties"]["RoleName"]
        for _s, lid, r in resources(dev, "AWS::IAM::Role")
        if isinstance(r["Properties"].get("RoleName"), str)
    }
    actions: set[str] = set()
    for _stack, _lid, policy in resources(dev, "AWS::IAM::Policy"):
        props = policy["Properties"]
        names = {role_names.get(ref.get("Ref", ""), "") for ref in props.get("Roles", [])}
        if not any("sandbox-executor" in n for n in names):
            continue
        for statement in props["PolicyDocument"]["Statement"]:
            action = statement.get("Action", [])
            actions.update([action] if isinstance(action, str) else action)
    assert actions <= ALLOWED_SANDBOX_ACTIONS, actions
    # Never a wildcard: `ec2:*` on `*` would be a different decision entirely.
    assert not any(a.endswith("*") for a in actions), actions


def test_the_executor_is_the_only_function_with_a_security_group(dev: dict[str, Any]) -> None:
    with_sg = [
        body["Properties"].get("FunctionName")
        for _s, _l, body in ours(resources(dev, "AWS::Lambda::Function"))
        if body["Properties"].get("VpcConfig", {}).get("SecurityGroupIds")
    ]
    assert with_sg == ["dev-sandbox-executor"]


def test_the_executor_timeout_leaves_room_for_the_contract_maximum(dev: dict[str, Any]) -> None:
    """The contract allows timeout_s up to 60, so the Lambda must outlive it."""
    timeouts = {
        body["Properties"]["FunctionName"]: body["Properties"]["Timeout"]
        for _s, _l, body in ours(resources(dev, "AWS::Lambda::Function"))
    }
    assert timeouts["dev-sandbox-executor"] >= 60
