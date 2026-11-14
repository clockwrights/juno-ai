"""Pause, resume, stop and start (docs/14 §1, ADR-27).

The asymmetry is the design: several things can pause the agent, and only a
person with the owner's credentials can start it again — after reading why it
stopped.
"""

from __future__ import annotations

from typing import Any

import pytest

from ops import actions
from ops.admin import AdminSession, Stage, StageRefusalError
from tests.unit.conftest import STAGE


class FakeTelegram:
    def __init__(self) -> None:
        self.deleted = False
        self.webhook: tuple[str, str] | None = None

    def delete_webhook(self) -> None:
        self.deleted = True

    def set_webhook(self, url: str, secret: str) -> None:
        self.webhook = (url, secret)


class FakeEvents:
    def __init__(self) -> None:
        self.disabled: list[str] = []
        self.enabled: list[str] = []

    def disable_rule(self, Name: str) -> dict[str, Any]:  # noqa: N803
        self.disabled.append(Name)
        return {}

    def enable_rule(self, Name: str) -> dict[str, Any]:  # noqa: N803
        self.enabled.append(Name)
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


class FakeSts:
    @staticmethod
    def get_caller_identity() -> dict[str, str]:
        return {"Arn": "arn:aws:iam::000000000000:user/owner"}


@pytest.fixture
def stage() -> Stage:
    return Stage(name=STAGE, account="000000000000", region="eu-central-1")


@pytest.fixture
def session(aws: dict[str, Any], stage: Stage) -> AdminSession:
    admin = AdminSession(stage, assume=False)
    admin._clients.update(
        {
            "dynamodb": aws["dynamodb"],
            "s3": aws["s3"],
            "events": FakeEvents(),
            "lambda": FakeLambda(),
            "sts": FakeSts(),
        }
    )
    return admin


def pause_state(aws: dict[str, Any]) -> dict[str, Any]:
    return (
        aws["dynamodb"]
        .get_item(
            TableName=f"{STAGE}-agent_state",
            Key={"PK": {"S": "PAUSE"}, "SK": {"S": "current"}},
        )
        .get("Item", {})
    )


def test_pause_does_all_three_things(session: AdminSession, stage: Stage, aws: Any) -> None:
    result = actions.pause(session, stage, reason="investigating diary drift")

    assert pause_state(aws)["paused"]["BOOL"] is True
    assert set(session.client("events").disabled) == {
        f"{STAGE}-governor-rate",
        f"{STAGE}-governor-weekly",
    }
    assert session.client("lambda").concurrency == {
        f"{STAGE}-agent-conversation": 0,
        f"{STAGE}-agent-heartbeat": 0,
    }
    assert result.journal_sk


def test_pause_records_who_and_why(session: AdminSession, stage: Stage, aws: Any) -> None:
    actions.pause(session, stage, reason="a specific reason")
    item = pause_state(aws)
    assert item["reason"]["S"] == "a specific reason"
    assert item["by"]["S"] == "human:owner"


def test_the_pause_is_journaled(session: AdminSession, stage: Stage, aws: Any) -> None:
    """A human action is journaled next to the supervisor's own (docs/14 §1)."""
    actions.pause(session, stage, reason="x")
    entries = aws["dynamodb"].scan(TableName=f"{STAGE}-supervisor_journal")["Items"]
    assert entries[0]["event_type"]["S"] == "human.pause"
    assert entries[0]["by"]["S"] == "human:owner"


def test_resume_restores_everything(session: AdminSession, stage: Stage, aws: Any) -> None:
    actions.pause(session, stage, reason="x")
    actions.resume(session, stage)

    assert pause_state(aws)["paused"]["BOOL"] is False
    assert set(session.client("events").enabled) == {
        f"{STAGE}-governor-rate",
        f"{STAGE}-governor-weekly",
    }
    assert session.client("lambda").concurrency == {
        f"{STAGE}-agent-conversation": 2,
        f"{STAGE}-agent-heartbeat": 1,
    }


def test_resume_refuses_while_a_level_two_is_unacknowledged(
    session: AdminSession, stage: Stage, aws: Any
) -> None:
    """An agent paused because something was wrong should not be restarted by
    someone who has not read why."""
    aws["dynamodb"].put_item(
        TableName=f"{STAGE}-supervisor_journal",
        Item={
            "PK": {"S": "JOURNAL"},
            "SK": {"S": actions._now()},
            "event_type": {"S": "access.denied"},
            "level": {"N": "2"},
            "action": {"S": "pause"},
            "summary": {"S": "an agent role attempted a denied action"},
        },
    )
    with pytest.raises(StageRefusalError, match="not been acknowledged"):
        actions.resume(session, stage)


def test_acknowledging_the_decision_allows_resume(
    session: AdminSession, stage: Stage, aws: Any
) -> None:
    sk = actions._now()
    aws["dynamodb"].put_item(
        TableName=f"{STAGE}-supervisor_journal",
        Item={
            "PK": {"S": "JOURNAL"},
            "SK": {"S": sk},
            "event_type": {"S": "access.denied"},
            "level": {"N": "2"},
            "action": {"S": "pause"},
            "summary": {"S": "s"},
        },
    )
    actions.resume(session, stage, ack=sk)
    assert pause_state(aws)["paused"]["BOOL"] is False


def test_a_humans_own_pause_does_not_block_their_resume(
    session: AdminSession, stage: Stage
) -> None:
    """Otherwise pausing deliberately would lock the owner out for an hour."""
    actions.pause(session, stage, reason="going away")
    actions.resume(session, stage)


def test_stop_also_removes_the_webhook(session: AdminSession, stage: Stage, aws: Any) -> None:
    """ADR-27: no webhook means no inbound is even accepted."""
    telegram = FakeTelegram()
    result = actions.stop(session, stage, reason="away for a week", telegram=telegram)
    assert telegram.deleted is True
    assert pause_state(aws)["paused"]["BOOL"] is True
    assert any("webhook" in d for d in result.details)


def test_a_failed_webhook_delete_does_not_undo_the_pause(
    session: AdminSession, stage: Stage, aws: Any
) -> None:
    class Broken:
        def delete_webhook(self) -> None:
            raise RuntimeError("telegram is down")

    result = actions.stop(session, stage, reason="x", telegram=Broken())
    assert pause_state(aws)["paused"]["BOOL"] is True, "the pause is what matters"
    assert any("FAILED" in d for d in result.details)


def test_flags_default_to_the_contract(session: AdminSession, stage: Stage) -> None:
    flags = actions.list_flags(session, stage)
    assert flags["proposals_enabled"] is False
    assert flags["heartbeat_enabled"] is True


def test_setting_a_flag_is_journaled(session: AdminSession, stage: Stage, aws: Any) -> None:
    actions.set_flag(session, stage, "proposals_enabled", True)
    assert actions.list_flags(session, stage)["proposals_enabled"] is True
    entries = aws["dynamodb"].scan(TableName=f"{STAGE}-supervisor_journal")["Items"]
    assert entries[0]["event_type"]["S"] == "human.flag"


def test_an_unknown_flag_is_refused(session: AdminSession, stage: Stage) -> None:
    with pytest.raises(StageRefusalError, match="unknown flag"):
        actions.set_flag(session, stage, "do_whatever_you_want", True)
