"""`agent_conversations`: the working memory of an open chat.

Message bodies over 350 KiB spill to S3 with a pointer, because a DynamoDB item
is capped at 400 KiB and a long tool result should degrade to slower rather than
to lost.

Closing a conversation into an episode is deterministic housekeeping (ADR-16),
not something the agent chooses to do — so a conversation always becomes memory
even when the agent never thought to summarise it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from core.clock import utc_now_iso
from core.storage.base import StoreBase

#: Below DynamoDB's 400 KiB item cap with room for the rest of the item.
OVERFLOW_LIMIT = 350 * 1024


@dataclass(frozen=True)
class Message:
    role: str
    content: Any
    sk: str
    trace_id: str = ""
    usage: dict[str, int] | None = None


class ConversationStore(StoreBase):
    @property
    def table(self) -> str:
        return self.resources.table("agent_conversations")

    def _pk(self, conversation_id: str) -> str:
        return self.key(f"CONV#{conversation_id}")

    def open(self, conversation_id: str, *, chat_id: str = "") -> None:
        """Create the META item if this is a new conversation."""
        now = utc_now_iso(self.clock)
        self.dynamodb.put_item(
            TableName=self.table,
            Item={
                "PK": {"S": self._pk(conversation_id)},
                "SK": {"S": "META"},
                "status": {"S": "open"},
                "opened_at": {"S": now},
                "last_activity_at": {"S": now},
                "token_estimate": {"N": "0"},
                "chat_id": {"S": chat_id},
                "namespace": {"S": self.namespace},
            },
            ConditionExpression="attribute_not_exists(PK)",
        )

    def append(
        self,
        conversation_id: str,
        *,
        role: str,
        content: Any,
        trace_id: str = "",
        usage: dict[str, int] | None = None,
        seq: int = 0,
    ) -> str:
        now = utc_now_iso(self.clock)
        sk = f"MSG#{now}#{seq:04d}"
        encoded = json.dumps(content, default=str)

        item: dict[str, Any] = {
            "PK": {"S": self._pk(conversation_id)},
            "SK": {"S": sk},
            "role": {"S": role},
            "trace_id": {"S": trace_id},
            "namespace": {"S": self.namespace},
        }
        if len(encoded.encode("utf-8")) > OVERFLOW_LIMIT:
            key = f"conversations/{conversation_id}/{sk.replace('#', '_')}.json"
            self.s3.put_object(
                Bucket=self.resources.bucket,
                Key=key,
                Body=encoded.encode("utf-8"),
                ContentType="application/json",
            )
            item["body_s3_key"] = {"S": key}
        else:
            item["content"] = {"S": encoded}
        if usage:
            item["usage"] = {"M": {k: {"N": str(v)} for k, v in usage.items()}}

        self.dynamodb.put_item(TableName=self.table, Item=item)
        self._touch(conversation_id, now)
        return sk

    def transcript(self, conversation_id: str, limit: int = 200) -> list[Message]:
        response = self.dynamodb.query(
            TableName=self.table,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :m)",
            ExpressionAttributeValues={
                ":pk": {"S": self._pk(conversation_id)},
                ":m": {"S": "MSG#"},
            },
            Limit=limit,
        )
        return [self._to_message(item) for item in response.get("Items", [])]

    def meta(self, conversation_id: str) -> dict[str, Any] | None:
        response = self.dynamodb.get_item(
            TableName=self.table,
            Key={"PK": {"S": self._pk(conversation_id)}, "SK": {"S": "META"}},
        )
        item = response.get("Item")
        if not item:
            return None
        return {
            "status": item.get("status", {}).get("S", "open"),
            "opened_at": item.get("opened_at", {}).get("S", ""),
            "last_activity_at": item.get("last_activity_at", {}).get("S", ""),
            "chat_id": item.get("chat_id", {}).get("S", ""),
            "episode_id": item.get("episode_id", {}).get("S"),
        }

    def idle_conversations(self, older_than_iso: str) -> list[str]:
        """Open conversations whose last activity predates the threshold."""
        response = self.dynamodb.query(
            TableName=self.table,
            IndexName="status-last_activity-index",
            KeyConditionExpression="#s = :open AND last_activity_at < :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":open": {"S": "open"},
                ":t": {"S": older_than_iso},
            },
        )
        return [
            i["PK"]["S"].removeprefix("CANARY#").removeprefix("CONV#")
            for i in response.get("Items", [])
        ]

    def close(self, conversation_id: str, *, episode_id: str) -> None:
        self.dynamodb.update_item(
            TableName=self.table,
            Key={"PK": {"S": self._pk(conversation_id)}, "SK": {"S": "META"}},
            UpdateExpression="SET #s = :closed, episode_id = :e",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":closed": {"S": "closed"},
                ":e": {"S": episode_id},
            },
        )

    def _touch(self, conversation_id: str, now: str) -> None:
        self.dynamodb.update_item(
            TableName=self.table,
            Key={"PK": {"S": self._pk(conversation_id)}, "SK": {"S": "META"}},
            UpdateExpression="SET last_activity_at = :n",
            ExpressionAttributeValues={":n": {"S": now}},
        )

    def _to_message(self, item: dict[str, Any]) -> Message:
        if "content" in item:
            raw = item["content"]["S"]
        else:
            key = item["body_s3_key"]["S"]
            body = self.s3.get_object(Bucket=self.resources.bucket, Key=key)["Body"]
            raw = body.read().decode("utf-8")
        usage = None
        if "usage" in item:
            usage = {k: int(v["N"]) for k, v in item["usage"]["M"].items()}
        return Message(
            role=item["role"]["S"],
            content=json.loads(raw),
            sk=item["SK"]["S"],
            trace_id=item.get("trace_id", {}).get("S", ""),
            usage=usage,
        )
