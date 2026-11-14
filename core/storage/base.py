"""Shared plumbing for the store classes.

Every store follows the same shape: it is constructed from a `StageResources`
description, it takes a `Clock`, and it never reaches for configuration itself.
That makes each one trivially testable against moto and means a store cannot
quietly depend on an environment variable that only exists in one Lambda.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import boto3

from core.clock import SYSTEM_CLOCK, Clock

LIVE = "live"
CANARY = "canary"
CANARY_PREFIX = "CANARY#"

#: Bodies larger than this go to S3 and the item keeps a pointer (docs/04 §1).
INLINE_BODY_LIMIT = 4096


class StorageError(RuntimeError):
    """A store operation failed in a way the caller must handle."""


class ConflictError(StorageError):
    """A conditional write lost a race (ETag mismatch, or the item exists)."""


@dataclass(frozen=True)
class StageResources:
    """Where a stage's data lives.

    Built from the environment inside a Lambda, and by hand in tests. Nothing
    below this reads `os.environ`.
    """

    stage: str
    bucket: str
    table_prefix: str
    vector_bucket: str = ""
    vector_index: str = "memory"

    @classmethod
    def from_env(cls) -> StageResources:
        stage = os.environ["STAGE"]
        return cls(
            stage=stage,
            bucket=os.environ["BUCKET_NAME"],
            table_prefix=f"{stage}-",
            vector_bucket=os.environ.get("VECTOR_BUCKET", f"{stage}-agent-vectors"),
            vector_index=os.environ.get("VECTOR_INDEX", "memory"),
        )

    def table(self, name: str) -> str:
        return f"{self.table_prefix}{name}"


def namespaced(key: str, namespace: str) -> str:
    """Prefix a partition key when running in canary mode (ADR-12).

    Canary runs share the real tables. The prefix is what keeps them separable
    by an IAM `LeadingKeys` condition, so it is applied in exactly one place.
    """
    if namespace == CANARY and not key.startswith(CANARY_PREFIX):
        return f"{CANARY_PREFIX}{key}"
    return key


def strip_namespace(key: str) -> str:
    return key.removeprefix(CANARY_PREFIX)


class StoreBase:
    """Common construction for every store."""

    def __init__(
        self,
        resources: StageResources,
        *,
        namespace: str = LIVE,
        clock: Clock = SYSTEM_CLOCK,
        dynamodb: Any | None = None,
        s3: Any | None = None,
    ) -> None:
        if namespace not in (LIVE, CANARY):
            raise ValueError(f"unknown namespace {namespace!r}")
        self.resources = resources
        self.namespace = namespace
        self.clock = clock
        self._dynamodb = dynamodb
        self._s3 = s3

    @property
    def dynamodb(self) -> Any:
        if self._dynamodb is None:
            self._dynamodb = boto3.client("dynamodb")
        return self._dynamodb

    @property
    def s3(self) -> Any:
        if self._s3 is None:
            self._s3 = boto3.client("s3")
        return self._s3

    @property
    def is_canary(self) -> bool:
        return self.namespace == CANARY

    def key(self, partition: str) -> str:
        return namespaced(partition, self.namespace)
