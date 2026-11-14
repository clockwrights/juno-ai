"""Layer 2: the supervisor, plus the six dumb alarms.

The supervisor can read everything and change almost nothing: disable the two
governor rules, zero the two agent concurrencies, write the journal and the
pause item. That is its entire write surface, and there is deliberately no
`events:EnableRule` anywhere in the account — resuming is a human action
(ADR-09).

Alarms exist only on `prod` (ADR-23), which keeps the account inside the free
tier of ten. `infra/tests/test_alarms.py` asserts exactly six on prod and zero
elsewhere.
"""

from __future__ import annotations

from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda_event_sources as sources
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from infra.stacks.agent import AgentStack
from infra.stacks.common import StageStack, lambda_role, make_function
from infra.stacks.foundation import FoundationStack
from infra.stacks.gates import GatesStack
from infra.stacks.storage import StorageStack


class SupervisorStack(StageStack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        foundation: FoundationStack,
        storage: StorageStack,
        gates: GatesStack,
        agent: AgentStack,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ------------------------------------------------------- supervisor
        self.role = lambda_role(self, stack=self, short_name="supervisor")
        self.function = make_function(
            self,
            stack=self,
            short_name="supervisor",
            role=self.role,
            timeout_seconds=120,
            environment={
                "EVENT_BUS_NAME": foundation.bus.event_bus_name,
                "JOURNAL_TABLE": storage.journal.table_name,
                "OPS_TOPIC_ARN": foundation.ops_topic.topic_arn,
            },
        )
        self.function.add_event_source(
            sources.SqsEventSource(storage.supervisor_events, batch_size=1)
        )
        # Imported by ARN so the rule below does not try to edit the queue's
        # policy across stacks; storage.py grants the rule explicitly.
        supervisor_queue = sqs.Queue.from_queue_arn(
            self, "supervisor-events-imported", storage.supervisor_events.queue_arn
        )
        self._grant_supervisor(foundation, storage, gates, agent)

        # Everything on the bus reaches the supervisor's queue; the rubric, not
        # the rule, decides what matters (docs/07).
        events.Rule(
            self,
            "supervisor-rule",
            rule_name=self.name("supervisor-events"),
            event_bus=foundation.bus,
            event_pattern=events.EventPattern(
                source=[
                    "agent.runtime",
                    "agent.gate",
                    "agent.pipeline",
                    "agent.alarm",
                ]
            ),
            targets=[targets.SqsQueue(supervisor_queue)],
        )

        # The governor rules live here rather than in gates/ because the
        # supervisor must be able to disable them, and a rule is easier to
        # reason about next to the thing that switches it off.
        self.governor_rate = events.Rule(
            self,
            "governor-rate",
            rule_name=self.name("governor-rate"),
            schedule=events.Schedule.rate(cdk.Duration.minutes(15)),
            targets=[targets.LambdaFunction(gates.functions["gate-governor"])],
        )
        self.governor_weekly = events.Rule(
            self,
            "governor-weekly",
            rule_name=self.name("governor-weekly"),
            schedule=events.Schedule.cron(minute="0", hour="4", week_day="SUN"),
            targets=[
                targets.LambdaFunction(
                    gates.functions["gate-governor"],
                    event=events.RuleTargetInput.from_object({"force_mode": "maintenance"}),
                )
            ],
        )

        # --------------------------------------------------------- ops-alarms
        self.ops_role = lambda_role(self, stack=self, short_name="ops-alarms")
        self.ops_function = make_function(
            self,
            stack=self,
            short_name="ops-alarms",
            role=self.ops_role,
            environment={"OPS_TOPIC_ARN": foundation.ops_topic.topic_arn},
        )
        foundation.ops_topic.add_subscription(
            __import__(
                "aws_cdk.aws_sns_subscriptions", fromlist=["LambdaSubscription"]
            ).LambdaSubscription(self.ops_function)
        )
        foundation.parameters["telegram-ops-bot"].grant_read(self.ops_role)

        if self.is_prod:
            self._alarms(foundation, storage, agent)

    # ------------------------------------------------------------------ IAM
    def _grant_supervisor(
        self,
        foundation: FoundationStack,
        storage: StorageStack,
        gates: GatesStack,
        agent: AgentStack,
    ) -> None:
        role = self.role
        read_tables = [
            storage.memory,
            storage.diary,
            storage.state,
            storage.conversations,
            storage.proposals,
            storage.sessions,
            storage.journal,
        ]
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["dynamodb:GetItem", "dynamodb:Query"],
                resources=[t.table_arn for t in read_tables]
                + [f"{storage.journal.table_arn}/index/*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(actions=["dynamodb:PutItem"], resources=[storage.journal.table_arn])
        )
        # The whole of the supervisor's power over the agent's state: one item.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["dynamodb:PutItem"],
                resources=[storage.state.table_arn],
                conditions={"ForAllValues:StringEquals": {"dynamodb:LeadingKeys": ["PAUSE"]}},
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"],
                resources=[
                    f"{storage.bucket.bucket_arn}/profile/*",
                    f"{storage.bucket.bucket_arn}/memories/*",
                    f"{storage.bucket.bucket_arn}/sessions/*",
                    f"{storage.bucket.bucket_arn}/prompts/*",
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["events:DisableRule"],
                resources=[
                    f"arn:aws:events:{self.region}:{self.account}:rule/{self.name('governor-rate')}",
                    f"arn:aws:events:{self.region}:{self.account}:rule/"
                    f"{self.name('governor-weekly')}",
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:PutFunctionConcurrency"],
                resources=[f.function_arn for f in agent.functions.values()],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[f"{gates.functions['gate-model'].function_arn}:supervisor"],
            )
        )
        foundation.ops_topic.grant_publish(role)
        foundation.parameters["telegram-ops-bot"].grant_read(role)
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:FilterLogEvents", "logs:GetLogEvents"],
                resources=[
                    f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/lambda/"
                    f"{self.stage}-*"
                ],
            )
        )
        # CloudWatch metric reads have no resource ARNs. This is the only
        # wildcard resource the IAM test permits.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:GetMetricData", "cloudwatch:GetMetricStatistics"],
                resources=["*"],
            )
        )

    # --------------------------------------------------------------- alarms
    def _alarms(
        self, foundation: FoundationStack, storage: StorageStack, agent: AgentStack
    ) -> None:
        """Six alarms, prod only (docs/06 §9).

        Five are CloudWatch alarms; the sixth, `budget-90-no-action`, is a
        delayed self-check inside `ops-alarms` rather than an alarm, because it
        asserts the *absence* of a supervisor decision.
        """
        action = cw_actions.SnsAction(foundation.ops_topic)

        def alarm(alarm_id: str, metric: cw.IMetric, threshold: float, evaluation: int) -> None:
            a = cw.Alarm(
                self,
                alarm_id,
                alarm_name=self.name(alarm_id),
                metric=metric,
                threshold=threshold,
                evaluation_periods=evaluation,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
            a.add_alarm_action(action)

        alarm(
            "supervisor-errors",
            self.function.metric_errors(period=cdk.Duration.minutes(5)),
            1,
            1,
        )

        # Messages arriving while the supervisor never runs: the failure mode
        # that would otherwise be silent.
        alarm(
            "supervisor-silent",
            cw.MathExpression(
                expression="IF(recv > 0 AND inv == 0, 1, 0)",
                using_metrics={
                    "recv": storage.supervisor_events.metric_number_of_messages_received(
                        period=cdk.Duration.hours(1)
                    ),
                    "inv": self.function.metric_invocations(period=cdk.Duration.hours(1)),
                },
                period=cdk.Duration.hours(1),
            ),
            1,
            1,
        )

        alarm(
            "agent-error-rate",
            cw.MathExpression(
                expression="conv + hb",
                using_metrics={
                    "conv": agent.functions["agent-conversation"].metric_errors(
                        period=cdk.Duration.minutes(15)
                    ),
                    "hb": agent.functions["agent-heartbeat"].metric_errors(
                        period=cdk.Duration.minutes(15)
                    ),
                },
                period=cdk.Duration.minutes(15),
            ),
            5,
            1,
        )

        # The design's single custom metric, from a CloudTrail metric filter.
        alarm(
            "access-denied",
            cw.Metric(
                namespace="Agent",
                metric_name="AccessDenied",
                # No dimensions: the metric filter cannot publish them and a
                # default value at the same time. See foundation.py.
                period=cdk.Duration.minutes(5),
                statistic="Sum",
            ),
            1,
            1,
        )

        alarm(
            "dlq-depth",
            cw.MathExpression(
                expression="a + b + c",
                using_metrics={
                    name: q.metric_approximate_number_of_messages_visible(
                        period=cdk.Duration.minutes(5)
                    )
                    for name, q in zip("abc", storage.dlqs, strict=True)
                },
                period=cdk.Duration.minutes(5),
            ),
            1,
            1,
        )
