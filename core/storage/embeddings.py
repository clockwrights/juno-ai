"""Turning text into vectors, through the gateway (ADR-29).

The agent role holds no provider key (ADR-03), so it cannot call the embedding
API directly. This invokes `gate-model`'s `embed` alias, which holds the key,
meters the tokens and chooses the model — the same shape as every other model
call in the system, for the same reason.

Chunking lives in `core/storage/memory.py`, where the memory being chunked is;
this module is only the client.
"""

from __future__ import annotations

import json
from typing import Any

from core import logging as jlog
from core.contracts import envelope, validate

_log = jlog.get_logger("core.embeddings")

#: `intfloat/multilingual-e5-large-instruct` (ADR-29). Asserted on every
#: response: a provider that silently changed dimension would corrupt the index
#: one vector at a time, and a loud failure is far cheaper to recover from.
EXPECTED_DIMENSION = 1024

#: The provider accepts a batch; the contract caps it at 64.
MAX_BATCH = 64


class EmbeddingError(RuntimeError):
    """The gateway could not produce embeddings."""


class GatewayEmbedder:
    """Callable that embeds one string, for `MemoryStore(embed=...)`."""

    def __init__(
        self,
        *,
        stage: str,
        gate_arn: str,
        lambda_client: Any | None = None,
        trace_id: str = "",
    ) -> None:
        self.stage = stage
        # The alias is the boundary: the agent role is granted this ARN and not
        # the bare function, so it cannot reach the chat budgets through it.
        self.gate_arn = gate_arn if ":embed" in gate_arn else f"{gate_arn}:embed"
        self._lambda = lambda_client
        self.trace_id = trace_id

    @property
    def lambda_client(self) -> Any:
        if self._lambda is None:
            import boto3
            from botocore.config import Config

            self._lambda = boto3.client(
                "lambda",
                config=Config(read_timeout=60, connect_timeout=5, retries={"max_attempts": 1}),
            )
        return self._lambda

    def __call__(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if len(texts) > MAX_BATCH:
            raise EmbeddingError(f"at most {MAX_BATCH} texts per call, got {len(texts)}")

        request = {
            **envelope(self.stage, self.trace_id or None),
            "role": "embed",
            "texts": list(texts),
        }
        validate("embed-request", request)

        response = self.lambda_client.invoke(
            FunctionName=self.gate_arn,
            InvocationType="RequestResponse",
            Payload=json.dumps(request).encode("utf-8"),
        )
        if response.get("FunctionError"):
            raise EmbeddingError(f"gate-model:embed raised: {response['FunctionError']}")

        payload = json.loads(response["Payload"].read().decode("utf-8"))
        if payload.get("error"):
            error = payload["error"]
            raise EmbeddingError(f"{error.get('type')}: {error.get('message')}")

        vectors = payload.get("embeddings") or []
        if len(vectors) != len(texts):
            raise EmbeddingError(f"asked for {len(texts)} embeddings, got {len(vectors)}")
        for vector in vectors:
            if len(vector) != EXPECTED_DIMENSION:
                # The index is fixed at 1024 dimensions (docs/09 §3). A vector
                # of another size would be rejected by S3 Vectors anyway, but
                # failing here names the cause.
                raise EmbeddingError(
                    f"expected {EXPECTED_DIMENSION} dimensions, got {len(vector)}; "
                    "the embedding model changed and the index must be rebuilt "
                    "(scripts/reindex.py)"
                )
        return [list(v) for v in vectors]
