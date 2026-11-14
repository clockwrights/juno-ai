"""The memory store (docs/04).

The agent decides what to remember; this decides nothing. What it does enforce
is the shape of remembering: bodies over 4 KiB go to S3, tags are written in the
same transaction as the record, forgetting is a tombstone rather than a delete,
and every update keeps the previous version.

Nothing here removes data. `memory_forget` sets `tombstone` and a reason; the
vector keeps its entry with `tombstone: true` so a purge script can find it
later. That asymmetry is deliberate — an agent that can delete its own history
cannot be audited.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from ulid import ULID

from core.clock import utc_now_iso
from core.contracts import parse_iso
from core.storage.base import (
    INLINE_BODY_LIMIT,
    ConflictError,
    StoreBase,
)
from core.storage.relevance import rank_score, relevance
from core.storage.vectors import VectorIndex

EMBEDDING_MODEL = "intfloat/multilingual-e5-large-instruct"
EMBEDDING_DIM = 1024

MEMORY_TYPES = frozenset(
    {"user_fact", "user_preference", "episode", "agent_learning", "project", "pending"}
)
SOURCES = frozenset({"conversation", "heartbeat", "maintenance", "canary"})


@dataclass
class Memory:
    """One memory record, as the tools see it."""

    id: str
    type: str
    content: str
    tags: list[str] = field(default_factory=list)
    importance: int = 3
    created_at: str = ""
    last_accessed: str = ""
    access_count: int = 0
    version: int = 1
    tombstone: bool = False
    source: str = "conversation"
    namespace: str = "live"
    consolidated_into: str | None = None
    forget_reason: str | None = None

    def score(self, now: dt.datetime, similarity: float = 0.0) -> float:
        return rank_score(similarity, self.relevance(now))

    def relevance(self, now: dt.datetime) -> float:
        return relevance(
            importance=self.importance,
            access_count=self.access_count,
            last_accessed=parse_iso(self.last_accessed or self.created_at),
            now=now,
        )


class MemoryStore(StoreBase):
    """Reads and writes `agent_memory`, its tag table, S3 bodies and vectors."""

    def __init__(self, *args: Any, index: VectorIndex, embed: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.index = index
        # `embed` is a callable rather than a client so the store does not care
        # whether embeddings come from the gateway, a fake, or a cassette.
        self.embed = embed

    # ------------------------------------------------------------------ keys
    @property
    def table(self) -> str:
        return self.resources.table("agent_memory")

    @property
    def tags_table(self) -> str:
        return self.resources.table("agent_memory_tags")

    def _pk(self, mem_id: str) -> str:
        return self.key(mem_id if mem_id.startswith("MEM#") else f"MEM#{mem_id}")

    # ----------------------------------------------------------------- write
    def save(
        self,
        *,
        content: str,
        type: str,
        tags: list[str] | None = None,
        importance: int = 3,
        source: str = "conversation",
    ) -> Memory:
        """Write a new memory, its tags and its vector."""
        if type not in MEMORY_TYPES:
            raise ValueError(f"unknown memory type {type!r}")
        if source not in SOURCES:
            raise ValueError(f"unknown source {source!r}")
        if not 1 <= importance <= 5:
            raise ValueError("importance must be 1..5")
        tags = sorted(set(tags or []))
        if len(tags) > 10:
            raise ValueError("at most 10 tags")

        now = utc_now_iso(self.clock)
        mem_id = f"MEM#{ULID()}"
        pk = self._pk(mem_id)
        # A canary run's writes are always sourced as canary, whatever the
        # caller passed: the namespace is the truth, not the argument.
        source = "canary" if self.is_canary else source

        item: dict[str, Any] = {
            "PK": {"S": pk},
            "SK": {"S": "META"},
            "type": {"S": type},
            "tags": {"SS": tags} if tags else {"NULL": True},
            "importance": {"N": str(importance)},
            "created_at": {"S": now},
            "last_accessed": {"S": now},
            "access_count": {"N": "0"},
            "version": {"N": "1"},
            "tombstone": {"BOOL": False},
            "source": {"S": source},
            "embedding_model": {"S": EMBEDDING_MODEL},
            "embedding_dim": {"N": str(EMBEDDING_DIM)},
            "namespace": {"S": self.namespace},
        }
        item.update(self._body_attributes(pk, content))

        # The record and its tag rows land together or not at all: a tag row
        # pointing at a memory that does not exist is a search result that
        # cannot be loaded.
        transact = [{"Put": {"TableName": self.table, "Item": item}}]
        transact += [
            {
                "Put": {
                    "TableName": self.tags_table,
                    "Item": {
                        "PK": {"S": self.key(f"TAG#{tag}")},
                        "SK": {"S": pk},
                        "namespace": {"S": self.namespace},
                        "created_at": {"S": now},
                    },
                }
            }
            for tag in tags
        ]
        self.dynamodb.transact_write_items(TransactItems=transact)

        self._index(pk, content, type=type, tags=tags, tombstone=False)

        return Memory(
            id=pk,
            type=type,
            content=content,
            tags=tags,
            importance=importance,
            created_at=now,
            last_accessed=now,
            source=source,
            namespace=self.namespace,
        )

    def update(self, mem_id: str, content: str) -> Memory:
        """Replace the content, keeping the previous version as `V#<n>`."""
        pk = self._pk(mem_id)
        current = self.get(pk)
        if current is None:
            raise ConflictError(f"{pk} does not exist")

        now = utc_now_iso(self.clock)
        snapshot: dict[str, Any] = {
            "PK": {"S": pk},
            "SK": {"S": f"V#{current.version}"},
            "content": {"S": current.content},
            "tags": {"SS": current.tags} if current.tags else {"NULL": True},
            "importance": {"N": str(current.importance)},
            "archived_at": {"S": now},
        }
        updates = {
            ":v": {"N": str(current.version + 1)},
            ":t": {"S": now},
        }
        body = self._body_attributes(pk, content)
        expression = "SET version = :v, last_accessed = :t"
        if "content" in body:
            expression += ", content = :c"
            updates[":c"] = body["content"]
            expression += " REMOVE body_s3_key"
        else:
            expression += ", body_s3_key = :b"
            updates[":b"] = body["body_s3_key"]
            expression += " REMOVE content"

        self.dynamodb.transact_write_items(
            TransactItems=[
                {"Put": {"TableName": self.table, "Item": snapshot}},
                {
                    "Update": {
                        "TableName": self.table,
                        "Key": {"PK": {"S": pk}, "SK": {"S": "META"}},
                        "UpdateExpression": expression,
                        "ExpressionAttributeValues": updates,
                        "ConditionExpression": "attribute_exists(PK)",
                    }
                },
            ]
        )
        self._index(pk, content, type=current.type, tags=current.tags, tombstone=False)
        updated = self.get(pk)
        assert updated is not None
        return updated

    def forget(self, mem_id: str, reason: str) -> None:
        """Tombstone a memory. Never a delete (docs/04 §2)."""
        pk = self._pk(mem_id)
        self.dynamodb.update_item(
            TableName=self.table,
            Key={"PK": {"S": pk}, "SK": {"S": "META"}},
            UpdateExpression="SET tombstone = :t, forget_reason = :r, last_accessed = :n",
            ExpressionAttributeValues={
                ":t": {"BOOL": True},
                ":r": {"S": reason},
                ":n": {"S": utc_now_iso(self.clock)},
            },
            ConditionExpression="attribute_exists(PK)",
        )
        # The vector stays, flagged, so purge_tombstones.py can find it later
        # and so a search cannot accidentally resurrect the content.
        current = self.get(pk)
        if current is not None:
            self._index(pk, current.content, type=current.type, tags=current.tags, tombstone=True)

    # ------------------------------------------------------------------ read
    def get(self, mem_id: str) -> Memory | None:
        pk = self._pk(mem_id)
        response = self.dynamodb.get_item(
            TableName=self.table, Key={"PK": {"S": pk}, "SK": {"S": "META"}}
        )
        item = response.get("Item")
        return self._to_memory(item) if item else None

    def history(self, mem_id: str) -> list[dict[str, Any]]:
        """Prior versions, oldest first."""
        pk = self._pk(mem_id)
        response = self.dynamodb.query(
            TableName=self.table,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :v)",
            ExpressionAttributeValues={":pk": {"S": pk}, ":v": {"S": "V#"}},
        )
        return sorted(
            (
                {
                    "version": int(i["SK"]["S"].removeprefix("V#")),
                    "content": i.get("content", {}).get("S", ""),
                    "archived_at": i.get("archived_at", {}).get("S", ""),
                }
                for i in response.get("Items", [])
            ),
            key=lambda v: v["version"],
        )

    def search(
        self,
        query: str,
        *,
        type: str | None = None,
        tags: list[str] | None = None,
        top_k: int = 8,
    ) -> list[Memory]:
        """Embed, query the index, rank by similarity and relevance, refresh."""
        vector = self.embed(query)
        vector_filter: dict[str, Any] = {"namespace": self.namespace, "tombstone": False}
        if type:
            vector_filter["type"] = type
        if tags:
            vector_filter["tags"] = list(tags)

        # Over-fetch: chunked bodies produce several vectors per memory, and
        # ranking happens after de-duplication.
        hits = self.index.query(vector, filter=vector_filter, k=top_k * 3)

        best: dict[str, float] = {}
        for hit in hits:
            mem_id = str(hit.metadata.get("mem_id") or hit.id.split("#chunk")[0])
            best[mem_id] = max(best.get(mem_id, -1.0), hit.score)

        now = self.clock.now()
        found: list[tuple[float, Memory]] = []
        for mem_id, similarity in best.items():
            memory = self.get(mem_id)
            if memory is None or memory.tombstone:
                continue
            found.append((memory.score(now, similarity), memory))

        found.sort(key=lambda pair: pair[0], reverse=True)
        results = [memory for _score, memory in found[:top_k]]
        self._touch(results)
        return results

    def list_by_type_including_tombstones(self, type: str, limit: int = 200) -> list[Memory]:
        """Every memory of a type, tombstones included.

        `list_by_type` hides tombstones because that is what a caller almost
        always wants. Consolidation is the exception: a tombstoned original is
        exactly what it needs to follow.
        """
        response = self.dynamodb.query(
            TableName=self.table,
            IndexName="type-last_accessed-index",
            KeyConditionExpression="#t = :t",
            ExpressionAttributeNames={"#t": "type"},
            ExpressionAttributeValues={":t": {"S": type}},
            ScanIndexForward=False,
            Limit=limit,
        )
        loaded = [self.get(i["PK"]["S"]) for i in response.get("Items", [])]
        return [m for m in loaded if m is not None]

    def list_by_type(self, type: str, limit: int = 50) -> list[Memory]:
        response = self.dynamodb.query(
            TableName=self.table,
            IndexName="type-last_accessed-index",
            KeyConditionExpression="#t = :t",
            ExpressionAttributeNames={"#t": "type"},
            ExpressionAttributeValues={":t": {"S": type}},
            ScanIndexForward=False,
            Limit=limit,
        )
        loaded = [self.get(i["PK"]["S"]) for i in response.get("Items", [])]
        return [m for m in loaded if m is not None and not m.tombstone]

    # --------------------------------------------------------------- helpers
    def _touch(self, memories: list[Memory]) -> None:
        """Refresh `last_accessed` and `access_count` — best effort (docs/04 §3).

        Search must not fail because a bookkeeping update did.
        """
        now = utc_now_iso(self.clock)
        for memory in memories:
            try:
                self.dynamodb.update_item(
                    TableName=self.table,
                    Key={"PK": {"S": memory.id}, "SK": {"S": "META"}},
                    UpdateExpression=("SET last_accessed = :n ADD access_count :one"),
                    ExpressionAttributeValues={":n": {"S": now}, ":one": {"N": "1"}},
                )
                memory.last_accessed = now
                memory.access_count += 1
            except Exception:
                continue

    def _body_attributes(self, pk: str, content: str) -> dict[str, Any]:
        """Inline small bodies; spill large ones to S3 (docs/04 §1)."""
        if len(content.encode("utf-8")) <= INLINE_BODY_LIMIT:
            return {"content": {"S": content}}
        key = f"memories/{pk.replace('#', '_')}.md"
        self.s3.put_object(
            Bucket=self.resources.bucket,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType="text/markdown",
        )
        return {"body_s3_key": {"S": key}}

    def _read_body(self, item: dict[str, Any]) -> str:
        if "content" in item:
            return str(item["content"].get("S", ""))
        key = item.get("body_s3_key", {}).get("S")
        if not key:
            return ""
        obj = self.s3.get_object(Bucket=self.resources.bucket, Key=key)
        return str(obj["Body"].read().decode("utf-8"))

    def _index(self, pk: str, content: str, *, type: str, tags: list[str], tombstone: bool) -> None:
        """Embed and upsert, chunking anything the model cannot take at once.

        The embedding model takes 514 tokens (ADR-29), so a long body becomes
        several vectors that all point at the same `mem_id`; search collapses
        them back with `max`.
        """
        for i, chunk in enumerate(chunk_text(content)):
            vector = self.embed(chunk)
            self.index.upsert(
                f"{pk}#chunk{i}" if i else pk,
                vector,
                {
                    "mem_id": pk,
                    "namespace": self.namespace,
                    "type": type,
                    "tags": tags,
                    "tombstone": tombstone,
                },
            )

    def _to_memory(self, item: dict[str, Any]) -> Memory:
        tags_attr = item.get("tags", {})
        return Memory(
            id=item["PK"]["S"],
            type=item["type"]["S"],
            content=self._read_body(item),
            tags=sorted(tags_attr.get("SS", [])) if "SS" in tags_attr else [],
            importance=int(item["importance"]["N"]),
            created_at=item["created_at"]["S"],
            last_accessed=item.get("last_accessed", item["created_at"])["S"],
            access_count=int(item.get("access_count", {"N": "0"})["N"]),
            version=int(item.get("version", {"N": "1"})["N"]),
            tombstone=bool(item.get("tombstone", {}).get("BOOL", False)),
            source=item.get("source", {}).get("S", "conversation"),
            namespace=item.get("namespace", {}).get("S", "live"),
            consolidated_into=item.get("consolidated_into", {}).get("S"),
            forget_reason=item.get("forget_reason", {}).get("S"),
        )


#: Roughly 514 tokens. Characters rather than tokens because the store must not
#: depend on a tokeniser it would have to keep in sync with the provider.
CHUNK_CHARS = 1600


def chunk_text(content: str, size: int = CHUNK_CHARS) -> list[str]:
    """Split on paragraph boundaries where possible, hard-split where not."""
    if len(content) <= size:
        return [content or ""]
    chunks: list[str] = []
    remaining = content
    while remaining:
        if len(remaining) <= size:
            chunks.append(remaining)
            break
        window = remaining[:size]
        split = window.rfind("\n\n")
        if split < size // 2:
            split = window.rfind(" ")
        if split < size // 2:
            split = size
        chunks.append(remaining[:split].strip())
        remaining = remaining[split:].strip()
    return [c for c in chunks if c]
