"""Layer 3 gates.

Every side effect the agent can cause passes through one of these (ADR-03).
VP0 creates them all with their final names, roles, concurrency and — for
`gate-model` — the three aliases whose resource policies are the actual
enforcement of which role may use which model budget (docs/06 §5.1).

Handler code is the placeholder until each gate's VP; the roles are final now,
so no later VP widens a boundary as a side effect.
"""

from __future__ import annotations

from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from constructs import Construct

from infra.stacks.common import StageStack, lambda_role, make_function
from infra.stacks.foundation import FoundationStack
from infra.stacks.storage import StorageStack

GATE_NAMES = (
    "gate-telegram",
    "gate-dispatcher",
    "gate-governor",
    "gate-notify",
    "gate-model",
    "gate-proposal",
    "gate-canary",
)


class GatesStack(StageStack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        foundation: FoundationStack,
        storage: StorageStack,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.roles: dict[str, iam.Role] = {}
        self.functions: dict[str, lambda_.Function] = {}

        for gate in GATE_NAMES:
            role = lambda_role(self, stack=self, short_name=gate)
            self.roles[gate] = role
            self.functions[gate] = make_function(
                self,
                stack=self,
                short_name=gate,
                role=role,
                timeout_seconds=600 if gate == "gate-model" else 120,
                memory_mb=512,
                environment={"EVENT_BUS_NAME": foundation.bus.event_bus_name},
            )

        self._grant_gate_model(foundation, storage)

        # ------------------------------------------------- gate-model aliases
        # Three aliases, three resource policies. The alias is the boundary:
        # the handler reads it from its own ARN and refuses roles that do not
        # belong to it, but IAM refuses them first (docs/06 §5.1).
        model_fn = self.functions["gate-model"]
        version = model_fn.current_version
        self.model_aliases: dict[str, lambda_.Alias] = {}
        for alias_name in ("agent", "supervisor", "canary", "embed"):
            self.model_aliases[alias_name] = lambda_.Alias(
                self,
                f"gate-model-{alias_name}",
                alias_name=alias_name,
                version=version,
            )

        cdk.CfnOutput(self, "gate-model-arn", value=model_fn.function_arn)

    def _grant_gate_model(self, foundation: FoundationStack, storage: StorageStack) -> None:
        """The one gate whose full policy VP2 does not get to widen."""
        role = self.roles["gate-model"]

        foundation.parameters["together-api-key"].grant_read(role)

        # Conditional token counters live under STATE#counters#<date>; the
        # LeadingKeys condition keeps this role out of every other partition.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["dynamodb:UpdateItem", "dynamodb:GetItem"],
                resources=[storage.state.table_arn],
                conditions={"ForAllValues:StringEquals": {"dynamodb:LeadingKeys": ["STATE"]}},
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["dynamodb:PutItem", "dynamodb:UpdateItem"],
                resources=[storage.sessions.table_arn],
            )
        )
        # Session records: write only. Nothing in the system but the admin role
        # can delete them, which is what makes them trustworthy evidence.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject"],
                resources=[f"{storage.bucket.bucket_arn}/sessions/*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["events:PutEvents"],
                resources=[foundation.bus.event_bus_arn],
                conditions={"StringEquals": {"events:source": "agent.gate"}},
            )
        )
