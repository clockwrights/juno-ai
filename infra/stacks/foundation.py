"""Foundation: the pieces every other stack depends on.

This is the only stack VP0 deploys in full. It holds the sandbox VPC (which is
free precisely because it has no NAT and no endpoints), the event bus, the ops
SNS topic, the SSM parameter placeholders, the cost budget and the admin role.

Nothing here is billed by the hour. `infra/tests/test_network.py` asserts that.
"""

from __future__ import annotations

from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_cloudtrail as cloudtrail
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_events as events
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from infra.stacks.common import StageStack

#: docs/09 §5. Six parameters: ADR-30 folded the two Telegram bots into one
#: token but kept both names, so splitting later needs no deploy.
SECURE_PARAMETERS: tuple[str, ...] = (
    "together-api-key",
    "telegram-agent-bot",
    "telegram-ops-bot",
    "github-proposal-token",
    "webhook-secret",
)
PLAIN_PARAMETERS: tuple[str, ...] = ("owner-chat-id", "ops-chat-id")

#: The owner's whole budget (ADR-28). Alarms at 70% and 90%.
MONTHLY_BUDGET_USD = 20


class FoundationStack(StageStack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs: Any) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ------------------------------------------------------- sandbox VPC
        # A VPC with only private-isolated subnets costs nothing: no NAT, no
        # internet gateway, no endpoints. It exists solely to remove the
        # executor's network (ADR-20).
        self.sandbox_vpc = ec2.Vpc(
            self,
            "sandbox-vpc",
            vpc_name=self.name("sandbox-vpc"),
            ip_addresses=ec2.IpAddresses.cidr("10.80.0.0/24"),
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="isolated",
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=26,
                )
            ],
            enable_dns_hostnames=False,
            enable_dns_support=True,
        )

        self.sandbox_sg = ec2.SecurityGroup(
            self,
            "sandbox-sg",
            vpc=self.sandbox_vpc,
            description="sandbox-executor: no egress rules at all",
            allow_all_outbound=False,
        )

        # ------------------------------------------------------------- bus
        self.bus = events.EventBus(self, "bus", event_bus_name=self.name("agent-events"))
        # 90 days of replayable history for scripts/replay_archive.py (docs/09 §4).
        events.Archive(
            self,
            "bus-archive",
            source_event_bus=self.bus,
            archive_name=self.name("agent-events-archive"),
            retention=cdk.Duration.days(90),
            event_pattern=events.EventPattern(account=[self.account]),
        )

        # ------------------------------------------------------------- SNS
        self.ops_topic = sns.Topic(
            self, "ops-alerts", topic_name=self.name("ops-alerts"), display_name="agent ops alerts"
        )

        # ------------------------------------------------------ parameters
        # Values are placeholders written by CDK once; scripts/params.py sets the
        # real ones. CDK cannot create SecureString parameters, so these are
        # created as String and converted on first write — recorded in the
        # parameter description so it is not mistaken for a design choice.
        self.parameters: dict[str, ssm.StringParameter] = {}
        for param in (*SECURE_PARAMETERS, *PLAIN_PARAMETERS):
            secure = param in SECURE_PARAMETERS
            self.parameters[param] = ssm.StringParameter(
                self,
                f"param-{param}",
                parameter_name=f"/agent/{self.stage}/{param}",
                string_value="unset",
                description=(
                    "SET BY scripts/params.py — write as SecureString"
                    if secure
                    else "Set by scripts/params.py"
                ),
            )

        # --------------------------------------------------------- budget
        # Two free budgets, notifying the ops topic at 70% and 90% (docs/06 §8).
        for threshold in (70, 90):
            budgets.CfnBudget(
                self,
                f"budget-{threshold}",
                budget=budgets.CfnBudget.BudgetDataProperty(
                    budget_type="COST",
                    time_unit="MONTHLY",
                    budget_name=self.name(f"monthly-{threshold}"),
                    budget_limit=budgets.CfnBudget.SpendProperty(
                        amount=MONTHLY_BUDGET_USD, unit="USD"
                    ),
                ),
                notifications_with_subscribers=[
                    budgets.CfnBudget.NotificationWithSubscribersProperty(
                        notification=budgets.CfnBudget.NotificationProperty(
                            comparison_operator="GREATER_THAN",
                            notification_type="ACTUAL",
                            threshold=threshold,
                            threshold_type="PERCENTAGE",
                        ),
                        subscribers=[
                            budgets.CfnBudget.SubscriberProperty(
                                address=self.ops_topic.topic_arn, subscription_type="SNS"
                            )
                        ],
                    )
                ],
            )

        # ------------------------------------------------ access.denied
        # The design's single custom metric (docs/06 §8). A management-events
        # trail is free for the first copy; the metric filter turns any
        # AccessDenied by an agent role into the Level 2 signal the supervisor
        # pre-classifies without deliberation (ADR-13).
        #
        # Only prod carries it: the metric is what an alarm watches, and alarms
        # are prod-only, so an ephemeral stage would pay for a trail nobody reads.
        if self.is_prod:
            trail_logs = logs.LogGroup(
                self,
                "trail-logs",
                log_group_name=self.name("cloudtrail"),
                retention=logs.RetentionDays.TWO_WEEKS,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            )
            trail_bucket = s3.Bucket(
                self,
                "trail-bucket",
                bucket_name=f"{self.stage}-agent-trail-{self.account}",
                encryption=s3.BucketEncryption.S3_MANAGED,
                block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
                enforce_ssl=True,
                removal_policy=cdk.RemovalPolicy.DESTROY,
                auto_delete_objects=True,
                lifecycle_rules=[
                    # The CloudWatch copy is what is queried; S3 is only the
                    # delivery target CloudTrail requires.
                    s3.LifecycleRule(id="expire-fast", expiration=cdk.Duration.days(14))
                ],
            )
            trail = cloudtrail.Trail(
                self,
                "trail",
                trail_name=self.name("management-events"),
                bucket=trail_bucket,
                cloud_watch_log_group=trail_logs,
                send_to_cloud_watch_logs=True,
                include_global_service_events=True,
                is_multi_region_trail=False,
                management_events=cloudtrail.ReadWriteType.WRITE_ONLY,
            )
            trail.node.add_dependency(trail_bucket)

            trail_logs.add_metric_filter(
                "access-denied-filter",
                filter_pattern=logs.FilterPattern.all(
                    logs.FilterPattern.string_value("$.errorCode", "=", "*AccessDenied*"),
                    logs.FilterPattern.string_value(
                        "$.userIdentity.sessionContext.sessionIssuer.userName",
                        "=",
                        f"{self.stage}-agent-*",
                    ),
                ),
                metric_namespace="Agent",
                metric_name="AccessDenied",
                metric_value="1",
                # `defaultValue` and `dimensions` are mutually exclusive in
                # CloudWatch, and CloudFormation only says so at create time
                # (a template assertion cannot catch it). The default wins: it
                # publishes a 0 when nothing matches, which keeps the alarm in
                # OK rather than INSUFFICIENT_DATA, and an alarm that is only
                # ever in one of two states is the whole point of this metric.
                # The `stage` dimension distinguished nothing anyway — there is
                # one stage (ADR-32), and the namespace is already ours.
                default_value=0,
            )

        # ----------------------------------------------------- admin role
        # What scripts/ and the control panel assume (ADR-25). The only
        # principal with delete rights on live data.
        self.admin_role = iam.Role(
            self,
            "admin-role",
            role_name=self.name("agent-admin"),
            assumed_by=iam.AccountPrincipal(self.account),
            description="Owner-assumed admin role for scripts/ and the control panel",
            max_session_duration=cdk.Duration.hours(4),
        )
        self.admin_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:*",
                    "s3:*",
                    "s3vectors:*",
                    "ssm:GetParameter",
                    "ssm:GetParameters",
                    "ssm:PutParameter",
                    "lambda:InvokeFunction",
                    "lambda:PutFunctionConcurrency",
                    "lambda:DeleteFunctionConcurrency",
                    "lambda:UpdateFunctionConfiguration",
                    "lambda:GetFunction*",
                    "lambda:ListVersionsByFunction",
                    "lambda:UpdateAlias",
                    "events:DisableRule",
                    "events:EnableRule",
                    "events:PutEvents",
                    "events:DescribeRule",
                    "sqs:*",
                    "logs:FilterLogEvents",
                    "logs:GetLogEvents",
                    "logs:DescribeLogGroups",
                    "logs:DeleteLogGroup",
                    "cloudwatch:GetMetricData",
                    "cloudwatch:DescribeAlarms",
                    "cloudwatch:DeleteAlarms",
                    "cloudformation:DescribeStacks",
                    "cloudformation:DeleteStack",
                    "cloudformation:ListStackResources",
                    "budgets:ViewBudget",
                    "budgets:DeleteBudget",
                ],
                resources=["*"],
                conditions={"StringEquals": {"aws:RequestedRegion": self.region}},
            )
        )

        cdk.CfnOutput(self, "bus-name", value=self.bus.event_bus_name)
        cdk.CfnOutput(self, "ops-topic-arn", value=self.ops_topic.topic_arn)
        cdk.CfnOutput(self, "admin-role-arn", value=self.admin_role.role_arn)
