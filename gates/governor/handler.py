"""`gate-governor` — decides whether a heartbeat runs (ADR-07, docs/06 §3).

The agent writes a *wish*; this decides. Scheduling lives outside agent-editable
code because a bound the agent could edit is not a bound, and because giving the
agent scheduler permissions would mean giving it a way to run itself more often
than the design allows.

A rate(15 min) rule is the physical floor: nothing invokes the agent in between,
so no bug in this file can make it wake more often than that.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass
from typing import Any

from ulid import ULID

from core import logging as jlog
from core.clock import SYSTEM_CLOCK, Clock
from core.contracts import envelope, parse_iso, validate
from core.events import put_event
from core.storage.base import StageResources
from core.storage.state import BudgetExhaustedError, StateStore

_log = jlog.get_logger("gate-governor")

#: docs/13 tunables. Context values in the stack; constants here so the decision
#: is testable without configuration.
MIN_MINUTES = 15
MAX_MINUTES = 1440
DEFAULT_MINUTES = 120
DAILY_CAP = 48
WALL_CLOCK_SECONDS = 720
NOTIFICATION_BUDGET = 2


@dataclass
class Decision:
    run: bool
    reason: str = ""
    mode: str = "free"
    applied_minutes: int = DEFAULT_MINUTES
    clamped_from: int | None = None


def decide(state: StateStore, *, force_mode: str | None, clock: Clock) -> Decision:
    """The whole scheduling decision, as a pure-ish function of stored state."""
    flags = state.flags()
    if state.pause_state().paused or not flags.get("heartbeat_enabled", True):
        return Decision(run=False, reason="paused")

    # A conversation in flight blocks a cycle: two of the agent running at once
    # would double-spend the budget and race on the profile.
    if state.any_lock_live():
        return Decision(run=False, reason="conversation_active")

    mode = "maintenance" if force_mode == "maintenance" else "free"

    if mode == "free":
        wish = state.wake_wish()
        if wish:
            wished = int(wish["wish_minutes"])
            applied = max(MIN_MINUTES, min(MAX_MINUTES, wished))
            due_at = parse_iso(wish["wish_at"])
            if applied != wished:
                # The agent asked for something out of bounds; it still gets a
                # cycle, just not the one it asked for.
                return Decision(
                    run=_due(due_at, clock, applied, wished),
                    reason="" if _due(due_at, clock, applied, wished) else "not_due",
                    mode=mode,
                    applied_minutes=applied,
                    clamped_from=wished,
                )
            if clock.now() < due_at:
                return Decision(run=False, reason="not_due", mode=mode, applied_minutes=applied)
            return Decision(run=True, mode=mode, applied_minutes=applied)
        return Decision(run=True, mode=mode, applied_minutes=DEFAULT_MINUTES)

    # The weekly cron ignores `next_wake` but still respects pause and the cap.
    return Decision(run=True, mode=mode, applied_minutes=DEFAULT_MINUTES)


def _due(due_at: dt.datetime, clock: Clock, applied: int, wished: int) -> bool:
    """A clamped wish is measured from when it was written, not from the wish."""
    adjusted = due_at + dt.timedelta(minutes=applied - wished)
    return bool(clock.now() >= adjusted)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return Governor().handle(event)


class Governor:
    def __init__(
        self,
        *,
        resources: StageResources | None = None,
        state: StateStore | None = None,
        lambda_client: Any = None,
        clock: Clock = SYSTEM_CLOCK,
        heartbeat_arn: str = "",
    ) -> None:
        self._resources = resources
        self._state = state
        self._lambda = lambda_client
        self.clock = clock
        self.heartbeat_arn = heartbeat_arn or os.environ.get("HEARTBEAT_ARN", "")

    @property
    def resources(self) -> StageResources:
        if self._resources is None:
            self._resources = StageResources.from_env()
        return self._resources

    @property
    def state(self) -> StateStore:
        if self._state is None:
            self._state = StateStore(self.resources, clock=self.clock)
        return self._state

    @property
    def lambda_client(self) -> Any:
        if self._lambda is None:
            import boto3

            self._lambda = boto3.client("lambda")
        return self._lambda

    def handle(self, event: dict[str, Any]) -> dict[str, Any]:
        stage = self.resources.stage
        trace_id = str(ULID())
        jlog.set_trace_id(trace_id)

        decision = decide(self.state, force_mode=event.get("force_mode"), clock=self.clock)

        if decision.clamped_from is not None:
            self._emit(
                "governor.clamped",
                {
                    "wished_minutes": decision.clamped_from,
                    "applied_minutes": decision.applied_minutes,
                },
                trace_id,
            )

        if not decision.run:
            self._emit("governor.skipped", {"reason": decision.reason}, trace_id)
            return {"skipped": decision.reason}

        # The cap is a conditional update, so two governor invocations racing
        # cannot both pass it.
        try:
            self.state.spend("heartbeats", 1, DAILY_CAP)
        except BudgetExhaustedError:
            self._emit("governor.skipped", {"reason": "daily_cap"}, trace_id)
            return {"skipped": "daily_cap"}

        invocation_id = str(ULID())
        payload = {
            **envelope(stage, trace_id),
            "mode": decision.mode,
            "invocation_id": invocation_id,
            "budgets": {
                "notifications_remaining": self.state.remaining(
                    "notifications", NOTIFICATION_BUDGET
                ),
                "tokens_remaining_today": self._tokens_remaining(decision.mode),
                "wall_clock_seconds": WALL_CLOCK_SECONDS,
            },
            "agenda": _agenda(decision.mode),
        }
        validate("heartbeat-request", payload)

        # Async: the governor must not hold a connection open for twelve
        # minutes, and it has nothing to do with the result.
        self.lambda_client.invoke(
            FunctionName=self.heartbeat_arn,
            InvocationType="Event",
            Payload=json.dumps(payload).encode("utf-8"),
        )
        jlog.info(_log, "heartbeat_invoked", mode=decision.mode, invocation_id=invocation_id)
        return {"invoked": invocation_id, "mode": decision.mode}

    def _tokens_remaining(self, mode: str) -> int:
        from gates.model.policy import policy_for

        role = "heartbeat"
        policy = policy_for(role)
        return self.state.remaining(f"tokens#{role}", policy.daily_token_budget)

    def _emit(self, detail_type: str, detail: dict[str, Any], trace_id: str) -> None:
        try:
            put_event(
                detail_type,
                "agent.gate",
                detail,
                stage=self.resources.stage,
                trace_id=trace_id,
            )
        except Exception as exc:
            jlog.warning(_log, "event_failed", detail_type=detail_type, error=str(exc))


def _agenda(mode: str) -> list[str]:
    if mode == "maintenance":
        return ["close_idle_conversations", "consolidate_memory"]
    return []
