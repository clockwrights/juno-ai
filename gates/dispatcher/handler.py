"""`gate-dispatcher` — runs one turn and always answers (docs/06 §2).

The dispatcher holds the bot token; the agent does not (ADR-03). It is also the
component that guarantees the owner gets *something* back: if the agent times
out, crashes, is paused, or returns an error, the dispatcher sends a fixed
message and emits `conversation.failed`. It never retries the model call —
a retry would double the spend on a turn that already failed, and the owner
would rather be told.
"""

from __future__ import annotations

import json
from typing import Any

from core import logging as jlog
from core.clock import SYSTEM_CLOCK, Clock
from core.contracts import envelope, validate
from core.events import put_event
from core.storage.base import StageResources
from core.storage.conversations import ConversationStore
from core.storage.state import StateStore

_log = jlog.get_logger("gate-dispatcher")

#: Long enough for a real turn, short enough to leave room in the Lambda's own
#: timeout to still send a reply.
INVOKE_TIMEOUT_SECONDS = 100

#: Held while a turn runs; blocks a heartbeat from starting alongside it.
LOCK_MINUTES = 30

FALLBACK = {
    "paused": (
        "I'm paused right now — the owner stopped me deliberately or something "
        "tripped a guardrail. They have been told."
    ),
    "timeout": "That took longer than I'm allowed and I had to stop. Try asking again.",
    "crash": "I broke while answering that. The owner has been alerted.",
    "model_error": "Something went wrong reaching my own model. Try me again in a moment.",
    "budget_exhausted": ("I've spent my model budget for today. I'll be back tomorrow."),
}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return Dispatcher().handle(event)


class Dispatcher:
    def __init__(
        self,
        *,
        resources: StageResources | None = None,
        state: StateStore | None = None,
        conversations: ConversationStore | None = None,
        telegram: Any = None,
        lambda_client: Any = None,
        clock: Clock = SYSTEM_CLOCK,
        agent_arn: str = "",
        chat_id: str = "",
    ) -> None:
        self._resources = resources
        self._state = state
        self._conversations = conversations
        self._telegram = telegram
        self._lambda = lambda_client
        self.clock = clock
        self._agent_arn = agent_arn
        self._chat_id = chat_id

    # ------------------------------------------------------------------ deps
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
    def conversations(self) -> ConversationStore:
        if self._conversations is None:
            self._conversations = ConversationStore(self.resources, clock=self.clock)
        return self._conversations

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
    def lambda_client(self) -> Any:
        if self._lambda is None:
            import boto3
            from botocore.config import Config

            self._lambda = boto3.client(
                "lambda",
                config=Config(
                    read_timeout=INVOKE_TIMEOUT_SECONDS,
                    connect_timeout=5,
                    retries={"max_attempts": 0},
                ),
            )
        return self._lambda

    @property
    def agent_arn(self) -> str:
        if not self._agent_arn:
            import os

            self._agent_arn = os.environ["CONVERSATION_ARN"]
        return self._agent_arn

    # ---------------------------------------------------------------- handle
    def handle(self, event: dict[str, Any]) -> dict[str, Any]:
        outcomes = []
        for record in event.get("Records", []):
            outcomes.append(self._one(json.loads(record["body"])))
        return {"processed": outcomes}

    def _one(self, message: dict[str, Any]) -> str:
        trace_id = str(message["trace_id"])
        jlog.set_trace_id(trace_id)
        conversation_id = str(message["conversation_id"])
        channel = str(message.get("channel", "telegram"))
        chat_id = str((message.get("telegram") or {}).get("chat_id", ""))

        self._open(conversation_id, chat_id)
        self.state.set_lock(conversation_id, minutes=LOCK_MINUTES, trace_id=trace_id)
        self.conversations.append(
            conversation_id, role="user", content=message["text"], trace_id=trace_id, seq=0
        )

        if self.state.pause_state().paused:
            return self._fail(conversation_id, channel, chat_id, trace_id, "paused")

        payload = {
            **envelope(self.resources.stage, trace_id, self.clock),
            "mode": "live",
            "channel": channel,
            "conversation_id": conversation_id,
            "message": {
                "message_id": str(message["message_id"]),
                "text": str(message["text"]),
                "received_at": str(message["received_at"]),
            },
            "budgets": self._budgets(),
        }
        validate("conversation-request", payload)

        try:
            response = self._invoke(payload)
        except Exception as exc:
            jlog.error(_log, "invoke_failed", error=str(exc))
            kind = "paused" if _is_throttle(exc) else "crash"
            return self._fail(conversation_id, channel, chat_id, trace_id, kind)

        if response.get("error"):
            kind = str(response["error"].get("type", "crash"))
            self._emit_failed(conversation_id, trace_id, kind, response["error"].get("message", ""))
            # The agent's own fallback reply is better than the dispatcher's,
            # because it knows what it was trying to do.
            reply = str(response.get("reply") or FALLBACK.get(kind, FALLBACK["crash"]))
        else:
            reply = str(response.get("reply", ""))

        self._deliver(conversation_id, channel, chat_id, reply, trace_id)
        self.state.set_lock(conversation_id, minutes=LOCK_MINUTES, trace_id=trace_id)
        if not response.get("error"):
            self._emit_completed(conversation_id, trace_id, response)
        return "replied"

    # --------------------------------------------------------------- helpers
    def _open(self, conversation_id: str, chat_id: str) -> None:
        from botocore.exceptions import ClientError

        try:
            self.conversations.open(conversation_id, chat_id=chat_id)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise

    def _budgets(self) -> dict[str, int]:
        from gates.model.policy import policy_for

        return {
            "notifications_remaining": self.state.remaining("notifications", 2),
            "tokens_remaining_today": self.state.remaining(
                "tokens#conversation", policy_for("conversation").daily_token_budget
            ),
        }

    def _invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.lambda_client.invoke(
            FunctionName=self.agent_arn,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload, default=str).encode("utf-8"),
        )
        body = json.loads(response["Payload"].read().decode("utf-8"))
        if response.get("FunctionError"):
            return {"error": {"type": "crash", "message": str(body)[:200]}, "reply": ""}
        return dict(body)

    def _deliver(
        self, conversation_id: str, channel: str, chat_id: str, reply: str, trace_id: str
    ) -> None:
        self.conversations.append(
            conversation_id, role="assistant", content=reply, trace_id=trace_id, seq=1
        )
        # The panel reads the reply from the store; sending it to Telegram would
        # be sending it to the wrong place (docs/02 §3.1).
        if channel == "telegram" and chat_id:
            self.telegram.send_message(chat_id, reply)

    def _fail(
        self, conversation_id: str, channel: str, chat_id: str, trace_id: str, kind: str
    ) -> str:
        reply = FALLBACK.get(kind, FALLBACK["crash"])
        self._deliver(conversation_id, channel, chat_id, reply, trace_id)
        self._emit_failed(conversation_id, trace_id, kind, "")
        return kind

    def _emit_failed(self, conversation_id: str, trace_id: str, kind: str, detail: str) -> None:
        self._emit(
            "conversation.failed",
            {
                "conversation_id": conversation_id,
                "error": {"type": kind, "message": detail[:200]},
            },
            trace_id,
        )

    def _emit_completed(
        self, conversation_id: str, trace_id: str, response: dict[str, Any]
    ) -> None:
        self._emit(
            "conversation.completed",
            {
                "conversation_id": conversation_id,
                "usage": response.get("usage", {}),
                "memory_ops": response.get("memory_ops", {}),
            },
            trace_id,
        )

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


def _is_throttle(exc: Exception) -> bool:
    """Reserved concurrency 0 is how a pause looks from the caller's side."""
    name = type(exc).__name__
    return "TooManyRequests" in name or "Throttl" in name or "throttl" in str(exc).lower()
