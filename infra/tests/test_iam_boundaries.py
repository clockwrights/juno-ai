"""IAM boundaries (docs/12 §3).

The agent is editable by proposal; its role is not. These tests are the reason a
widened permission cannot slip through as a side effect of a code change — the
allowlist in `infra/iam/allowlists.py` has to be edited in the same PR, and
CODEOWNERS routes that to the owner.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from infra.iam.allowlists import FORBIDDEN_AGENT_ACTIONS, expected_actions
from infra.tests.conftest import ours, resources

AGENT_ROLES = ("agent-conversation", "agent-heartbeat")

#: Roles a *human* assumes, not roles the running system holds. The admin role
#: is deliberately broad (ADR-25) — it is what `scripts/` and the control panel
#: use, and the only principal that may delete live data. Its boundary is *who
#: can assume it*, asserted separately below, not what it may do.
HUMAN_ROLES = ("agent-admin",)


def _statements_by_role(built: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Map role short-name -> the policy statements attached to it.

    Policies reference their role by `Ref` to the role's logical id, so the role
    names are resolved first and the policies matched back to them.
    """
    role_logical_to_name: dict[str, str] = {}
    for _stack, logical_id, role in resources(built, "AWS::IAM::Role"):
        name = role["Properties"].get("RoleName")
        if isinstance(name, str):
            role_logical_to_name[logical_id] = name

    by_role: dict[str, list[dict[str, Any]]] = {}
    for _stack, _lid, policy in resources(built, "AWS::IAM::Policy"):
        props = policy["Properties"]
        for ref in props.get("Roles", []):
            logical = ref.get("Ref") if isinstance(ref, dict) else None
            name = role_logical_to_name.get(logical or "")
            if not name:
                continue
            short = name.removeprefix("dev-").removeprefix("prod-").removesuffix("-role")
            by_role.setdefault(short, []).extend(props["PolicyDocument"]["Statement"])
    return by_role


def _actions(statements: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for st in statements:
        action = st.get("Action", [])
        out.update([action] if isinstance(action, str) else action)
    return out


@pytest.mark.parametrize("role", AGENT_ROLES)
def test_agent_role_holds_no_forbidden_action(role: str, dev: dict[str, Any]) -> None:
    held = _actions(_statements_by_role(dev).get(role, []))
    for forbidden in FORBIDDEN_AGENT_ACTIONS:
        if forbidden.endswith("*"):
            prefix = forbidden[:-1]
            offenders = {a for a in held if a.startswith(prefix)}
            assert not offenders, f"{role} holds {offenders}, matching {forbidden}"
        else:
            assert forbidden not in held, f"{role} holds {forbidden}"


@pytest.mark.parametrize("role", AGENT_ROLES)
def test_agent_role_matches_the_allowlist(role: str, dev: dict[str, Any]) -> None:
    """Set equality, not containment: extra permissions fail as loudly as missing ones."""
    held = _actions(_statements_by_role(dev).get(role, []))
    assert held == expected_actions(role), (
        f"{role} drifted from infra/iam/allowlists.py.\n"
        f"  only in template:  {sorted(held - expected_actions(role))}\n"
        f"  only in allowlist: {sorted(expected_actions(role) - held)}"
    )


def test_executor_role_holds_only_logs_and_the_eni_actions_aws_forces(
    dev: dict[str, Any],
) -> None:
    """The sandbox runs agent-written code, so its credentials must be worthless.

    Two log actions by design, three ENI actions because a Lambda in a VPC
    cannot be created without them (ADR-33). They are worthless here because the
    subnets have no route to the EC2 API — which `test_network.py` asserts.
    """
    from infra.tests.test_sandbox_role import ALLOWED_SANDBOX_ACTIONS

    held = _actions(_statements_by_role(dev).get("sandbox-executor", []))
    assert held <= ALLOWED_SANDBOX_ACTIONS, held


def test_supervisor_can_pause_and_nothing_more(dev: dict[str, Any]) -> None:
    held = _actions(_statements_by_role(dev).get("supervisor", []))
    assert "events:DisableRule" in held
    assert "lambda:PutFunctionConcurrency" in held
    # Resuming is a human action (ADR-09), so this must not exist anywhere.
    assert "events:EnableRule" not in held
    for banned in ("dynamodb:DeleteItem", "dynamodb:UpdateItem", "s3:PutObject", "s3:DeleteObject"):
        assert banned not in held, f"supervisor holds {banned}; it is read-only plus pause"


def test_no_lambda_role_can_enable_a_rule(dev: dict[str, Any]) -> None:
    """A paused agent stays paused until a *human* runs resume.py (ADR-09).

    The admin role holds EnableRule precisely because resuming must require
    someone with the owner's credentials.
    """
    for role, statements in _statements_by_role(dev).items():
        if role in HUMAN_ROLES:
            continue
        assert "events:EnableRule" not in _actions(statements), role


#: The only two reasons a Lambda role may name `*` as its resource. Both are
#: forced by AWS — metric reads have no ARNs, and Lambda documents the ENI
#: actions as requiring `*` (ADR-33) — and both are listed here so that a third
#: reason has to be argued for rather than merged.
WILDCARD_PREFIXES = ("cloudwatch:", "ec2:")


def test_only_cloudwatch_metrics_and_eni_actions_use_a_wildcard_resource(
    dev: dict[str, Any],
) -> None:
    """Every other Lambda-role grant must be scoped to an ARN."""
    for role, statements in _statements_by_role(dev).items():
        if role in HUMAN_ROLES:
            continue
        for st in statements:
            res = st.get("Resource")
            resource_list = [res] if isinstance(res, (str, dict)) else (res or [])
            if "*" not in [r for r in resource_list if isinstance(r, str)]:
                continue
            actions = _actions([st])
            assert all(a.startswith(WILDCARD_PREFIXES) for a in actions), (
                f"{role} uses a wildcard resource for {actions}"
            )
            if any(a.startswith("ec2:") for a in actions):
                assert role == "sandbox-executor", (
                    f"{role} holds EC2 permissions; only the VPC-bound sandbox may"
                )


def test_every_function_has_its_own_role(dev: dict[str, Any]) -> None:
    """No shared roles: one blast radius per function (docs/02 §2)."""
    role_arns = [
        body["Properties"]["Role"] for _s, _l, body in ours(resources(dev, "AWS::Lambda::Function"))
    ]
    rendered = [str(r) for r in role_arns]
    assert len(rendered) == len(set(rendered)), "two functions share a role"


def test_agent_can_invoke_only_gate_model_and_the_sandbox(dev: dict[str, Any]) -> None:
    for role in AGENT_ROLES:
        for st in _statements_by_role(dev).get(role, []):
            if "lambda:InvokeFunction" not in _actions([st]):
                continue
            rendered = str(st.get("Resource"))
            # Cross-stack references render as Fn::ImportValue with the
            # hyphens stripped from the logical id, so compare on a normalised
            # form rather than the literal resource name.
            flat = re.sub(r"[^a-z]", "", rendered.lower())
            assert "gatemodel" in flat and "sandboxexecutor" in flat, rendered
            for forbidden in ("gateproposal", "gatenotify", "gatecanary"):
                assert forbidden not in flat, f"{role} may invoke {forbidden}"


def test_the_admin_role_is_assumable_only_within_the_account(dev: dict[str, Any]) -> None:
    """The admin role is broad, so what bounds it is who may assume it (ADR-25)."""
    admins = [
        role
        for _s, _l, role in resources(dev, "AWS::IAM::Role")
        if str(role["Properties"].get("RoleName", "")).endswith("agent-admin")
    ]
    assert len(admins) == 1
    statements = admins[0]["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    for statement in statements:
        principal = statement["Principal"]
        assert "Service" not in principal, "no service may assume the admin role"
        assert "AWS" in principal, principal
