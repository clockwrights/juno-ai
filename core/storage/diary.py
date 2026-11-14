"""The diary: append-only, and the agent's only thread of self between wake-ups.

Append-only is enforced by a condition on the sort key rather than by
convention. There is no update and no delete path here at all — not for the
agent, not for the gates. A diary that can be rewritten is not a memory of what
happened, it is a story about it.
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

from core.clock import utc_now_iso
from core.storage.base import ConflictError, StoreBase


class DiaryStore(StoreBase):
    @property
    def table(self) -> str:
        return self.resources.table("agent_diary")

    @property
    def partition(self) -> str:
        return self.key("DIARY")

    def append(
        self,
        content: str,
        *,
        invocation_id: str,
        mode: str = "free",
        auto: bool = False,
        trace_id: str | None = None,
        artifacts_touched: list[str] | None = None,
        memory_ops: dict[str, int] | None = None,
        next_wake_wish_minutes: int | None = None,
    ) -> str:
        """Write one entry and return its sort key.

        `auto=True` marks the entry the handler's `finally` block writes when a
        cycle ends without the agent writing one itself (docs/02 §3.2).
        """
        if len(content.encode("utf-8")) > 8192:
            raise ValueError("a diary entry is at most 8 KiB")

        sk = f"{utc_now_iso(self.clock)}#{invocation_id}"
        item: dict[str, Any] = {
            "PK": {"S": self.partition},
            "SK": {"S": sk},
            "content": {"S": content},
            "mode": {"S": mode},
            "auto": {"BOOL": auto},
            "namespace": {"S": self.namespace},
        }
        if trace_id:
            item["trace_id"] = {"S": trace_id}
        if artifacts_touched:
            item["artifacts_touched"] = {"SS": sorted(set(artifacts_touched))}
        if memory_ops:
            item["memory_ops"] = {"M": {k: {"N": str(v)} for k, v in memory_ops.items()}}
        if next_wake_wish_minutes is not None:
            item["next_wake_wish_minutes"] = {"N": str(next_wake_wish_minutes)}

        try:
            self.dynamodb.put_item(
                TableName=self.table,
                Item=item,
                ConditionExpression="attribute_not_exists(SK)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise ConflictError(f"diary entry {sk} already exists") from exc
            raise
        return sk

    def last(self, n: int = 5) -> list[dict[str, str]]:
        """The `n` most recent entries, newest first."""
        response = self.dynamodb.query(
            TableName=self.table,
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": {"S": self.partition}},
            ScanIndexForward=False,
            Limit=n,
        )
        return [
            {
                "sk": item["SK"]["S"],
                "content": item["content"]["S"],
                "mode": item.get("mode", {}).get("S", "free"),
                "auto": str(item.get("auto", {}).get("BOOL", False)),
            }
            for item in response.get("Items", [])
        ]

    def between(self, start_sk: str, end_sk: str) -> list[dict[str, str]]:
        """Entries in a window, oldest first. Used by the weekly report."""
        response = self.dynamodb.query(
            TableName=self.table,
            KeyConditionExpression="PK = :pk AND SK BETWEEN :a AND :b",
            ExpressionAttributeValues={
                ":pk": {"S": self.partition},
                ":a": {"S": start_sk},
                ":b": {"S": end_sk},
            },
        )
        return [
            {"sk": i["SK"]["S"], "content": i["content"]["S"]} for i in response.get("Items", [])
        ]
