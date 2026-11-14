"""The supervisor (docs/07, docs/11 VP7 acceptance).

The property that matters most: the pause happens before the model is
consulted, so a supervisor whose model is down still stops the agent (ADR-13).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from core.storage.base import StageResources
from core.storage.state import StateStore
from supervisor.handler import Supervisor
from supervisor.rubric import PreClassification, pre_classify, reconcile
from tests.unit.conftest import STAGE


class FakeEvents:
    def __init__(self) -> None:
        self.disabled: list[str] = []
        self.enabled: list[str] = []

    def disable_rule(self, Name: str) -> dict[str, Any]:  # noqa: N803
        self.disabled.append(Name)
        return {}


class FakeLambda:
    def __init__(self) -> None:
        self.concurrency: dict[str, int] = {}

    # These are boto3's own parameter names; the fake has to accept them.
    def put_function_concurrency(
        self,
        FunctionName: str,  # noqa: N803
        ReservedConcurrentExecutions: int,  # noqa: N803
    ) -> dict[str, Any]:
        self.concurrency[FunctionName] = ReservedConcurrentExecutions
        return {}


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, bool]] = []

    def send_message(self, chat_id: str, text: str, *, ops: bool = False, stage: str = "") -> int:
        self.sent.append((chat_id, text, ops))
        return 1


class FakeModel:
    def __init__(self, payload: dict[str, Any] | None = None, *, broken: bool = False) -> None:
        self.payload = payload
        self.broken = broken
        self.calls = 0

    def invoke(self, **kwargs: Any) -> Any:
        from core.model_client import ModelResponse

        self.calls += 1
        if self.broken:
            raise RuntimeError("the supervisor's own model is down")
        if self.payload is None:
            return ModelResponse(error={"type": "upstream_error", "message": "down"})
        return ModelResponse(content=json.dumps(self.payload), finish_reason="stop")


@pytest.fixture
def supervisor(aws: dict[str, Any], resources: StageResources, clock: Any) -> Supervisor:
    return Supervisor(
        resources=resources,
        state=StateStore(resources, clock=clock, dynamodb=aws["dynamodb"], s3=aws["s3"]),
        model=FakeModel({"level": 0, "action": "log", "summary": "s", "probable_cause": "c"}),
        telegram=FakeTelegram(),
        lambda_client=FakeLambda(),
        events_client=FakeEvents(),
        dynamodb=aws["dynamodb"],
        clock=clock,
        ops_chat_id="99",
    )


def bus_event(detail_type: str, **detail: Any) -> dict[str, Any]:
    return {
        "Records": [
            {
                "body": json.dumps(
                    {
                        "detail-type": detail_type,
                        "detail": {
                            "schema_version": "1",
                            "trace_id": "t1",
                            "stage": STAGE,
                            "issued_at": "2026-09-06T12:00:00Z",
                            **detail,
                        },
                    }
                )
            }
        ]
    }


# ------------------------------------------------------- pre-classification


def test_access_denied_is_always_level_two() -> None:
    """There is no benign reading of an agent role testing an IAM boundary."""
    pre = pre_classify("access.denied", {"action": "dynamodb:DeleteItem"}, 0)
    assert pre.level == 2
    assert pre.decided is True


def test_budget_at_ninety_percent_is_level_two() -> None:
    assert pre_classify("budget.threshold", {"percent": 90}, 0).level == 2


def test_budget_at_seventy_percent_is_level_one() -> None:
    assert pre_classify("budget.threshold", {"percent": 70}, 0).level == 1


def test_repeated_network_attempts_are_level_two() -> None:
    pre = pre_classify("executor.anomaly", {"kind": "network_attempt", "count_24h": 3}, 3)
    assert pre.level == 2
    assert pre.decided is True


def test_a_single_network_attempt_is_not() -> None:
    assert (
        pre_classify("executor.anomaly", {"kind": "network_attempt", "count_24h": 1}, 1).level == 0
    )


def test_a_high_memory_rate_outside_maintenance_is_level_two() -> None:
    pre = pre_classify(
        "heartbeat.completed",
        {"memory_ops": {"saved": 60, "forgotten": 0}, "source": "heartbeat"},
        0,
    )
    assert pre.level == 2


def test_the_same_rate_during_maintenance_is_fine() -> None:
    """ADR-18: consolidation legitimately tombstones many items on a Sunday."""
    pre = pre_classify(
        "heartbeat.completed",
        {"memory_ops": {"forgotten": 80}, "source": "maintenance"},
        0,
    )
    assert pre.level == 0


def test_three_of_anything_in_a_day_is_a_pattern() -> None:
    assert pre_classify("conversation.failed", {}, 3).level == 1


def test_an_isolated_failure_is_level_zero() -> None:
    assert pre_classify("conversation.failed", {}, 1).level == 0


# ------------------------------------------------------------- reconciling


def test_a_preclassified_level_two_cannot_be_lowered() -> None:
    pre = PreClassification(2, "access.denied", decided=True)
    level, action, note = reconcile(pre, 0, "log")
    assert level == 2
    assert action == "pause"
    assert "the rule wins" in note


def test_the_model_may_raise_the_level() -> None:
    level, action, _ = reconcile(PreClassification(0, ""), 2, "pause")
    assert level == 2
    assert action == "pause"


def test_inconsistent_model_output_becomes_level_one() -> None:
    """Level 2 with action 'log' is a signal in itself, not something to fix."""
    level, action, note = reconcile(PreClassification(0, ""), 2, "log")
    assert level == 1
    assert action == "notify"
    assert "action" in note


def test_unusable_model_output_becomes_level_one() -> None:
    level, _action, note = reconcile(PreClassification(0, ""), None, None)
    assert level == 1
    assert "nothing usable" in note


def test_an_unknown_level_becomes_level_one() -> None:
    assert reconcile(PreClassification(0, ""), 7, "log")[0] == 1


# ---------------------------------------------------------------- handler


def test_access_denied_pauses_all_three_ways(supervisor: Supervisor) -> None:
    """Any one of the three stops the agent; doing all three means a single
    API failure does not leave it half-paused."""
    supervisor.handle(bus_event("access.denied", principal_role="agent-heartbeat"))

    assert supervisor.state.pause_state().paused is True
    assert set(supervisor._events.disabled) == {
        f"{STAGE}-governor-rate",
        f"{STAGE}-governor-weekly",
    }
    assert supervisor._lambda.concurrency == {
        f"{STAGE}-agent-conversation": 0,
        f"{STAGE}-agent-heartbeat": 0,
    }


def test_the_pause_happens_even_when_the_model_is_down(
    aws: dict[str, Any], resources: StageResources, clock: Any
) -> None:
    """ADR-13, literally: the supervisor's model can be down and Level 2 works."""
    supervisor = Supervisor(
        resources=resources,
        state=StateStore(resources, clock=clock, dynamodb=aws["dynamodb"], s3=aws["s3"]),
        model=FakeModel(broken=True),
        telegram=FakeTelegram(),
        lambda_client=FakeLambda(),
        events_client=FakeEvents(),
        dynamodb=aws["dynamodb"],
        clock=clock,
        ops_chat_id="99",
    )
    with pytest.raises(RuntimeError):
        supervisor.handle(bus_event("access.denied"))
    assert supervisor.state.pause_state().paused is True, "paused before the model was asked"


def test_a_level_zero_event_writes_a_journal_entry_only(supervisor: Supervisor) -> None:
    supervisor.handle(bus_event("conversation.failed", error={"type": "timeout"}))
    assert supervisor.state.pause_state().paused is False
    assert supervisor._telegram.sent == []
    entries = supervisor.dynamodb.scan(TableName=f"{STAGE}-supervisor_journal")["Items"]
    assert len(entries) == 1
    assert entries[0]["level"]["N"] == "0"


def test_a_level_one_event_notifies_without_pausing(supervisor: Supervisor) -> None:
    supervisor.handle(bus_event("deploy.rolled_back", version="v3"))
    assert supervisor.state.pause_state().paused is False
    assert len(supervisor._telegram.sent) == 1
    assert supervisor._telegram.sent[0][2] is True, "ops messages carry the marker (ADR-30)"


def test_the_pause_message_tells_the_owner_how_to_resume(supervisor: Supervisor) -> None:
    """Resume is a human action, so the message has to say what the action is."""
    supervisor.handle(bus_event("access.denied"))
    assert "resume.py" in supervisor._telegram.sent[0][1]


def test_the_journal_records_that_a_rule_decided(supervisor: Supervisor) -> None:
    supervisor.handle(bus_event("access.denied"))
    entry = supervisor.dynamodb.scan(TableName=f"{STAGE}-supervisor_journal")["Items"][0]
    assert entry["preclassified"]["BOOL"] is True
    assert entry["level"]["N"] == "2"


def test_a_metric_only_event_is_skipped(supervisor: Supervisor) -> None:
    result = supervisor.handle(bus_event("notification.sent", remaining_today=1))
    assert result["decisions"][0]["action"] == "skipped"
    assert supervisor.dynamodb.scan(TableName=f"{STAGE}-supervisor_journal")["Items"] == []


def test_a_metric_only_event_with_alarming_numbers_is_not_skipped(
    supervisor: Supervisor,
) -> None:
    result = supervisor.handle(
        bus_event(
            "heartbeat.completed",
            memory_ops={"saved": 80},
            source="heartbeat",
        )
    )
    assert result["decisions"][0]["level"] == 2


def test_the_model_is_asked_only_to_describe_a_preclassified_event(
    supervisor: Supervisor,
) -> None:
    supervisor._model = FakeModel(
        {"level": 0, "action": "log", "summary": "described", "probable_cause": "a bug"}
    )
    result = supervisor.handle(bus_event("access.denied"))
    assert result["decisions"][0]["level"] == 2, "the model cannot lower it"
    assert supervisor._model.calls == 1


def test_the_supervisor_cannot_enable_a_rule() -> None:
    """Resuming is a human action (ADR-09); there is no code path to it here."""
    assert not hasattr(Supervisor, "resume")
    assert "enable_rule" not in dir(Supervisor)
