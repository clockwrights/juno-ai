"""The conversation and heartbeat paths (docs/11 VP5 and VP6 acceptance)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from core.storage.base import StageResources
from core.storage.conversations import ConversationStore
from core.storage.state import StateStore
from gates.common.telegram import OPS_MARKER, TelegramClient, ops_message, split_message
from gates.dispatcher.handler import Dispatcher
from gates.governor.handler import DAILY_CAP, Governor, decide
from gates.notify.handler import Notifier
from gates.telegram.handler import SECRET_HEADER, Webhook
from tests.unit.conftest import STAGE

SECRET = "a-webhook-secret"
OWNER = "12345"


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, bool]] = []

    def send_message(self, chat_id: str, text: str, *, ops: bool = False, stage: str = "") -> int:
        self.sent.append((chat_id, text, ops))
        return 1


class FakeSqs:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send_message(self, **kwargs: Any) -> dict[str, Any]:
        self.sent.append(kwargs)
        return {"MessageId": "m1"}


class FakeLambda:
    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.response = response if response is not None else {"reply": "hello back", "usage": {}}
        self.raises: Exception | None = None
        self.function_error: str | None = None
        self.invocations: list[dict[str, Any]] = []

    def invoke(self, **kwargs: Any) -> dict[str, Any]:
        import io

        self.invocations.append(kwargs)
        if self.raises:
            raise self.raises
        payload = {"Payload": io.BytesIO(json.dumps(self.response).encode("utf-8"))}
        if self.function_error:
            payload["FunctionError"] = self.function_error
        return payload


@pytest.fixture
def state(aws: dict[str, Any], resources: StageResources, clock: Any) -> StateStore:
    return StateStore(resources, clock=clock, dynamodb=aws["dynamodb"], s3=aws["s3"])


# ------------------------------------------------------------ gate-telegram


@pytest.fixture
def webhook(aws: dict[str, Any], resources: StageResources, clock: Any) -> Webhook:
    return Webhook(
        resources=resources,
        secret=SECRET,
        owner_chat_id=OWNER,
        queue_url="https://sqs.example/inbound.fifo",
        sqs=FakeSqs(),
        dynamodb=aws["dynamodb"],
        clock=clock,
    )


def update(text: str = "hello", chat_id: str = OWNER, update_id: int = 1) -> dict[str, Any]:
    return {
        "headers": {SECRET_HEADER: SECRET},
        "body": json.dumps(
            {
                "update_id": update_id,
                "message": {"message_id": 9, "chat": {"id": int(chat_id)}, "text": text},
            }
        ),
    }


def test_a_valid_update_is_enqueued(webhook: Webhook) -> None:
    assert webhook.handle(update())["statusCode"] == 200
    body = json.loads(webhook._sqs.sent[0]["MessageBody"])
    assert body["text"] == "hello"
    assert body["conversation_id"] == f"tg:{OWNER}"


def test_a_wrong_secret_is_rejected(webhook: Webhook) -> None:
    """This handler is the entire authentication boundary (ADR-22)."""
    event = update()
    event["headers"][SECRET_HEADER] = "wrong"
    assert webhook.handle(event)["statusCode"] == 403
    assert webhook._sqs.sent == []


def test_a_missing_secret_is_rejected(webhook: Webhook) -> None:
    event = update()
    event["headers"] = {}
    assert webhook.handle(event)["statusCode"] == 403


def test_a_stranger_gets_a_silent_200(webhook: Webhook) -> None:
    """Telling them they were rejected is more than they need to know."""
    response = webhook.handle(update(chat_id="99999"))
    assert response["statusCode"] == 200
    assert webhook._sqs.sent == []


def test_a_duplicate_update_is_enqueued_once(webhook: Webhook) -> None:
    webhook.handle(update(update_id=7))
    webhook.handle(update(update_id=7))
    assert len(webhook._sqs.sent) == 1


def test_the_fifo_group_is_the_conversation(webhook: Webhook) -> None:
    """One turn at a time per chat (docs/02 §3.1)."""
    webhook.handle(update())
    sent = webhook._sqs.sent[0]
    assert sent["MessageGroupId"] == f"tg:{OWNER}"
    assert sent["MessageDeduplicationId"] == "tg:1"


def test_an_empty_message_is_ignored(webhook: Webhook) -> None:
    webhook.handle(update(text="   "))
    assert webhook._sqs.sent == []


def test_malformed_json_does_not_raise(webhook: Webhook) -> None:
    assert webhook.handle({"headers": {SECRET_HEADER: SECRET}, "body": "{"})["statusCode"] == 400


# ---------------------------------------------------------- gate-dispatcher


@pytest.fixture
def dispatcher(aws: dict[str, Any], resources: StageResources, clock: Any) -> Dispatcher:
    return Dispatcher(
        resources=resources,
        state=StateStore(resources, clock=clock, dynamodb=aws["dynamodb"], s3=aws["s3"]),
        conversations=ConversationStore(
            resources, clock=clock, dynamodb=aws["dynamodb"], s3=aws["s3"]
        ),
        telegram=FakeTelegram(),
        lambda_client=FakeLambda(),
        clock=clock,
        agent_arn="arn:aws:lambda:eu-central-1:0:function:test-agent-conversation",
    )


def sqs_event(text: str = "hello", channel: str = "telegram") -> dict[str, Any]:
    body = {
        "schema_version": "1",
        "trace_id": "trace-1",
        "stage": STAGE,
        "issued_at": "2026-09-06T12:00:00Z",
        "channel": channel,
        "conversation_id": "tg:12345",
        "message_id": "tg:1",
        "text": text,
        "received_at": "2026-09-06T12:00:00Z",
        "telegram": {"chat_id": "12345", "update_id": "1", "message_id": 9},
    }
    return {"Records": [{"body": json.dumps(body)}]}


def test_a_turn_is_run_and_the_reply_delivered(dispatcher: Dispatcher) -> None:
    dispatcher.handle(sqs_event())
    assert dispatcher._telegram.sent[0][1] == "hello back"
    transcript = dispatcher.conversations.transcript("tg:12345")
    assert [m.role for m in transcript] == ["user", "assistant"]


def test_a_lock_is_held_for_the_turn(dispatcher: Dispatcher) -> None:
    """The lock is what stops a heartbeat starting alongside a conversation."""
    dispatcher.handle(sqs_event())
    assert dispatcher.state.any_lock_live() is True


def test_a_paused_agent_answers_with_the_paused_text(dispatcher: Dispatcher) -> None:
    dispatcher.state.set_pause(paused=True, by="human:owner", reason="investigating")
    dispatcher.handle(sqs_event())
    assert "paused" in dispatcher._telegram.sent[0][1].lower()
    assert dispatcher._lambda.invocations == [], "a paused agent is not invoked"


def test_a_throttled_invoke_reads_as_paused(dispatcher: Dispatcher) -> None:
    """Reserved concurrency 0 is how a pause looks from the caller's side."""

    # Named exactly as botocore names it: `_is_throttle` matches on the class
    # name, so a tidier name here would test nothing.
    class TooManyRequestsException(Exception):  # noqa: N818
        pass

    dispatcher._lambda.raises = TooManyRequestsException("rate exceeded")
    dispatcher.handle(sqs_event())
    assert "paused" in dispatcher._telegram.sent[0][1].lower()


def test_a_crash_still_answers(dispatcher: Dispatcher) -> None:
    dispatcher._lambda.raises = RuntimeError("kaboom")
    dispatcher.handle(sqs_event())
    assert dispatcher._telegram.sent, "the owner must always get something back"


def test_the_model_call_is_never_retried(dispatcher: Dispatcher) -> None:
    """A retry would double the spend on a turn that already failed, so the
    agent is invoked exactly once even though that once failed."""
    dispatcher._lambda.raises = RuntimeError("kaboom")
    dispatcher.handle(sqs_event())
    assert len(dispatcher._lambda.invocations) == 1


def test_an_agent_error_uses_the_agents_own_reply(dispatcher: Dispatcher) -> None:
    dispatcher._lambda.response = {
        "reply": "I've spent my budget for today.",
        "error": {"type": "budget_exhausted", "message": "spent"},
        "usage": {},
    }
    dispatcher.handle(sqs_event())
    assert dispatcher._telegram.sent[0][1] == "I've spent my budget for today."


def test_the_panel_channel_stores_the_reply_and_sends_nothing(dispatcher: Dispatcher) -> None:
    """docs/02 §3.1: same path, same memory, no Telegram call."""
    dispatcher.handle(sqs_event(channel="panel"))
    assert dispatcher._telegram.sent == []
    transcript = dispatcher.conversations.transcript("tg:12345")
    assert transcript[-1].content == "hello back"


def test_the_budgets_are_passed_to_the_agent(dispatcher: Dispatcher) -> None:
    dispatcher.handle(sqs_event())
    payload = json.loads(dispatcher._lambda.invocations[0]["Payload"])
    assert payload["budgets"]["notifications_remaining"] == 2
    assert payload["budgets"]["tokens_remaining_today"] == 200_000


# ------------------------------------------------------------ gate-governor


def test_a_paused_agent_never_wakes(state: StateStore, clock: Any) -> None:
    state.set_pause(paused=True, by="supervisor", reason="access.denied")
    assert decide(state, force_mode=None, clock=clock).reason == "paused"


def test_the_heartbeat_flag_off_never_wakes(state: StateStore, clock: Any) -> None:
    state.set_flag("heartbeat_enabled", False, by="human:owner")
    assert decide(state, force_mode=None, clock=clock).reason == "paused"


def test_a_live_conversation_blocks_a_cycle(state: StateStore, clock: Any) -> None:
    state.set_lock("tg:1", minutes=30, trace_id="t")
    assert decide(state, force_mode=None, clock=clock).reason == "conversation_active"


def test_with_no_wish_the_default_interval_applies(state: StateStore, clock: Any) -> None:
    decision = decide(state, force_mode=None, clock=clock)
    assert decision.run is True
    assert decision.applied_minutes == 120


def test_a_wish_in_the_future_is_not_yet_due(state: StateStore, clock: Any) -> None:
    state.set_wake_wish(60, reason="later")
    assert decide(state, force_mode=None, clock=clock).reason == "not_due"


def test_a_wish_that_has_arrived_runs(state: StateStore, clock: Any) -> None:
    state.set_wake_wish(60, reason="later")
    clock.advance(minutes=61)
    assert decide(state, force_mode=None, clock=clock).run is True


def test_a_too_short_wish_is_clamped(state: StateStore, clock: Any) -> None:
    """15 minutes is the physical floor; asking for 5 does not change that."""
    state.set_wake_wish(15, reason="soon")
    state.dynamodb.update_item(
        TableName=state.table,
        Key={"PK": {"S": "STATE"}, "SK": {"S": "next_wake"}},
        UpdateExpression="SET wish_minutes = :m",
        ExpressionAttributeValues={":m": {"N": "5"}},
    )
    decision = decide(state, force_mode=None, clock=clock)
    assert decision.clamped_from == 5
    assert decision.applied_minutes == 15


def test_the_weekly_force_ignores_the_wish(state: StateStore, clock: Any) -> None:
    state.set_wake_wish(1440, reason="tomorrow")
    decision = decide(state, force_mode="maintenance", clock=clock)
    assert decision.run is True
    assert decision.mode == "maintenance"


def test_the_weekly_force_still_respects_pause(state: StateStore, clock: Any) -> None:
    state.set_pause(paused=True, by="supervisor", reason="x")
    assert decide(state, force_mode="maintenance", clock=clock).run is False


def test_the_daily_cap_stops_further_cycles(
    aws: dict[str, Any], resources: StageResources, clock: Any, state: StateStore
) -> None:
    governor = Governor(
        resources=resources,
        state=state,
        lambda_client=FakeLambda(),
        clock=clock,
        heartbeat_arn="arn:aws:lambda:eu-central-1:0:function:test-agent-heartbeat",
    )
    state.spend("heartbeats", DAILY_CAP, DAILY_CAP)
    assert governor.handle({}) == {"skipped": "daily_cap"}


def test_a_cycle_is_invoked_asynchronously(
    aws: dict[str, Any], resources: StageResources, clock: Any, state: StateStore
) -> None:
    """The governor must not hold a connection open for twelve minutes."""
    fake = FakeLambda()
    governor = Governor(
        resources=resources,
        state=state,
        lambda_client=fake,
        clock=clock,
        heartbeat_arn="arn:aws:lambda:eu-central-1:0:function:test-agent-heartbeat",
    )
    governor.handle({})
    assert fake.invocations[0]["InvocationType"] == "Event"
    payload = json.loads(fake.invocations[0]["Payload"])
    assert payload["mode"] == "free"
    assert payload["budgets"]["wall_clock_seconds"] == 720


def test_maintenance_carries_the_agenda(
    aws: dict[str, Any], resources: StageResources, clock: Any, state: StateStore
) -> None:
    fake = FakeLambda()
    governor = Governor(
        resources=resources,
        state=state,
        lambda_client=fake,
        clock=clock,
        heartbeat_arn="arn",
    )
    governor.handle({"force_mode": "maintenance"})
    payload = json.loads(fake.invocations[0]["Payload"])
    assert payload["agenda"] == ["close_idle_conversations", "consolidate_memory"]


# -------------------------------------------------------------- gate-notify


@pytest.fixture
def notifier(aws: dict[str, Any], resources: StageResources, clock: Any) -> Notifier:
    return Notifier(
        resources=resources,
        state=StateStore(resources, clock=clock, dynamodb=aws["dynamodb"], s3=aws["s3"]),
        telegram=FakeTelegram(),
        clock=clock,
        chat_id=OWNER,
    )


def notification(text: str = "the roof is leaking") -> dict[str, Any]:
    return {
        "Records": [
            {
                "body": json.dumps(
                    {
                        "schema_version": "1",
                        "trace_id": "t1",
                        "stage": STAGE,
                        "issued_at": "2026-09-06T12:00:00Z",
                        "kind": "notify",
                        "priority": "normal",
                        "text": text,
                        "source": "heartbeat",
                    }
                )
            }
        ]
    }


def test_the_first_two_notifications_are_sent(notifier: Notifier) -> None:
    notifier.handle(notification("one"))
    notifier.handle(notification("two"))
    assert len(notifier._telegram.sent) == 2


def test_the_third_becomes_a_note(notifier: Notifier) -> None:
    """Converted, not dropped: the owner still gets it, just not now."""
    for text in ("one", "two", "three"):
        notifier.handle(notification(text))
    assert len(notifier._telegram.sent) == 2
    notes = notifier.state.pending_notes()
    assert [n["text"] for n in notes] == ["three"]


def test_the_day_rolls_over(notifier: Notifier) -> None:
    notifier.handle(notification("one"))
    notifier.handle(notification("two"))
    notifier.clock.advance(days=1)
    notifier.handle(notification("next day"))
    assert len(notifier._telegram.sent) == 3


def test_urgent_is_marked_but_not_exempt(notifier: Notifier) -> None:
    """docs/03 §3.2: priority is informational, it does not bypass the budget."""
    for _ in range(2):
        notifier.handle(notification("filler"))
    event = notification("urgent thing")
    body = json.loads(event["Records"][0]["body"])
    body["priority"] = "urgent"
    event["Records"][0]["body"] = json.dumps(body)
    notifier.handle(event)
    assert len(notifier._telegram.sent) == 2


# --------------------------------------------------------------- telegram


def test_a_short_message_is_one_chunk() -> None:
    assert split_message("hello") == ["hello"]


def test_a_long_message_splits_on_a_paragraph() -> None:
    text = ("a" * 3000) + "\n\n" + ("b" * 3000)
    chunks = split_message(text)
    assert len(chunks) == 2
    assert chunks[0] == "a" * 3000


def test_every_chunk_fits_telegrams_limit() -> None:
    chunks = split_message("word " * 5000)
    assert all(len(c) <= 4000 for c in chunks)


def test_an_ops_message_carries_the_marker() -> None:
    """ADR-30: one bot, so the marker is what keeps the two voices apart."""
    fake = FakeTelegram()
    client = TelegramClient(token="x")
    client._post = lambda method, payload: (
        fake.sent.append(  # type: ignore[assignment]
            (payload.get("chat_id", ""), payload["text"], True)
        )
        or {}
    )
    client.send_message("1", "the agent was paused", ops=True, stage="prod")
    assert fake.sent[0][1].startswith(OPS_MARKER)
    assert "prod" in fake.sent[0][1]


def test_an_agent_message_carries_no_marker() -> None:
    fake = FakeTelegram()
    client = TelegramClient(token="x")
    client._post = lambda method, payload: (
        fake.sent.append(  # type: ignore[assignment]
            (payload.get("chat_id", ""), payload["text"], False)
        )
        or {}
    )
    client.send_message("1", "hello")
    assert not fake.sent[0][1].startswith(OPS_MARKER)


def test_ops_messages_are_never_in_the_agents_voice() -> None:
    message = ops_message("prod", "Agent paused", "access.denied on agent_state", "https://x")
    assert "Agent paused" in message
    assert "https://x" in message
