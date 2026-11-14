"""`gate-telegram` — the only public surface in the system (docs/06 §1).

A Lambda Function URL rather than API Gateway (ADR-22), which means this handler
is the entire authentication boundary. It does four things and nothing else:
check the webhook secret, check the sender is the owner, dedupe the update, and
enqueue. Every one of those is cheap, because Telegram retries anything that
does not answer within a second or two, and a retry storm on the agent's own
endpoint would be self-inflicted.

Nothing here reads message content beyond copying it onto a queue.
"""

from __future__ import annotations

import hmac
import json
import time
from typing import Any

from core import logging as jlog
from core.clock import SYSTEM_CLOCK, Clock
from core.contracts import envelope, new_trace_id, validate
from core.storage.base import StageResources

_log = jlog.get_logger("gate-telegram")

SECRET_HEADER = "x-telegram-bot-api-secret-token"

#: Telegram retries an update it thinks failed, and an update id can also arrive
#: twice during a webhook change. 24 hours of dedup keys is well past both.
DEDUP_TTL_SECONDS = 24 * 3600


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return Webhook().handle(event)


class Webhook:
    def __init__(
        self,
        *,
        resources: StageResources | None = None,
        secret: str = "",
        owner_chat_id: str = "",
        queue_url: str = "",
        sqs: Any = None,
        dynamodb: Any = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._resources = resources
        self._secret = secret
        self._owner_chat_id = owner_chat_id
        self._queue_url = queue_url
        self._sqs = sqs
        self._dynamodb = dynamodb
        self.clock = clock

    @property
    def resources(self) -> StageResources:
        if self._resources is None:
            self._resources = StageResources.from_env()
        return self._resources

    @property
    def secret(self) -> str:
        if not self._secret:
            from gates.common.parameters import get_parameter

            self._secret = get_parameter(f"/agent/{self.resources.stage}/webhook-secret")
        return self._secret

    @property
    def owner_chat_id(self) -> str:
        if not self._owner_chat_id:
            from gates.common.parameters import get_parameter

            self._owner_chat_id = get_parameter(f"/agent/{self.resources.stage}/owner-chat-id")
        return self._owner_chat_id

    @property
    def sqs(self) -> Any:
        if self._sqs is None:
            import boto3

            self._sqs = boto3.client("sqs")
        return self._sqs

    @property
    def dynamodb(self) -> Any:
        if self._dynamodb is None:
            import boto3

            self._dynamodb = boto3.client("dynamodb")
        return self._dynamodb

    @property
    def queue_url(self) -> str:
        if not self._queue_url:
            import os

            self._queue_url = os.environ["INBOUND_QUEUE_URL"]
        return self._queue_url

    def handle(self, event: dict[str, Any]) -> dict[str, Any]:
        headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

        # `compare_digest` rather than `==`: this is the one comparison in the
        # system an attacker can time.
        if not hmac.compare_digest(str(headers.get(SECRET_HEADER, "")), self.secret):
            jlog.warning(_log, "bad_webhook_secret")
            return _reply(403)

        try:
            body = json.loads(event.get("body") or "{}")
        except json.JSONDecodeError:
            return _reply(400)

        message = body.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        text = str(message.get("text", ""))
        update_id = str(body.get("update_id", ""))

        # Silently 200 rather than 403: telling a stranger their message was
        # rejected is more information than they need, and Telegram would retry
        # anything else.
        if chat_id != str(self.owner_chat_id):
            jlog.warning(_log, "not_the_owner", chat_id=_redact(chat_id))
            return _reply(200)

        if not text.strip():
            return _reply(200)

        if not self._claim(update_id):
            jlog.info(_log, "duplicate_update", update_id=update_id)
            return _reply(200)

        trace_id = new_trace_id()
        jlog.set_trace_id(trace_id)
        payload = {
            **envelope(self.resources.stage, trace_id, self.clock),
            "channel": "telegram",
            "conversation_id": f"tg:{chat_id}",
            "message_id": f"tg:{update_id}",
            "text": text,
            "received_at": self.clock.now().isoformat().replace("+00:00", "Z"),
            "telegram": {
                "chat_id": chat_id,
                "update_id": update_id,
                "message_id": int(message.get("message_id", 0)),
            },
        }
        validate("inbound-message", payload)

        self.sqs.send_message(
            QueueUrl=self.queue_url,
            MessageBody=json.dumps(payload, default=str),
            # One turn at a time per chat; dedup on the update id.
            MessageGroupId=payload["conversation_id"],
            MessageDeduplicationId=payload["message_id"],
        )
        jlog.info(_log, "enqueued", update_id=update_id)
        return _reply(200)

    def _claim(self, update_id: str) -> bool:
        """Record the update id, returning False if it was already there."""
        from botocore.exceptions import ClientError

        try:
            self.dynamodb.put_item(
                TableName=self.resources.table("agent_state"),
                Item={
                    "PK": {"S": "TG_UPDATE"},
                    "SK": {"S": update_id},
                    "ttl": {"N": str(int(time.time()) + DEDUP_TTL_SECONDS)},
                },
                ConditionExpression="attribute_not_exists(SK)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True


def _reply(status: int) -> dict[str, Any]:
    return {"statusCode": status, "body": ""}


def _redact(chat_id: str) -> str:
    return f"…{chat_id[-4:]}" if len(chat_id) > 4 else "…"
