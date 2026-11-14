"""The vector index behind an interface (ADR-15).

Two implementations satisfy the same behaviour suite: `S3VectorsIndex` in
production and `InMemoryIndex` in tests. Keeping the interface narrow —
upsert, query, delete — is what makes the ADR-15 fallback to a different
provider a change in one file rather than everywhere.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.storage.base import StageResources


@dataclass(frozen=True)
class VectorHit:
    id: str
    score: float
    metadata: dict[str, Any]


class VectorIndex(Protocol):
    def upsert(self, vector_id: str, vector: list[float], metadata: dict[str, Any]) -> None: ...

    def query(
        self, vector: list[float], filter: dict[str, Any] | None = None, k: int = 8
    ) -> list[VectorHit]: ...

    def delete(self, vector_id: str) -> None: ...


def _matches(metadata: dict[str, Any], filter: dict[str, Any] | None) -> bool:
    """Filter semantics shared by both implementations.

    A list value in the metadata means "contains"; a list in the filter means
    "any of". Both are needed because `tags` is a list on the item and a set of
    wanted values in the query.
    """
    if not filter:
        return True
    for key, wanted in filter.items():
        actual = metadata.get(key)
        if isinstance(actual, list):
            wanted_list = wanted if isinstance(wanted, list) else [wanted]
            if not set(wanted_list) & set(actual):
                return False
        elif isinstance(wanted, list):
            if actual not in wanted:
                return False
        elif actual != wanted:
            return False
    return True


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError(f"dimension mismatch: {len(a)} vs {len(b)}")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


@dataclass
class InMemoryIndex:
    """Test implementation with the same filter semantics as S3 Vectors."""

    dimension: int = 1024
    _vectors: dict[str, tuple[list[float], dict[str, Any]]] = field(default_factory=dict)

    def upsert(self, vector_id: str, vector: list[float], metadata: dict[str, Any]) -> None:
        if len(vector) != self.dimension:
            raise ValueError(f"expected {self.dimension} dimensions, got {len(vector)}")
        self._vectors[vector_id] = (list(vector), dict(metadata))

    def query(
        self, vector: list[float], filter: dict[str, Any] | None = None, k: int = 8
    ) -> list[VectorHit]:
        hits = [
            VectorHit(id=vid, score=cosine(vector, stored), metadata=meta)
            for vid, (stored, meta) in self._vectors.items()
            if _matches(meta, filter)
        ]
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]

    def delete(self, vector_id: str) -> None:
        self._vectors.pop(vector_id, None)

    def __len__(self) -> int:
        return len(self._vectors)

    def ids(self) -> list[str]:
        return sorted(self._vectors)


class S3VectorsIndex:
    """Production implementation over S3 Vectors."""

    def __init__(self, resources: StageResources, client: Any | None = None) -> None:
        self.resources = resources
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("s3vectors")
        return self._client

    def upsert(self, vector_id: str, vector: list[float], metadata: dict[str, Any]) -> None:
        self.client.put_vectors(
            vectorBucketName=self.resources.vector_bucket,
            indexName=self.resources.vector_index,
            vectors=[
                {
                    "key": vector_id,
                    "data": {"float32": [float(v) for v in vector]},
                    "metadata": metadata,
                }
            ],
        )

    def query(
        self, vector: list[float], filter: dict[str, Any] | None = None, k: int = 8
    ) -> list[VectorHit]:
        request: dict[str, Any] = {
            "vectorBucketName": self.resources.vector_bucket,
            "indexName": self.resources.vector_index,
            "queryVector": {"float32": [float(v) for v in vector]},
            "topK": k,
            "returnMetadata": True,
            "returnDistance": True,
        }
        if filter:
            request["filter"] = _to_s3vectors_filter(filter)
        response = self.client.query_vectors(**request)
        return [
            VectorHit(
                id=item["key"],
                # S3 Vectors returns cosine *distance*; the interface is a score
                # where higher is better, so the whole system ranks one way.
                score=1.0 - float(item.get("distance", 1.0)),
                metadata=item.get("metadata", {}),
            )
            for item in response.get("vectors", [])
        ]

    def delete(self, vector_id: str) -> None:
        self.client.delete_vectors(
            vectorBucketName=self.resources.vector_bucket,
            indexName=self.resources.vector_index,
            keys=[vector_id],
        )


def _to_s3vectors_filter(filter: dict[str, Any]) -> dict[str, Any]:
    """Translate the interface's filter dialect into the service's."""
    clauses: list[dict[str, Any]] = []
    for key, wanted in filter.items():
        if isinstance(wanted, list):
            clauses.append({key: {"$in": wanted}})
        else:
            clauses.append({key: {"$eq": wanted}})
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}
