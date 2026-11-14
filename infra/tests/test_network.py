"""Network shape and the zero-cost invariant (docs/12 §3, ADR-20).

The design's whole cost story rests on there being nothing billed by the hour in
the account. That is easy to break by accident — adding a VPC endpoint to "fix"
a connectivity problem is a one-line change that quietly costs more than every
other resource combined — so it is asserted rather than remembered.
"""

from __future__ import annotations

from typing import Any

import pytest

from infra.tests.conftest import ours, resources

#: Anything here is billed while it merely exists.
ALWAYS_ON_TYPES = (
    "AWS::EC2::NatGateway",
    "AWS::EC2::VPCEndpoint",
    "AWS::ApiGateway::RestApi",
    "AWS::ApiGatewayV2::Api",
    "AWS::SecretsManager::Secret",
    "AWS::RDS::DBInstance",
    "AWS::ElastiCache::CacheCluster",
    "AWS::ECS::Service",
    "AWS::EC2::Instance",
    "AWS::ElasticLoadBalancingV2::LoadBalancer",
)


@pytest.mark.parametrize("stage", ["dev", "prod"])
@pytest.mark.parametrize("resource_type", ALWAYS_ON_TYPES)
def test_no_always_on_resource_exists(
    stage: str, resource_type: str, request: pytest.FixtureRequest
) -> None:
    built = request.getfixturevalue(stage)
    found = resources(built, resource_type)
    assert found == [], f"{resource_type} would be billed hourly: {found}"


def test_sandbox_vpc_has_no_internet_gateway(dev: dict[str, Any]) -> None:
    assert resources(dev, "AWS::EC2::InternetGateway") == []
    assert resources(dev, "AWS::EC2::VPCGatewayAttachment") == []


def test_sandbox_subnets_are_isolated(dev: dict[str, Any]) -> None:
    """No route table in the sandbox VPC may route anywhere off-VPC."""
    for _stack, logical_id, route in resources(dev, "AWS::EC2::Route"):
        props = route["Properties"]
        assert "GatewayId" not in props, f"{logical_id} routes to a gateway"
        assert "NatGatewayId" not in props, f"{logical_id} routes to a NAT"


def test_only_the_executor_runs_in_a_vpc(dev: dict[str, Any]) -> None:
    """Agent Lambdas run outside any VPC (ADR-20); the executor is the exception."""
    in_vpc = [
        body["Properties"].get("FunctionName")
        for _stack, _lid, body in ours(resources(dev, "AWS::Lambda::Function"))
        if body["Properties"].get("VpcConfig")
    ]
    assert in_vpc == ["dev-sandbox-executor"], in_vpc


def test_sandbox_security_group_has_no_egress(dev: dict[str, Any]) -> None:
    """`allow_all_outbound=False` leaves CDK's dummy deny-all rule and nothing else."""
    groups = resources(dev, "AWS::EC2::SecurityGroup")
    assert groups, "the sandbox security group is missing"
    for _stack, logical_id, sg in groups:
        for rule in sg["Properties"].get("SecurityGroupEgress", []):
            assert rule.get("CidrIp") == "255.255.255.255/32", (
                f"{logical_id} has a real egress rule: {rule}"
            )


def test_no_lambda_has_a_function_url(dev: dict[str, Any]) -> None:
    """gate-telegram gains one in VP5; until then there is no public surface."""
    assert resources(dev, "AWS::Lambda::Url") == []
