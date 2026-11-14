"""Read-only views over a stage, shared by `scripts/` and the panel (ADR-25).

Everything here is a plain function returning plain data, so the panel's routes
stay thin and the CLI can print the same thing the page renders. Nothing here
writes; the write side is `ops/actions.py`.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any

from ops.admin import AdminSession, Stage


@dataclass
class Overview:
    stage: str
    paused: bool
    pause_reason: str = ""
    paused_by: str = ""
    flags: dict[str, bool] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    last_diary: str = ""
    last_diary_at: str = ""
    open_conversations: int = 0
    recent_level_two: int = 0


def overview(session: AdminSession, stage: Stage) -> Overview:
    """The one page the owner looks at when they want to know it is fine."""
    ddb = session.client("dynamodb")
    state_table = stage.resource("agent_state")

    pause = _get(ddb, state_table, "PAUSE", "current")
    flags = {}
    for item in _query(ddb, state_table, "FLAG"):
        flags[item["SK"]["S"]] = bool(item.get("enabled", {}).get("BOOL", False))

    today = dt.datetime.now(tz=dt.UTC).strftime("%Y-%m-%d")
    counter_item = _get(ddb, state_table, "STATE", f"counters#{today}")
    counters = {
        k: int(v["N"])
        for k, v in counter_item.items()
        if isinstance(v, dict) and "N" in v and k != "ttl"
    }

    entries = diary(session, stage, limit=1)
    journal_entries = journal(session, stage, limit=50)

    return Overview(
        stage=stage.name,
        paused=bool(pause.get("paused", {}).get("BOOL", False)),
        pause_reason=pause.get("reason", {}).get("S", ""),
        paused_by=pause.get("by", {}).get("S", ""),
        flags=flags,
        counters=counters,
        last_diary=entries[0]["content"] if entries else "",
        last_diary_at=entries[0]["sk"] if entries else "",
        open_conversations=len(open_conversations(session, stage)),
        recent_level_two=sum(1 for e in journal_entries if e["level"] == 2),
    )


def diary(session: AdminSession, stage: Stage, *, limit: int = 20) -> list[dict[str, str]]:
    response = session.client("dynamodb").query(
        TableName=stage.resource("agent_diary"),
        KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": {"S": "DIARY"}},
        ScanIndexForward=False,
        Limit=limit,
    )
    return [
        {
            "sk": i["SK"]["S"],
            "content": i.get("content", {}).get("S", ""),
            "mode": i.get("mode", {}).get("S", "free"),
            "auto": "yes" if i.get("auto", {}).get("BOOL", False) else "",
        }
        for i in response.get("Items", [])
    ]


def journal(session: AdminSession, stage: Stage, *, limit: int = 20) -> list[dict[str, Any]]:
    response = session.client("dynamodb").query(
        TableName=stage.resource("supervisor_journal"),
        KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": {"S": "JOURNAL"}},
        ScanIndexForward=False,
        Limit=limit,
    )
    return [
        {
            "sk": i["SK"]["S"],
            "event_type": i.get("event_type", {}).get("S", ""),
            "level": int(i.get("level", {"N": "0"})["N"]),
            "action": i.get("action", {}).get("S", ""),
            "summary": i.get("summary", {}).get("S", ""),
            "probable_cause": i.get("probable_cause", {}).get("S", ""),
            "by": i.get("by", {}).get("S", "supervisor"),
            "preclassified": bool(i.get("preclassified", {}).get("BOOL", False)),
        }
        for i in response.get("Items", [])
    ]


def sessions(
    session: AdminSession, stage: Stage, *, date: str | None = None
) -> list[dict[str, Any]]:
    date = date or dt.datetime.now(tz=dt.UTC).strftime("%Y-%m-%d")
    response = session.client("dynamodb").query(
        TableName=stage.resource("agent_sessions"),
        KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": {"S": f"SESSION#{date}"}},
        ScanIndexForward=False,
        Limit=100,
    )
    return [
        {
            "trace_id": i["SK"]["S"].split("#", 1)[-1],
            "started_at": i.get("started_at", {}).get("S", ""),
            "kind": i.get("kind", {}).get("S", ""),
            "role": i.get("role", {}).get("S", ""),
            "calls": int(i.get("calls", {"N": "0"})["N"]),
            "input_tokens": int(i.get("input_tokens", {"N": "0"})["N"]),
            "output_tokens": int(i.get("output_tokens", {"N": "0"})["N"]),
            "cached_tokens": int(i.get("cached_tokens", {"N": "0"})["N"]),
            "s3_prefix": i.get("s3_prefix", {}).get("S", ""),
            "outcome": i.get("outcome", {}).get("S", ""),
        }
        for i in response.get("Items", [])
    ]


def session_records(session: AdminSession, stage: Stage, prefix: str) -> list[dict[str, Any]]:
    """Every model call in one session, in order — including its raw reasoning."""
    s3 = session.client("s3")
    listing = s3.list_objects_v2(Bucket=stage.bucket, Prefix=prefix)
    records = []
    for obj in sorted(listing.get("Contents", []), key=lambda o: o["Key"]):
        body = s3.get_object(Bucket=stage.bucket, Key=obj["Key"])["Body"].read()
        records.append(json.loads(body.decode("utf-8")))
    return records


def memories(
    session: AdminSession, stage: Stage, *, memory_type: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    ddb = session.client("dynamodb")
    if memory_type:
        response = ddb.query(
            TableName=stage.resource("agent_memory"),
            IndexName="type-last_accessed-index",
            KeyConditionExpression="#t = :t",
            ExpressionAttributeNames={"#t": "type"},
            ExpressionAttributeValues={":t": {"S": memory_type}},
            ScanIndexForward=False,
            Limit=limit,
        )
        keys = [i["PK"]["S"] for i in response.get("Items", [])]
        items = [_get(ddb, stage.resource("agent_memory"), k, "META") for k in keys]
    else:
        response = ddb.scan(TableName=stage.resource("agent_memory"), Limit=limit * 3)
        items = [i for i in response.get("Items", []) if i["SK"]["S"] == "META"]

    return [
        {
            "id": i["PK"]["S"],
            "type": i.get("type", {}).get("S", ""),
            "content": i.get("content", {}).get("S", "(stored in S3)"),
            "importance": int(i.get("importance", {"N": "0"})["N"]),
            "access_count": int(i.get("access_count", {"N": "0"})["N"]),
            "last_accessed": i.get("last_accessed", {}).get("S", ""),
            "tombstone": bool(i.get("tombstone", {}).get("BOOL", False)),
            "source": i.get("source", {}).get("S", ""),
        }
        for i in items
        if i
    ][:limit]


def open_conversations(session: AdminSession, stage: Stage) -> list[dict[str, str]]:
    try:
        response = session.client("dynamodb").query(
            TableName=stage.resource("agent_conversations"),
            IndexName="status-last_activity-index",
            KeyConditionExpression="#s = :open",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":open": {"S": "open"}},
        )
    except Exception:
        return []
    return [
        {
            "conversation_id": i["PK"]["S"].removeprefix("CONV#"),
            "last_activity_at": i.get("last_activity_at", {}).get("S", ""),
        }
        for i in response.get("Items", [])
    ]


def transcript(session: AdminSession, stage: Stage, conversation_id: str) -> list[dict[str, Any]]:
    response = session.client("dynamodb").query(
        TableName=stage.resource("agent_conversations"),
        KeyConditionExpression="PK = :pk AND begins_with(SK, :m)",
        ExpressionAttributeValues={
            ":pk": {"S": f"CONV#{conversation_id}"},
            ":m": {"S": "MSG#"},
        },
    )
    out = []
    for item in response.get("Items", []):
        raw = item.get("content", {}).get("S", '""')
        try:
            content = json.loads(raw)
        except json.JSONDecodeError:
            content = raw
        out.append({"role": item["role"]["S"], "content": content, "sk": item["SK"]["S"]})
    return out


def proposals(
    session: AdminSession, stage: Stage, *, status: str | None = None
) -> list[dict[str, Any]]:
    response = session.client("dynamodb").scan(
        TableName=stage.resource("agent_proposals"), Limit=200
    )
    out = []
    for item in response.get("Items", []):
        if item["SK"]["S"] != "META":
            continue
        record = {
            "proposal_id": item["PK"]["S"],
            "title": item.get("title", {}).get("S", ""),
            "status": item.get("status", {}).get("S", ""),
            "created_at": item.get("created_at", {}).get("S", ""),
            "pr_url": item.get("pr_url", {}).get("S", ""),
        }
        if status is None or record["status"] == status:
            out.append(record)
    return sorted(out, key=lambda p: p["created_at"], reverse=True)


def storage_sizes(session: AdminSession, stage: Stage) -> dict[str, Any]:
    """Roughly what the agent is keeping, for the Storage page."""
    sizes: dict[str, Any] = {"tables": {}, "prefixes": {}}
    ddb = session.client("dynamodb")
    for table in (
        "agent_memory",
        "agent_diary",
        "agent_conversations",
        "agent_sessions",
        "supervisor_journal",
    ):
        try:
            described = ddb.describe_table(TableName=stage.resource(table))["Table"]
            sizes["tables"][table] = {
                "items": described.get("ItemCount", 0),
                "bytes": described.get("TableSizeBytes", 0),
            }
        except Exception:
            sizes["tables"][table] = {"items": 0, "bytes": 0}

    s3 = session.client("s3")
    for prefix in ("memories/", "sessions/", "artifacts/", "drafts/", "conversations/"):
        count = 0
        total = 0
        try:
            for page in s3.get_paginator("list_objects_v2").paginate(
                Bucket=stage.bucket, Prefix=prefix
            ):
                for obj in page.get("Contents", []):
                    count += 1
                    total += obj["Size"]
        except Exception:
            pass
        sizes["prefixes"][prefix] = {"objects": count, "bytes": total}
    return sizes


def profile(session: AdminSession, stage: Stage) -> dict[str, str]:
    try:
        obj = session.client("s3").get_object(Bucket=stage.bucket, Key="profile/profile.md")
    except Exception:
        return {"content": "", "etag": ""}
    return {
        "content": obj["Body"].read().decode("utf-8"),
        "etag": str(obj["ETag"]).strip('"'),
    }


def _get(ddb: Any, table: str, pk: str, sk: str) -> dict[str, Any]:
    try:
        return dict(
            ddb.get_item(TableName=table, Key={"PK": {"S": pk}, "SK": {"S": sk}}).get("Item") or {}
        )
    except Exception:
        return {}


def _query(ddb: Any, table: str, pk: str) -> list[dict[str, Any]]:
    try:
        return list(
            ddb.query(
                TableName=table,
                KeyConditionExpression="PK = :pk",
                ExpressionAttributeValues={":pk": {"S": pk}},
            ).get("Items", [])
        )
    except Exception:
        return []
