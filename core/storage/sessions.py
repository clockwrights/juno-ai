"""Session records: what the agent actually thought and did (ADR-24).

Written by `gate-model` before a response is returned to the caller, so a
session cannot be hidden by agent code — the gateway is immutable and sees every
call. Deleted only by the admin role, from the control panel.

Under ADR-28 the record holds raw `reasoning_content` rather than a summary, so
it is as sensitive as message content and lives only in S3, where the owner
controls retention.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from core.clock import utc_date, utc_now_iso
from core.storage.base import StoreBase


@dataclass(frozen=True)
class SessionIndexEntry:
    trace_id: str
    kind: str
    role: str
    started_at: str
    calls: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    s3_prefix: str
    tool_names: tuple[str, ...] = ()
    outcome: str | None = None
    conversation_id: str | None = None


class SessionStore(StoreBase):
    """Write side used by `gate-model`; read and delete sides used by `ops/`."""

    @property
    def table(self) -> str:
        return self.resources.table("agent_sessions")

    def write_record(
        self,
        *,
        trace_id: str,
        seq: int,
        kind: str,
        role: str,
        model: str,
        request: dict[str, Any],
        response: dict[str, Any] | None,
        latency_ms: int,
        error: dict[str, Any] | None = None,
        conversation_id: str | None = None,
    ) -> str:
        """Write one call's record to S3 and upsert the index item.

        Both happen before the gateway returns. A failure here fails the model
        call (docs/06 §5.2 step 7): an unrecorded call is worse than no call.
        """
        date = utc_date(self.clock)
        now = utc_now_iso(self.clock)
        prefix = f"sessions/{date}/{trace_id}"
        key = f"{prefix}/{seq:04d}.json"

        record = {
            "schema_version": "1",
            "trace_id": trace_id,
            "stage": self.resources.stage,
            "issued_at": now,
            "seq": seq,
            "kind": kind,
            "role": role,
            "model": model,
            "requested_at": now,
            "latency_ms": latency_ms,
            "request": request,
            "response": response,
            "error": error,
        }
        body = json.dumps(record, default=str).encode("utf-8")
        self.s3.put_object(
            Bucket=self.resources.bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
        )

        usage = (response or {}).get("usage") or {}
        tools = [
            call.get("function", {}).get("name", "")
            for call in ((response or {}).get("tool_calls") or [])
        ]
        self._upsert_index(
            trace_id=trace_id,
            kind=kind,
            role=role,
            started_at=now,
            prefix=prefix,
            size_bytes=len(body),
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            cached_tokens=int((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)),
            tools=[t for t in tools if t],
            conversation_id=conversation_id,
        )
        return key

    def _upsert_index(
        self,
        *,
        trace_id: str,
        kind: str,
        role: str,
        started_at: str,
        prefix: str,
        size_bytes: int,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
        tools: list[str],
        conversation_id: str | None,
    ) -> None:
        """One index item per trace, accumulated across the calls in it."""
        date = utc_date(self.clock)
        expression = (
            "SET kind = :k, #r = :r, ended_at = :n, s3_prefix = :p, "
            "started_at = if_not_exists(started_at, :n) "
            "ADD calls :one, input_tokens :i, output_tokens :o, "
            "cached_tokens :c, size_bytes :b"
        )
        names = {"#r": "role"}
        values: dict[str, Any] = {
            ":k": {"S": kind},
            ":r": {"S": role},
            ":n": {"S": started_at},
            ":p": {"S": prefix},
            ":one": {"N": "1"},
            ":i": {"N": str(input_tokens)},
            ":o": {"N": str(output_tokens)},
            ":c": {"N": str(cached_tokens)},
            ":b": {"N": str(size_bytes)},
        }
        if tools:
            expression += ", tool_names :t"
            values[":t"] = {"SS": sorted(set(tools))}
        if conversation_id:
            expression += ", conversation_id = :cid"
            values[":cid"] = {"S": conversation_id}
            names["#cid"] = "conversation_id"

        self.dynamodb.update_item(
            TableName=self.table,
            Key={
                "PK": {"S": self.key(f"SESSION#{date}")},
                "SK": {"S": f"{started_at}#{trace_id}"},
            },
            UpdateExpression=expression,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    def set_outcome(self, trace_id: str, started_at: str, outcome: str) -> None:
        date = utc_date(self.clock)
        self.dynamodb.update_item(
            TableName=self.table,
            Key={
                "PK": {"S": self.key(f"SESSION#{date}")},
                "SK": {"S": f"{started_at}#{trace_id}"},
            },
            UpdateExpression="SET outcome = :o",
            ExpressionAttributeValues={":o": {"S": outcome}},
        )

    def by_date(self, date: str, limit: int = 100) -> list[SessionIndexEntry]:
        response = self.dynamodb.query(
            TableName=self.table,
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": {"S": self.key(f"SESSION#{date}")}},
            ScanIndexForward=False,
            Limit=limit,
        )
        return [self._to_entry(i) for i in response.get("Items", [])]

    def records(self, prefix: str) -> list[dict[str, Any]]:
        """Every call in one session, in order. Used by the panel."""
        listing = self.s3.list_objects_v2(Bucket=self.resources.bucket, Prefix=prefix)
        out = []
        for obj in sorted(listing.get("Contents", []), key=lambda o: o["Key"]):
            body = self.s3.get_object(Bucket=self.resources.bucket, Key=obj["Key"])["Body"]
            out.append(json.loads(body.read().decode("utf-8")))
        return out

    def delete_session(self, date: str, started_at: str, trace_id: str, prefix: str) -> int:
        """Remove a session's records and its index item. Admin only."""
        listing = self.s3.list_objects_v2(Bucket=self.resources.bucket, Prefix=prefix)
        keys = [{"Key": o["Key"]} for o in listing.get("Contents", [])]
        if keys:
            self.s3.delete_objects(
                Bucket=self.resources.bucket, Delete={"Objects": keys, "Quiet": True}
            )
        self.dynamodb.delete_item(
            TableName=self.table,
            Key={
                "PK": {"S": self.key(f"SESSION#{date}")},
                "SK": {"S": f"{started_at}#{trace_id}"},
            },
        )
        return len(keys)

    @staticmethod
    def _to_entry(item: dict[str, Any]) -> SessionIndexEntry:
        tools = item.get("tool_names", {})
        return SessionIndexEntry(
            trace_id=item["SK"]["S"].split("#", 1)[1],
            kind=item.get("kind", {}).get("S", ""),
            role=item.get("role", {}).get("S", ""),
            started_at=item.get("started_at", {}).get("S", ""),
            calls=int(item.get("calls", {"N": "0"})["N"]),
            input_tokens=int(item.get("input_tokens", {"N": "0"})["N"]),
            output_tokens=int(item.get("output_tokens", {"N": "0"})["N"]),
            cached_tokens=int(item.get("cached_tokens", {"N": "0"})["N"]),
            s3_prefix=item.get("s3_prefix", {}).get("S", ""),
            tool_names=tuple(tools.get("SS", [])) if "SS" in tools else (),
            outcome=item.get("outcome", {}).get("S"),
            conversation_id=item.get("conversation_id", {}).get("S"),
        )
