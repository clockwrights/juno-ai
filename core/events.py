"""Publishing to the `agent-events` bus.

Every component that emits an event goes through `put_event`, so the envelope
(docs/03 §1) and the `source` value are consistent and the supervisor's rules
can match on them (docs/03 §4). `source` is fixed per component and is *not* a
caller argument at the call site — the agent roles are IAM-restricted to
`agent.runtime`, so a mismatch would fail at AWS rather than silently mislabel.
"""

from __future__ import annotations

import json
import os
from typing import Any, Final, Literal

import boto3

from core import logging as jlog
from core.clock import SYSTEM_CLOCK, Clock
from core.contracts import envelope

_log = jlog.get_logger(__name__)

Source = Literal[
    "agent.runtime",
    "agent.gate",
    "agent.pipeline",
    "agent.alarm",
    "agent.supervisor",
]

#: Every detail-type the design defines (docs/03 §4). Emitting anything else is
#: a bug: the supervisor would never see it.
DETAIL_TYPES: Final[frozenset[str]] = frozenset(
    {
        "conversation.completed",
        "conversation.failed",
        "heartbeat.completed",
        "heartbeat.failed",
        "governor.skipped",
        "governor.clamped",
        "notification.sent",
        "notification.budget_exhausted",
        "model.budget_exhausted",
        "model.upstream_error",
        "executor.anomaly",
        "access.denied",
        "proposal.submitted",
        "proposal.rejected",
        "proposal.created",
        "deploy.completed",
        "deploy.rolled_back",
        "budget.threshold",
        "supervisor.decision",
    }
)


class UnknownDetailTypeError(ValueError):
    """Emitting a detail-type not in the contract."""


def _client() -> Any:
    return boto3.client("events")


def put_event(
    detail_type: str,
    source: Source,
    detail: dict[str, Any],
    *,
    stage: str | None = None,
    trace_id: str | None = None,
    bus_name: str | None = None,
    clock: Clock = SYSTEM_CLOCK,
    client: Any | None = None,
) -> None:
    """Publish one event, envelope included.

    Failures are logged and swallowed: an event is telemetry, and losing one
    must never fail the flow that produced it. The supervisor has its own
    silence alarm (docs/06 §9) for the case where events stop arriving.
    """
    if detail_type not in DETAIL_TYPES:
        raise UnknownDetailTypeError(detail_type)

    stage = stage or os.environ["STAGE"]
    bus = bus_name or os.environ.get("EVENT_BUS_NAME", f"{stage}-agent-events")
    body = {**envelope(stage, trace_id, clock), **detail}

    try:
        (client or _client()).put_events(
            Entries=[
                {
                    "EventBusName": bus,
                    "Source": source,
                    "DetailType": detail_type,
                    "Detail": json.dumps(body, default=str),
                }
            ]
        )
    except Exception as exc:
        jlog.warning(_log, "event_put_failed", detail_type=detail_type, error=str(exc))
