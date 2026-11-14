"""Event publishing (docs/03 §4)."""

from __future__ import annotations

from typing import Any

import pytest

from core import events


class FakeEvents:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def put_events(self, Entries: list[dict[str, Any]]) -> dict[str, Any]:  # noqa: N803
        self.entries.extend(Entries)
        return {"FailedEntryCount": 0}


class BrokenEvents:
    def put_events(self, Entries: list[dict[str, Any]]) -> dict[str, Any]:  # noqa: N803
        raise RuntimeError("EventBridge is down")


def test_a_known_detail_type_is_published() -> None:
    client = FakeEvents()
    events.put_event(
        "heartbeat.completed",
        "agent.runtime",
        {"invocation_id": "01J", "outcome": "worked"},
        stage="dev",
        trace_id="t-1",
        bus_name="dev-agent-events",
        client=client,
    )
    entry = client.entries[0]
    assert entry["DetailType"] == "heartbeat.completed"
    assert entry["Source"] == "agent.runtime"
    assert '"trace_id": "t-1"' in entry["Detail"] or '"trace_id":"t-1"' in entry["Detail"]


def test_an_unknown_detail_type_is_refused() -> None:
    """A typo would mean the supervisor silently never sees the event."""
    with pytest.raises(events.UnknownDetailTypeError):
        events.put_event(
            "heartbeat.finished",
            "agent.runtime",
            {},
            stage="dev",
            bus_name="b",
            client=FakeEvents(),
        )


def test_the_envelope_is_always_present() -> None:
    client = FakeEvents()
    events.put_event(
        "governor.skipped",
        "agent.gate",
        {"reason": "paused"},
        stage="prod",
        bus_name="b",
        client=client,
    )
    import json

    detail = json.loads(client.entries[0]["Detail"])
    assert detail["schema_version"] == "1"
    assert detail["stage"] == "prod"
    assert detail["reason"] == "paused"
    assert detail["trace_id"]


def test_a_publish_failure_does_not_raise() -> None:
    """Telemetry must never break the flow that produced it."""
    events.put_event(
        "conversation.completed",
        "agent.runtime",
        {},
        stage="dev",
        bus_name="b",
        client=BrokenEvents(),
    )


def test_every_documented_detail_type_is_known() -> None:
    for detail_type in ("access.denied", "deploy.rolled_back", "model.budget_exhausted"):
        assert detail_type in events.DETAIL_TYPES
