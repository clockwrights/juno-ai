"""`gate-notify` — decides whether a notification is actually sent (docs/06 §4).

The agent asks; this decides, atomically. The budget is a conditional update
rather than a read-then-check, so two notifications arriving at once cannot both
see room. When the budget is spent the message is not dropped — it becomes a
note, which the owner sees at the start of their next conversation.

That conversion is the whole point: a budget that loses messages would teach the
agent to hoard its notifications, and a budget that queues them would defeat
itself.
"""

from __future__ import annotations

import json
from typing import Any

from core import logging as jlog
from core.clock import SYSTEM_CLOCK, Clock
from core.events import put_event
from core.storage.base import StageResources
from core.storage.state import BudgetExhaustedError, StateStore

_log = jlog.get_logger("gate-notify")

DAILY_BUDGET = 2

PRIORITY_PREFIX = {"low": "🔔", "normal": "🔔", "urgent": "🔔❗"}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return Notifier().handle(event)


class Notifier:
    def __init__(
        self,
        *,
        resources: StageResources | None = None,
        state: StateStore | None = None,
        telegram: Any = None,
        clock: Clock = SYSTEM_CLOCK,
        chat_id: str = "",
    ) -> None:
        self._resources = resources
        self._state = state
        self._telegram = telegram
        self.clock = clock
        self._chat_id = chat_id

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
    def telegram(self) -> Any:
        if self._telegram is None:
            from gates.common.parameters import get_parameter
            from gates.common.telegram import TelegramClient

            self._telegram = TelegramClient(
                token=get_parameter(f"/agent/{self.resources.stage}/telegram-agent-bot")
            )
        return self._telegram

    @property
    def chat_id(self) -> str:
        if not self._chat_id:
            from gates.common.parameters import get_parameter

            self._chat_id = get_parameter(f"/agent/{self.resources.stage}/owner-chat-id")
        return self._chat_id

    def handle(self, event: dict[str, Any]) -> dict[str, Any]:
        results = []
        for record in event.get("Records", [{"body": json.dumps(event)}]):
            results.append(self._one(json.loads(record["body"])))
        return {"processed": results}

    def _one(self, body: dict[str, Any]) -> str:
        trace_id = str(body.get("trace_id", ""))
        jlog.set_trace_id(trace_id)
        text = str(body.get("text", ""))
        priority = str(body.get("priority", "normal"))

        try:
            self.state.spend("notifications", 1, DAILY_BUDGET)
        except BudgetExhaustedError:
            # Converted, not dropped. The owner still gets it, just not now.
            note_id = self.state.leave_note(text, source="notify_overflow")
            self._emit(
                "notification.budget_exhausted",
                {
                    "dropped_text_hash": _hash(text),
                    "exhausted_at": self.clock.now().isoformat().replace("+00:00", "Z"),
                },
                trace_id,
            )
            jlog.info(_log, "converted_to_note", note_id=note_id)
            return "converted_to_note"

        prefix = PRIORITY_PREFIX.get(priority, "🔔")
        self.telegram.send_message(self.chat_id, f"{prefix} {text}")
        remaining = self.state.remaining("notifications", DAILY_BUDGET)
        self._emit("notification.sent", {"remaining_today": remaining}, trace_id)
        jlog.info(_log, "notification_sent", remaining=remaining, priority=priority)
        return "sent"

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


def _hash(text: str) -> str:
    """The event records that *something* was dropped, never what it said."""
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
