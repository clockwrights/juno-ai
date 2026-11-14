"""`agent_state`: flags, counters, locks, notes and the pause item.

Two things here carry the weight of the whole cost and safety model:

- **Counters are conditional updates.** `notifications < 2` and
  `tokens + n <= budget` are enforced by DynamoDB, not by the caller checking
  first. Two concurrent gates cannot both see room and both spend it.
- **Pause lives in its own partition.** `PAUSE#current`, not `STATE#pause`, so
  an IAM `LeadingKeys` condition can deny the agent roles outright (docs/09 §1).
  A sort-key-level split would not have been expressible in IAM.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import ClientError
from ulid import ULID

from core.clock import utc_date, utc_now_iso
from core.contracts import flag_defaults, parse_iso
from core.storage.base import StoreBase


class BudgetExhaustedError(RuntimeError):
    """A conditional counter update was refused: the budget is spent."""

    def __init__(self, counter: str, limit: int) -> None:
        super().__init__(f"{counter} is exhausted (limit {limit})")
        self.counter = counter
        self.limit = limit


@dataclass(frozen=True)
class Pause:
    paused: bool
    by: str = ""
    reason: str = ""
    at: str = ""


class StateStore(StoreBase):
    @property
    def table(self) -> str:
        return self.resources.table("agent_state")

    # ----------------------------------------------------------------- flags
    def flags(self) -> dict[str, bool]:
        """Read every flag, falling back to the contract's defaults.

        Read fresh on every invocation; there is deliberately no cache
        (docs/03 §6), so turning something off takes effect immediately.
        """
        defaults = flag_defaults()
        response = self.dynamodb.query(
            TableName=self.table,
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": {"S": self.key("FLAG")}},
        )
        for item in response.get("Items", []):
            name = item["SK"]["S"]
            if name in defaults:
                defaults[name] = bool(item.get("enabled", {}).get("BOOL", False))
        return defaults

    def flag(self, name: str) -> bool:
        return self.flags().get(name, False)

    def set_flag(self, name: str, enabled: bool, *, by: str) -> None:
        """Only `scripts/flags.py` reaches this; no Lambda role may write here."""
        self.dynamodb.put_item(
            TableName=self.table,
            Item={
                "PK": {"S": self.key("FLAG")},
                "SK": {"S": name},
                "enabled": {"BOOL": enabled},
                "set_by": {"S": by},
                "at": {"S": utc_now_iso(self.clock)},
            },
        )

    # ----------------------------------------------------------------- pause
    def pause_state(self) -> Pause:
        response = self.dynamodb.get_item(
            TableName=self.table,
            Key={"PK": {"S": self.key("PAUSE")}, "SK": {"S": "current"}},
        )
        item = response.get("Item")
        if not item:
            return Pause(paused=False)
        return Pause(
            paused=bool(item.get("paused", {}).get("BOOL", False)),
            by=item.get("by", {}).get("S", ""),
            reason=item.get("reason", {}).get("S", ""),
            at=item.get("at", {}).get("S", ""),
        )

    def set_pause(self, *, paused: bool, by: str, reason: str) -> None:
        self.dynamodb.put_item(
            TableName=self.table,
            Item={
                "PK": {"S": self.key("PAUSE")},
                "SK": {"S": "current"},
                "paused": {"BOOL": paused},
                "by": {"S": by},
                "reason": {"S": reason},
                "at": {"S": utc_now_iso(self.clock)},
            },
        )

    # -------------------------------------------------------------- counters
    def counters(self, date: str | None = None) -> dict[str, int]:
        date = date or utc_date(self.clock)
        response = self.dynamodb.get_item(
            TableName=self.table,
            Key={"PK": {"S": self.key("STATE")}, "SK": {"S": f"counters#{date}"}},
        )
        item = response.get("Item") or {}
        return {
            k: int(v["N"])
            for k, v in item.items()
            if isinstance(v, dict) and "N" in v and k not in ("ttl",)
        }

    def spend(self, counter: str, amount: int, limit: int, *, date: str | None = None) -> int:
        """Add to a counter only if it stays within `limit`.

        The condition is what makes this safe under concurrency: the check and
        the increment are one operation, so two callers cannot both pass.
        Returns the new total; raises `BudgetExhaustedError` if refused.
        """
        date = date or utc_date(self.clock)
        # 90-day TTL (docs/09 §6); counters are operational, not history.
        expires = int((self.clock.now() + dt.timedelta(days=90)).timestamp())
        try:
            response = self.dynamodb.update_item(
                TableName=self.table,
                Key={"PK": {"S": self.key("STATE")}, "SK": {"S": f"counters#{date}"}},
                UpdateExpression=("SET #c = if_not_exists(#c, :zero) + :amount, #ttl = :ttl"),
                ConditionExpression="attribute_not_exists(#c) OR #c <= :headroom",
                ExpressionAttributeNames={"#c": counter, "#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":amount": {"N": str(amount)},
                    ":zero": {"N": "0"},
                    ":headroom": {"N": str(limit - amount)},
                    ":ttl": {"N": str(expires)},
                },
                ReturnValues="UPDATED_NEW",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise BudgetExhaustedError(counter, limit) from exc
            raise
        return int(response["Attributes"][counter]["N"])

    def remaining(self, counter: str, limit: int, *, date: str | None = None) -> int:
        return max(0, limit - self.counters(date).get(counter, 0))

    # ----------------------------------------------------------------- locks
    def set_lock(self, chat_id: str, *, minutes: int, trace_id: str) -> None:
        expires = self.clock.now() + dt.timedelta(minutes=minutes)
        self.dynamodb.put_item(
            TableName=self.table,
            Item={
                "PK": {"S": self.key("STATE")},
                "SK": {"S": f"lock#{chat_id}"},
                "expires_at": {"S": expires.isoformat().replace("+00:00", "Z")},
                "trace_id": {"S": trace_id},
                "ttl": {"N": str(int(expires.timestamp()) + 3600)},
            },
        )

    def any_lock_live(self) -> bool:
        """Whether a conversation is in flight, which blocks a heartbeat."""
        response = self.dynamodb.query(
            TableName=self.table,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :lock)",
            ExpressionAttributeValues={
                ":pk": {"S": self.key("STATE")},
                ":lock": {"S": "lock#"},
            },
        )
        now = self.clock.now()
        for item in response.get("Items", []):
            expires = item.get("expires_at", {}).get("S")
            if expires and parse_iso(expires) > now:
                return True
        return False

    def clear_lock(self, chat_id: str) -> None:
        self.dynamodb.delete_item(
            TableName=self.table,
            Key={"PK": {"S": self.key("STATE")}, "SK": {"S": f"lock#{chat_id}"}},
        )

    # ----------------------------------------------------------------- notes
    def leave_note(self, text: str, *, source: str = "conversation") -> str:
        note_id = str(ULID())
        self.dynamodb.put_item(
            TableName=self.table,
            Item={
                "PK": {"S": self.key("NOTE")},
                "SK": {"S": note_id},
                "text": {"S": text},
                "created_at": {"S": utc_now_iso(self.clock)},
                "source": {"S": source},
            },
        )
        return note_id

    def pending_notes(self) -> list[dict[str, str]]:
        """Notes not yet delivered, oldest first."""
        response = self.dynamodb.query(
            TableName=self.table,
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": {"S": self.key("NOTE")}},
        )
        return [
            {
                "note_id": i["SK"]["S"],
                "text": i["text"]["S"],
                "created_at": i.get("created_at", {}).get("S", ""),
            }
            for i in response.get("Items", [])
            if "delivered_at" not in i
        ]

    def mark_delivered(self, note_ids: list[str]) -> None:
        now = utc_now_iso(self.clock)
        for note_id in note_ids:
            self.dynamodb.update_item(
                TableName=self.table,
                Key={"PK": {"S": self.key("NOTE")}, "SK": {"S": note_id}},
                UpdateExpression="SET delivered_at = :n",
                ExpressionAttributeValues={":n": {"S": now}},
            )

    # ------------------------------------------------------------- next wake
    def set_wake_wish(self, minutes: int, reason: str) -> None:
        """The agent's *wish*. `gate-governor` decides what actually happens."""
        wish_at = self.clock.now() + dt.timedelta(minutes=minutes)
        self.dynamodb.put_item(
            TableName=self.table,
            Item={
                "PK": {"S": self.key("STATE")},
                "SK": {"S": "next_wake"},
                "wish_at": {"S": wish_at.isoformat().replace("+00:00", "Z")},
                "wish_minutes": {"N": str(minutes)},
                "reason": {"S": reason},
                "written_at": {"S": utc_now_iso(self.clock)},
            },
        )

    def wake_wish(self) -> dict[str, Any] | None:
        response = self.dynamodb.get_item(
            TableName=self.table,
            Key={"PK": {"S": self.key("STATE")}, "SK": {"S": "next_wake"}},
        )
        item = response.get("Item")
        if not item:
            return None
        return {
            "wish_at": item.get("wish_at", {}).get("S", ""),
            "wish_minutes": int(item.get("wish_minutes", {"N": "0"})["N"]),
            "reason": item.get("reason", {}).get("S", ""),
        }
