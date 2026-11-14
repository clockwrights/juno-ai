"""moto-backed fixtures mirroring the real table and bucket shapes.

The definitions here must match `infra/stacks/storage.py`; `test_storage_shape.py`
asserts that they do, so a GSI added to the stack without being added here fails
rather than silently making the unit tests test the wrong thing.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from core.clock import FrozenClock
from core.storage.base import StageResources
from core.storage.vectors import InMemoryIndex

REGION = "eu-central-1"
STAGE = "test"
BUCKET = f"{STAGE}-agent-000000000000"

TABLES: dict[str, dict[str, Any]] = {
    "agent_memory": {
        "gsi": [
            ("type-last_accessed-index", "type", "last_accessed"),
            ("namespace-created_at-index", "namespace", "created_at"),
        ]
    },
    "agent_memory_tags": {},
    "agent_diary": {},
    "agent_state": {},
    "agent_conversations": {"gsi": [("status-last_activity-index", "status", "last_activity_at")]},
    "agent_proposals": {},
    "agent_sessions": {"gsi": [("conversation-index", "conversation_id", "started_at")]},
    "supervisor_journal": {"gsi": [("event_type-sk-index", "event_type", "SK")]},
}


@pytest.fixture(autouse=True)
def _aws_env() -> Iterator[None]:
    """moto refuses to run without credentials; make sure they are never real."""
    previous = {k: os.environ.get(k) for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")}
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = REGION
    yield
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture
def aws() -> Iterator[dict[str, Any]]:
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name=REGION)
        s3 = boto3.client("s3", region_name=REGION)

        for name, spec in TABLES.items():
            attrs = [
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ]
            gsis = []
            for index_name, pk, sk in spec.get("gsi", []):
                for attribute in (pk, sk):
                    if attribute not in [a["AttributeName"] for a in attrs]:
                        attrs.append({"AttributeName": attribute, "AttributeType": "S"})
                gsis.append(
                    {
                        "IndexName": index_name,
                        "KeySchema": [
                            {"AttributeName": pk, "KeyType": "HASH"},
                            {"AttributeName": sk, "KeyType": "RANGE"},
                        ],
                        "Projection": {"ProjectionType": "ALL"},
                    }
                )
            ddb.create_table(
                TableName=f"{STAGE}-{name}",
                KeySchema=[
                    {"AttributeName": "PK", "KeyType": "HASH"},
                    {"AttributeName": "SK", "KeyType": "RANGE"},
                ],
                AttributeDefinitions=attrs,
                BillingMode="PAY_PER_REQUEST",
                **({"GlobalSecondaryIndexes": gsis} if gsis else {}),
            )

        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        s3.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
        yield {"dynamodb": ddb, "s3": s3}


@pytest.fixture
def resources() -> StageResources:
    return StageResources(
        stage=STAGE,
        bucket=BUCKET,
        table_prefix=f"{STAGE}-",
        vector_bucket=f"{STAGE}-agent-vectors",
    )


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(dt.datetime(2026, 9, 6, 12, 0, tzinfo=dt.UTC))


@pytest.fixture
def index() -> InMemoryIndex:
    return InMemoryIndex(dimension=8)


@pytest.fixture
def embed() -> Any:
    """A deterministic 8-dimension embedding.

    Not a real model: it hashes tokens into buckets, so similar text lands near
    similar text and identical text is identical. That is all the store's
    ranking logic needs to be exercised.
    """

    def _embed(text: str) -> list[float]:
        vector = [0.0] * 8
        for word in text.lower().split():
            vector[hash(word) % 8] += 1.0
        if not any(vector):
            vector[0] = 1.0
        return vector

    return _embed
