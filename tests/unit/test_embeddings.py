"""The embeddings path (ADR-29).

Memory does not work without this, and it is the one model call the agent makes
that carries no thinking — so it is metered like everything else but never
recorded as a session.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from core.contracts import envelope, validate
from core.storage.base import StageResources
from core.storage.embeddings import EXPECTED_DIMENSION, EmbeddingError, GatewayEmbedder
from core.storage.sessions import SessionStore
from core.storage.state import StateStore
from gates.model.handler import Gateway
from gates.model.policy import EMBEDDING_MODEL, policy_for, role_allowed_on_alias
from tests.unit.conftest import STAGE


class FakeContext:
    def __init__(self, alias: str = "embed") -> None:
        self.invoked_function_arn = (
            f"arn:aws:lambda:eu-central-1:000000000000:function:{STAGE}-gate-model:{alias}"
        )


class FakeProvider:
    def __init__(self, dimension: int = EXPECTED_DIMENSION) -> None:
        self.dimension = dimension
        self.calls: list[list[str]] = []
        self.raises: Exception | None = None

    def build_request(self, **kwargs: Any) -> dict[str, Any]:
        return {}

    def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("an embed call must not reach the chat path")

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.raises:
            raise self.raises
        return [[0.01] * self.dimension for _ in texts]


@pytest.fixture
def gateway(aws: dict[str, Any], resources: StageResources, clock: Any) -> Gateway:
    return Gateway(
        resources=resources,
        provider=FakeProvider(),
        state=StateStore(resources, clock=clock, dynamodb=aws["dynamodb"], s3=aws["s3"]),
        sessions=SessionStore(resources, clock=clock, dynamodb=aws["dynamodb"], s3=aws["s3"]),
        clock=clock,
    )


def embed_request(*texts: str, **overrides: Any) -> dict[str, Any]:
    payload = {**envelope(STAGE, "trace-1"), "role": "embed", "texts": list(texts) or ["hello"]}
    payload.update(overrides)
    return payload


# ------------------------------------------------------------------- policy


def test_the_embed_role_uses_the_embedding_model() -> None:
    assert policy_for("embed").model == EMBEDDING_MODEL


def test_embed_has_its_own_alias() -> None:
    """So the agent can be granted embeddings without the chat budgets."""
    assert role_allowed_on_alias("embed", "embed")
    assert not role_allowed_on_alias("embed", "agent")
    assert not role_allowed_on_alias("conversation", "embed")


# ------------------------------------------------------------------ gateway


def test_an_embed_call_returns_vectors(gateway: Gateway) -> None:
    response = gateway.handle(embed_request("the owner lives in Springfield"), FakeContext())
    assert response.get("error") is None
    assert len(response["embeddings"]) == 1
    assert response["dimension"] == EXPECTED_DIMENSION
    assert response["model"] == EMBEDDING_MODEL
    validate("embed-response", response)


def test_a_batch_round_trips(gateway: Gateway) -> None:
    response = gateway.handle(embed_request("one", "two", "three"), FakeContext())
    assert len(response["embeddings"]) == 3


def test_an_embed_call_on_the_agent_alias_is_refused(gateway: Gateway) -> None:
    response = gateway.handle(embed_request(), FakeContext(alias="agent"))
    assert response["error"]["type"] == "role_not_allowed"


def test_a_chat_role_on_the_embed_alias_is_refused(gateway: Gateway) -> None:
    chat = {
        **envelope(STAGE, "t"),
        "role": "conversation",
        "system": [{"text": "x", "stable": True}],
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 100,
    }
    assert gateway.handle(chat, FakeContext(alias="embed"))["error"]["type"] == "role_not_allowed"


def test_an_embed_request_missing_texts_is_refused(gateway: Gateway) -> None:
    payload = embed_request()
    del payload["texts"]
    assert gateway.handle(payload, FakeContext())["error"]["type"] == "invalid_request"


def test_the_caller_may_not_choose_the_embedding_model(gateway: Gateway) -> None:
    response = gateway.handle(embed_request(model="something-else"), FakeContext())
    assert response["error"]["type"] == "invalid_request"


def test_embeddings_are_metered(gateway: Gateway, resources: Any, clock: Any, aws: Any) -> None:
    gateway.handle(embed_request("some text to embed"), FakeContext())
    state = StateStore(resources, clock=clock, dynamodb=aws["dynamodb"], s3=aws["s3"])
    assert state.counters()["tokens#embed"] > 0


def test_an_exhausted_embed_budget_makes_no_call(
    gateway: Gateway, resources: Any, clock: Any, aws: Any
) -> None:
    """A runaway re-index is the failure this budget exists for."""
    state = StateStore(resources, clock=clock, dynamodb=aws["dynamodb"], s3=aws["s3"])
    state.spend(
        "tokens#embed",
        policy_for("embed").daily_token_budget,
        policy_for("embed").daily_token_budget,
    )
    response = gateway.handle(embed_request(), FakeContext())
    assert response["error"]["type"] == "budget_exhausted"
    assert gateway.provider.calls == []  # type: ignore[attr-defined]


def test_an_embed_call_writes_no_session_record(
    gateway: Gateway, resources: Any, clock: Any, aws: Any
) -> None:
    """Sessions exist so the owner can read what the agent thought (ADR-24).
    An embedding has no thinking in it; recording one per memory save would
    bury the readable sessions."""
    gateway.handle(embed_request(), FakeContext())
    sessions = SessionStore(resources, clock=clock, dynamodb=aws["dynamodb"], s3=aws["s3"])
    assert sessions.by_date("2026-09-06") == []


def test_an_upstream_failure_is_returned_not_raised(gateway: Gateway) -> None:
    gateway.provider.raises = RuntimeError("provider down")  # type: ignore[attr-defined]
    assert gateway.handle(embed_request(), FakeContext())["error"]["type"] == "upstream_error"


# ------------------------------------------------------------------- client


class FakeLambda:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.invocations: list[dict[str, Any]] = []
        self.function_error: str | None = None

    def invoke(self, **kwargs: Any) -> dict[str, Any]:
        self.invocations.append(kwargs)
        out: dict[str, Any] = {"Payload": io.BytesIO(json.dumps(self.payload).encode("utf-8"))}
        if self.function_error:
            out["FunctionError"] = self.function_error
        return out


def embedder(
    payload: dict[str, Any], arn: str = "arn:aws:lambda:r:a:function:test-gate-model"
) -> tuple[GatewayEmbedder, FakeLambda]:
    fake = FakeLambda(payload)
    return GatewayEmbedder(stage=STAGE, gate_arn=arn, lambda_client=fake), fake


def ok_payload(n: int = 1, dimension: int = EXPECTED_DIMENSION) -> dict[str, Any]:
    return {"schema_version": "1", "trace_id": "t", "embeddings": [[0.1] * dimension] * n}


def test_the_client_calls_the_embed_alias() -> None:
    """The alias is the boundary: the bare function ARN would reach chat budgets."""
    client, fake = embedder(ok_payload())
    client("hello")
    assert fake.invocations[0]["FunctionName"].endswith(":embed")


def test_an_arn_that_already_names_the_alias_is_left_alone() -> None:
    client, fake = embedder(ok_payload(), arn="arn:aws:lambda:r:a:function:test-gate-model:embed")
    client("hello")
    assert fake.invocations[0]["FunctionName"].count(":embed") == 1


def test_the_client_returns_one_vector_per_text() -> None:
    client, _ = embedder(ok_payload(n=3))
    assert len(client.embed_batch(["a", "b", "c"])) == 3


def test_a_gateway_error_becomes_an_embedding_error() -> None:
    client, _ = embedder({"error": {"type": "budget_exhausted", "message": "spent"}})
    with pytest.raises(EmbeddingError, match="budget_exhausted"):
        client("hello")


def test_a_crashed_gateway_becomes_an_embedding_error() -> None:
    client, fake = embedder(ok_payload())
    fake.function_error = "Unhandled"
    with pytest.raises(EmbeddingError, match="raised"):
        client("hello")


def test_a_wrong_count_is_refused() -> None:
    client, _ = embedder(ok_payload(n=1))
    with pytest.raises(EmbeddingError, match="asked for 2"):
        client.embed_batch(["a", "b"])


def test_a_wrong_dimension_is_refused_loudly() -> None:
    """A silently changed dimension would corrupt the index one vector at a time."""
    client, _ = embedder(ok_payload(dimension=768))
    with pytest.raises(EmbeddingError, match="reindex"):
        client("hello")


def test_an_oversized_batch_is_refused() -> None:
    client, _ = embedder(ok_payload())
    with pytest.raises(EmbeddingError, match="at most"):
        client.embed_batch(["x"] * 65)


def test_an_empty_batch_makes_no_call() -> None:
    client, fake = embedder(ok_payload())
    assert client.embed_batch([]) == []
    assert fake.invocations == []


def test_the_request_matches_the_contract() -> None:
    client, fake = embedder(ok_payload())
    client("hello")
    validate("embed-request", json.loads(fake.invocations[0]["Payload"]))
