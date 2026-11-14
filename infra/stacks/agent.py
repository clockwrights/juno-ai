"""The two agent Lambdas.

These are the only editable-by-proposal components that run (docs/02 §1), so
their roles are the most carefully bounded in the system and are written here in
final form at VP0. Later VPs replace the handler code and never touch the role.

The grants deliberately mirror `infra/iam/allowlists.py`; the assertion test
compares the two and fails if they drift.
"""

from __future__ import annotations

from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from constructs import Construct

from infra.stacks.common import StageStack, lambda_role, make_function
from infra.stacks.foundation import FoundationStack
from infra.stacks.gates import GatesStack
from infra.stacks.sandbox import SandboxStack
from infra.stacks.storage import StorageStack

AGENT_TABLE_ACTIONS = [
    "dynamodb:GetItem",
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:Query",
    "dynamodb:BatchGetItem",
    "dynamodb:BatchWriteItem",
    "dynamodb:TransactWriteItems",
]


class AgentStack(StageStack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        foundation: FoundationStack,
        storage: StorageStack,
        gates: GatesStack,
        sandbox: SandboxStack,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.roles: dict[str, iam.Role] = {}
        self.functions: dict[str, lambda_.Function] = {}
        self.aliases: dict[str, lambda_.Alias] = {}

        for short_name, timeout, memory in (
            ("agent-conversation", 120, 512),
            ("agent-heartbeat", 840, 512),  # 14 min: 12 min of work plus margin
        ):
            role = lambda_role(self, stack=self, short_name=short_name)
            self._grant_common(role, foundation, storage, gates, sandbox)
            self.roles[short_name] = role

            fn = make_function(
                self,
                stack=self,
                short_name=short_name,
                role=role,
                timeout_seconds=timeout,
                memory_mb=memory,
                environment={
                    "EVENT_BUS_NAME": foundation.bus.event_bus_name,
                    "BUCKET_NAME": storage.bucket.bucket_name,
                    "GATE_MODEL_ARN": gates.functions["gate-model"].function_arn,
                    "SANDBOX_ARN": sandbox.function.function_arn,
                },
            )
            self.functions[short_name] = fn

            # Alias `live` exists from VP0 so CodeDeploy has something to shift
            # in VP8 without the function being recreated (docs/11 VP5).
            self.aliases[short_name] = lambda_.Alias(
                self, f"{short_name}-live", alias_name="live", version=fn.current_version
            )

        # Conversation turns may write canonical artifacts; idle cycles may not.
        # This asymmetry is the staging-rule invariant made structural.
        self.roles["agent-conversation"].add_to_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject"],
                resources=[f"{storage.bucket.bucket_arn}/artifacts/*"],
            )
        )

        cdk.CfnOutput(
            self, "conversation-alias", value=self.aliases["agent-conversation"].function_arn
        )
        cdk.CfnOutput(self, "heartbeat-alias", value=self.aliases["agent-heartbeat"].function_arn)

    def _grant_common(
        self,
        role: iam.Role,
        foundation: FoundationStack,
        storage: StorageStack,
        gates: GatesStack,
        sandbox: SandboxStack,
    ) -> None:
        # Tables. No DeleteItem and no Scan anywhere: forgetting is a tombstone,
        # and a full scan is how a memory system becomes a data leak.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=AGENT_TABLE_ACTIONS,
                resources=[
                    storage.memory.table_arn,
                    f"{storage.memory.table_arn}/index/*",
                    storage.memory_tags.table_arn,
                    storage.diary.table_arn,
                    storage.conversations.table_arn,
                    f"{storage.conversations.table_arn}/index/*",
                    storage.proposals.table_arn,
                ],
            )
        )
        # agent_state: STATE and NOTE partitions only. PAUSE lives in its own
        # partition precisely so this condition can exclude it (docs/09 §1).
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:Query",
                    "dynamodb:UpdateItem",
                    "dynamodb:PutItem",
                ],
                resources=[storage.state.table_arn],
                conditions={
                    "ForAllValues:StringEquals": {"dynamodb:LeadingKeys": ["STATE", "NOTE", "FLAG"]}
                },
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:PutObject"],
                resources=[
                    f"{storage.bucket.bucket_arn}/profile/*",
                    f"{storage.bucket.bucket_arn}/memories/*",
                    f"{storage.bucket.bucket_arn}/drafts/*",
                    f"{storage.bucket.bucket_arn}/conversations/*",
                    f"{storage.bucket.bucket_arn}/proposals/*",
                    f"{storage.bucket.bucket_arn}/canary/*",
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"],
                resources=[f"{storage.bucket.bucket_arn}/artifacts/*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:ListBucket"],
                resources=[storage.bucket.bucket_arn],
                conditions={"StringLike": {"s3:prefix": ["drafts/*", "artifacts/*"]}},
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3vectors:PutVectors", "s3vectors:QueryVectors", "s3vectors:GetVectors"],
                resources=[
                    f"arn:aws:s3vectors:{self.region}:{self.account}:bucket/"
                    f"{storage.vector_bucket_name}/index/{storage.vector_index_name}"
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["sqs:SendMessage"], resources=[storage.notifications.queue_arn]
            )
        )
        # source is pinned: the agent cannot emit an event that looks like it
        # came from a gate or the supervisor.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["events:PutEvents"],
                resources=[foundation.bus.event_bus_arn],
                conditions={"StringEquals": {"events:source": "agent.runtime"}},
            )
        )
        # The only two functions the agent may invoke. There is no web path at
        # all under ADR-28.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[
                    f"{gates.functions['gate-model'].function_arn}:agent",
                    # Embeddings go through the gateway because it holds the
                    # provider key and the agent role may not (ADR-29).
                    f"{gates.functions['gate-model'].function_arn}:embed",
                    sandbox.function.function_arn,
                ],
            )
        )
