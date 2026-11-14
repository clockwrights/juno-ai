"""Storage: tables, bucket, vector index, queues.

Defined in VP0 so every role's ARNs are final from the start; the store classes
and scripts that use them arrive in VP1 (docs/11).

Removal policy is the one place stages differ in a way that matters:
non-prod is DESTROY so `make stage-down` really removes a throwaway stage, prod
is RETAIN so no deploy can delete the owner's memory. `scripts/destroy.py`
sweeps the retained resources deliberately and only when asked (ADR-27).
"""

from __future__ import annotations

from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from infra.stacks.common import StageStack
from infra.stacks.foundation import FoundationStack

BILLING = ddb.Billing.on_demand()


class StorageStack(StageStack):
    def __init__(
        self, scope: Construct, construct_id: str, *, foundation: FoundationStack, **kwargs: Any
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.removal = cdk.RemovalPolicy.RETAIN if self.is_prod else cdk.RemovalPolicy.DESTROY

        # ---------------------------------------------------------- tables
        self.memory = self._table(
            "agent_memory",
            partition_key="PK",
            sort_key="SK",
            point_in_time=True,
        )
        self.memory.add_global_secondary_index(
            index_name="type-last_accessed-index",
            partition_key=ddb.Attribute(name="type", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="last_accessed", type=ddb.AttributeType.STRING),
            projection_type=ddb.ProjectionType.INCLUDE,
            non_key_attributes=[
                "importance",
                "access_count",
                "tombstone",
                "namespace",
                "tags",
            ],
        )
        self.memory.add_global_secondary_index(
            index_name="namespace-created_at-index",
            partition_key=ddb.Attribute(name="namespace", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="created_at", type=ddb.AttributeType.STRING),
            projection_type=ddb.ProjectionType.KEYS_ONLY,
        )

        self.memory_tags = self._table("agent_memory_tags", "PK", "SK", point_in_time=True)
        self.diary = self._table("agent_diary", "PK", "SK", point_in_time=True)
        self.state = self._table("agent_state", "PK", "SK", point_in_time=True, ttl="ttl")

        self.conversations = self._table("agent_conversations", "PK", "SK", point_in_time=True)
        self.conversations.add_global_secondary_index(
            index_name="status-last_activity-index",
            partition_key=ddb.Attribute(name="status", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="last_activity_at", type=ddb.AttributeType.STRING),
            projection_type=ddb.ProjectionType.ALL,
        )

        # The stream is what triggers gate-proposal (docs/09 §1).
        self.proposals = self._table(
            "agent_proposals", "PK", "SK", point_in_time=True, stream=ddb.StreamViewType.NEW_IMAGE
        )

        self.sessions = self._table("agent_sessions", "PK", "SK", point_in_time=True)
        self.sessions.add_global_secondary_index(
            index_name="conversation-index",
            partition_key=ddb.Attribute(name="conversation_id", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="started_at", type=ddb.AttributeType.STRING),
            projection_type=ddb.ProjectionType.ALL,
        )

        self.journal = self._table("supervisor_journal", "PK", "SK", point_in_time=True)
        self.journal.add_global_secondary_index(
            index_name="event_type-sk-index",
            partition_key=ddb.Attribute(name="event_type", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="SK", type=ddb.AttributeType.STRING),
            projection_type=ddb.ProjectionType.ALL,
        )

        self.tables = {
            "agent_memory": self.memory,
            "agent_memory_tags": self.memory_tags,
            "agent_diary": self.diary,
            "agent_state": self.state,
            "agent_conversations": self.conversations,
            "agent_proposals": self.proposals,
            "agent_sessions": self.sessions,
            "supervisor_journal": self.journal,
        }

        # ---------------------------------------------------------- bucket
        self.bucket = s3.Bucket(
            self,
            "bucket",
            bucket_name=f"{self.stage}-agent-{self.account}",
            versioned=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=self.removal,
            auto_delete_objects=not self.is_prod,
            lifecycle_rules=[
                # Noncurrent versions expire after a year, except under profile/
                # which is the agent's self-image and kept forever (docs/09 §2).
                s3.LifecycleRule(
                    id="noncurrent-except-profile",
                    noncurrent_version_expiration=cdk.Duration.days(365),
                    prefix="memories/",
                ),
                s3.LifecycleRule(
                    id="sessions-180d",
                    prefix="sessions/",
                    expiration=cdk.Duration.days(180),
                ),
                s3.LifecycleRule(
                    id="exports-1d",
                    prefix="exports/",
                    expiration=cdk.Duration.days(1),
                ),
            ],
        )

        # ----------------------------------------------------------- queues
        self.inbound_dlq = sqs.Queue(
            self,
            "inbound-dlq",
            queue_name=self.name("inbound-messages-dlq.fifo"),
            fifo=True,
            retention_period=cdk.Duration.days(14),
            enforce_ssl=True,
        )
        self.inbound = sqs.Queue(
            self,
            "inbound",
            queue_name=self.name("inbound-messages.fifo"),
            fifo=True,
            content_based_deduplication=False,
            visibility_timeout=cdk.Duration.seconds(180),
            enforce_ssl=True,
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=self.inbound_dlq),
        )

        self.notifications_dlq = sqs.Queue(
            self,
            "notifications-dlq",
            queue_name=self.name("outbound-notifications-dlq"),
            retention_period=cdk.Duration.days(14),
            enforce_ssl=True,
        )
        self.notifications = sqs.Queue(
            self,
            "notifications",
            queue_name=self.name("outbound-notifications"),
            visibility_timeout=cdk.Duration.seconds(120),
            enforce_ssl=True,
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=3, queue=self.notifications_dlq
            ),
        )

        self.supervisor_dlq = sqs.Queue(
            self,
            "supervisor-dlq",
            queue_name=self.name("supervisor-events-dlq"),
            retention_period=cdk.Duration.days(14),
            enforce_ssl=True,
        )
        self.supervisor_events = sqs.Queue(
            self,
            "supervisor-events",
            queue_name=self.name("supervisor-events"),
            visibility_timeout=cdk.Duration.seconds(180),
            enforce_ssl=True,
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=self.supervisor_dlq),
        )

        self.dlqs = [self.inbound_dlq, self.notifications_dlq, self.supervisor_dlq]

        # The supervisor's EventBridge rule lives in the supervisor stack but
        # targets this queue. Letting CDK wire that up would make storage depend
        # on supervisor while supervisor already depends on storage, so the
        # policy is written here against the rule's *known* ARN instead. The
        # rule name is fixed in supervisor.py; the SourceArn condition keeps the
        # grant as narrow as the generated one would have been.
        self.supervisor_events.add_to_resource_policy(
            iam.PolicyStatement(
                actions=["sqs:SendMessage"],
                principals=[iam.ServicePrincipal("events.amazonaws.com")],
                resources=[self.supervisor_events.queue_arn],
                conditions={
                    "ArnEquals": {
                        "aws:SourceArn": (
                            f"arn:aws:events:{self.region}:{self.account}:rule/"
                            f"{self.name('supervisor-events')}"
                        )
                    }
                },
            )
        )

        # ------------------------------------------------------- S3 Vectors
        # No L2 construct exists yet; the L1 resource is created directly.
        # ADR-15's fallback applies if the region turns out not to offer it,
        # which VP1's manual check confirms.
        self.vector_bucket_name = f"{self.stage}-agent-vectors"
        self.vector_index_name = "memory"
        self.vectors = cdk.CfnResource(
            self,
            "vector-bucket",
            type="AWS::S3Vectors::VectorBucket",
            properties={"VectorBucketName": self.vector_bucket_name},
        )
        self.vectors.apply_removal_policy(self.removal)
        self.vector_index = cdk.CfnResource(
            self,
            "vector-index",
            type="AWS::S3Vectors::Index",
            properties={
                "VectorBucketName": self.vector_bucket_name,
                "IndexName": self.vector_index_name,
                "DataType": "float32",
                "Dimension": 1024,  # intfloat/multilingual-e5-large-instruct (ADR-29)
                "DistanceMetric": "cosine",
                "MetadataConfiguration": {
                    "NonFilterableMetadataKeys": ["mem_id"],
                },
            },
        )
        self.vector_index.add_dependency(self.vectors)
        self.vector_index.apply_removal_policy(self.removal)

        cdk.CfnOutput(self, "bucket-name", value=self.bucket.bucket_name)
        cdk.CfnOutput(self, "inbound-queue-url", value=self.inbound.queue_url)

    def _table(
        self,
        name: str,
        partition_key: str,
        sort_key: str,
        *,
        point_in_time: bool = True,
        stream: ddb.StreamViewType | None = None,
        ttl: str | None = None,
    ) -> ddb.TableV2:
        return ddb.TableV2(
            self,
            name,
            table_name=self.name(name),
            partition_key=ddb.Attribute(name=partition_key, type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name=sort_key, type=ddb.AttributeType.STRING),
            billing=BILLING,
            point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=point_in_time
            ),
            encryption=ddb.TableEncryptionV2.aws_managed_key(),
            dynamo_stream=stream,
            time_to_live_attribute=ttl,
            removal_policy=self.removal,
        )
