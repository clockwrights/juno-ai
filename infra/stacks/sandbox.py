"""The sandbox executor: agent-written code, no IAM, no network.

The isolation is structural rather than policy-based. The function sits in
private-isolated subnets of a VPC that has no NAT gateway, no internet gateway
and no VPC endpoints, so there is no route to anywhere — not to the internet and
not to AWS APIs. Its role holds exactly two log actions, so even if code found a
route it would have no credentials worth using.

`infra/tests/test_network.py` asserts all of that on the synthesized template.
"""

from __future__ import annotations

from typing import Any

from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from constructs import Construct

from infra.stacks.common import StageStack, lambda_role, make_function
from infra.stacks.foundation import FoundationStack


class SandboxStack(StageStack):
    def __init__(
        self, scope: Construct, construct_id: str, *, foundation: FoundationStack, **kwargs: Any
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.role = lambda_role(self, stack=self, short_name="sandbox-executor")

        # Forced by AWS, and discovered only on deploy: a Lambda in a VPC fails
        # to *create* unless its execution role can manage its own elastic
        # network interface, and Lambda requires these three actions on `*`
        # (scoping them is documented not to work). It does not weaken the
        # isolation, because the isolation was never the credentials: the
        # subnets have no route to the EC2 API, or to anything else, so code
        # running here cannot reach the endpoint these permissions are for
        # (ADR-33).
        self.role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "ec2:CreateNetworkInterface",
                    "ec2:DescribeNetworkInterfaces",
                    "ec2:DeleteNetworkInterface",
                ],
                resources=["*"],
            )
        )

        self.function = make_function(
            self,
            stack=self,
            short_name="sandbox-executor",
            role=self.role,
            timeout_seconds=60,
            memory_mb=512,
            vpc_args={
                "vpc": foundation.sandbox_vpc,
                "vpc_subnets": ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
                "security_groups": [foundation.sandbox_sg],
            },
        )
