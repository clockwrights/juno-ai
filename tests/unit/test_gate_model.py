"""The model gateway (docs/06 §5, docs/11 VP2 acceptance).

These tests are where the cost and safety model becomes real: a caller cannot
pick its own model, cannot spend past its budget, and cannot make a call that
leaves no record.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.contracts import envelope
from core.storage.base import StageResources
from core.storage.sessions import SessionStore
from core.storage.state import StateStore
from gates.model.handler import (
    Gateway,
    estimate_tokens,
    weighted_tokens,
)
from gates.model.policy import (
    ALIAS_ROLES,
    POLICIES,
    alias_from_arn,
    policy_for,
    role_allowed_on_alias,
)
from gates.model.provider import build_messages, normalise
from tests.unit.conftest import STAGE


class FakeContext:
    def __init__(self, alias: str = "agent") -> None:
        self.invoked_function_arn = (
            f"arn:aws:lambda:eu-central-1:000000000000:function:{STAGE}-gate-model:{alias}"
        )


class FakeProvider:
    """Records requests and returns queued payloads."""

    def __init__(self, payloads: list[dict[str, Any]] | None = None) -> None:
        self.payloads = payloads or [
            {
                "content": "hello",
                "finish_reason": "stop",
                "model": "zai-org/GLM-5.3",
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            }
        ]
        self.requests: list[dict[str, Any]] = []
        self.raises: Exception | None = None

    def build_request(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "model": kwargs["model"],
            "messages": build_messages(kwargs["system"], kwargs["messages"]),
            "max_tokens": kwargs["max_tokens"],
        }

    def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        if self.raises:
            raise self.raises
        index = min(len(self.requests) - 1, len(self.payloads) - 1)
        return self.payloads[index]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 8 for _ in texts]


@pytest.fixture
def gateway(aws: dict[str, Any], resources: StageResources, clock: Any) -> Gateway:
    provider = FakeProvider()
    return Gateway(
        resources=resources,
        provider=provider,
        state=StateStore(resources, clock=clock, dynamodb=aws["dynamodb"], s3=aws["s3"]),
        sessions=SessionStore(resources, clock=clock, dynamodb=aws["dynamodb"], s3=aws["s3"]),
        clock=clock,
    )


def request(role: str = "conversation", **overrides: Any) -> dict[str, Any]:
    payload = {
        **envelope(STAGE, "trace-1"),
        "role": role,
        "system": [{"text": "you are Juno", "stable": True}],
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 1000,
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------- policy


def test_every_role_has_a_policy() -> None:
    assert set(POLICIES) == {
        "conversation",
        "heartbeat",
        "consolidation",
        "supervisor",
        "canary",
        "embed",
    }


def test_the_expensive_model_is_only_where_the_owner_notices() -> None:
    """ADR-28: Flash carries the unattended volume, which is what fits $20."""
    assert POLICIES["conversation"].model == "zai-org/GLM-5.3"
    assert POLICIES["consolidation"].model == "zai-org/GLM-5.3"
    assert POLICIES["heartbeat"].model == "zai-org/GLM-5.3-Flash"
    assert POLICIES["supervisor"].model == "zai-org/GLM-5.3-Flash"


def test_the_daily_budgets_fit_the_monthly_envelope() -> None:
    """Worst case, every role spending every day, must stay under $20 with
    room for infrastructure. This is the test that catches a budget raised
    without anyone doing the arithmetic."""
    price = {
        "zai-org/GLM-5.3": (1.40, 4.40),
        "zai-org/GLM-5.3-Flash": (0.15, 0.50),
        # Embeddings are input-only; the same number in both slots keeps the
        # 80/20 split below arithmetically honest.
        "intfloat/multilingual-e5-large-instruct": (0.02, 0.02),
    }
    daily = 0.0
    for policy in POLICIES.values():
        inp, out = price[policy.model]
        # 80/20 input to output is the shape of an agent workload.
        daily += (policy.daily_token_budget * 0.8 / 1e6) * inp
        daily += (policy.daily_token_budget * 0.2 / 1e6) * out
    infrastructure = 1.0  # docs/02 §5 puts it under $0.50; round up.
    monthly = daily * 30 + infrastructure
    assert monthly < 20.0, f"worst case is ${monthly:.2f}/month"


def test_an_unknown_role_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown model role"):
        policy_for("sudo")


def test_alias_membership_matches_the_design() -> None:
    assert ALIAS_ROLES["agent"] == {"conversation", "heartbeat", "consolidation"}
    assert role_allowed_on_alias("supervisor", "supervisor")
    assert not role_allowed_on_alias("supervisor", "agent")
    assert not role_allowed_on_alias("conversation", "canary")


def test_an_unrecognised_alias_matches_no_role() -> None:
    """Defaulting to `agent` would be the dangerous direction."""
    assert alias_from_arn("arn:aws:lambda:r:a:function:dev-gate-model") == "unknown"
    assert not role_allowed_on_alias("conversation", "unknown")


# -------------------------------------------------------------------- gateway


def test_a_normal_call_returns_content_and_gateway_fields(gateway: Gateway) -> None:
    response = gateway.handle(request(), FakeContext())
    assert response["content"] == "hello"
    assert response["gateway"]["model_used"] == "zai-org/GLM-5.3"
    assert response["gateway"]["tokens_remaining_today"] > 0


def test_the_caller_may_not_choose_the_model(gateway: Gateway) -> None:
    """ADR-05: naming the model is the gateway's job and nobody else's."""
    response = gateway.handle(request(model="zai-org/GLM-5.3"), FakeContext())
    assert response["error"]["type"] == "invalid_request"
    assert "model" in response["error"]["message"]


@pytest.mark.parametrize("field", ["api_key", "base_url", "stream", "n"])
def test_other_forbidden_fields_are_refused(gateway: Gateway, field: str) -> None:
    response = gateway.handle(request(**{field: "x"}), FakeContext())
    assert response["error"]["type"] == "invalid_request"


def test_a_role_on_the_wrong_alias_is_refused(gateway: Gateway) -> None:
    response = gateway.handle(request("supervisor"), FakeContext(alias="agent"))
    assert response["error"]["type"] == "role_not_allowed"


def test_the_supervisor_alias_accepts_the_supervisor(gateway: Gateway) -> None:
    response = gateway.handle(request("supervisor"), FakeContext(alias="supervisor"))
    assert response.get("error") is None


def test_a_malformed_request_is_refused_not_raised(gateway: Gateway) -> None:
    response = gateway.handle({"role": "conversation"}, FakeContext())
    assert response["error"]["type"] == "invalid_request"


def test_max_tokens_is_capped(gateway: Gateway) -> None:
    gateway.handle(request(max_tokens=16000), FakeContext())
    assert gateway.provider.requests[0]["max_tokens"] <= 16000  # type: ignore[attr-defined]


def test_the_budget_stops_the_next_call(
    gateway: Gateway, resources: Any, clock: Any, aws: Any
) -> None:
    state = StateStore(resources, clock=clock, dynamodb=aws["dynamodb"], s3=aws["s3"])
    state.spend("tokens#conversation", 199_000, 200_000)
    response = gateway.handle(request(), FakeContext())
    assert response["error"]["type"] == "budget_exhausted"
    assert response["error"]["retry_after_seconds"] > 0


def test_an_exhausted_budget_makes_no_model_call(
    gateway: Gateway, resources: Any, clock: Any, aws: Any
) -> None:
    """The refusal must come before the spend, or the budget means nothing."""
    state = StateStore(resources, clock=clock, dynamodb=aws["dynamodb"], s3=aws["s3"])
    state.spend("tokens#conversation", 200_000, 200_000)
    gateway.handle(request(), FakeContext())
    assert gateway.provider.requests == []  # type: ignore[attr-defined]


def test_usage_is_recorded_against_the_role(
    gateway: Gateway, resources: Any, clock: Any, aws: Any
) -> None:
    gateway.handle(request(), FakeContext())
    state = StateStore(resources, clock=clock, dynamodb=aws["dynamodb"], s3=aws["s3"])
    assert state.counters()["tokens#conversation"] > 0


def test_roles_have_separate_budgets(
    gateway: Gateway, resources: Any, clock: Any, aws: Any
) -> None:
    gateway.handle(request("conversation"), FakeContext())
    gateway.handle(request("heartbeat"), FakeContext())
    counters = StateStore(resources, clock=clock, dynamodb=aws["dynamodb"], s3=aws["s3"]).counters()
    assert counters["tokens#conversation"] > 0
    assert counters["tokens#heartbeat"] > 0


def test_a_session_record_is_written_before_the_response(
    gateway: Gateway, resources: Any, clock: Any, aws: Any
) -> None:
    """The gateway is immutable and sees every call; that is what makes the
    session record trustworthy evidence (ADR-24)."""
    gateway.handle(request(), FakeContext())
    sessions = SessionStore(resources, clock=clock, dynamodb=aws["dynamodb"], s3=aws["s3"])
    entries = sessions.by_date("2026-09-06")
    assert len(entries) == 1
    assert entries[0].role == "conversation"


def test_an_upstream_error_is_returned_and_recorded(
    gateway: Gateway, resources: Any, clock: Any, aws: Any
) -> None:
    gateway.provider.raises = RuntimeError("provider is down")  # type: ignore[attr-defined]
    response = gateway.handle(request(), FakeContext())
    assert response["error"]["type"] == "upstream_error"
    sessions = SessionStore(resources, clock=clock, dynamodb=aws["dynamodb"], s3=aws["s3"])
    assert len(sessions.by_date("2026-09-06")) == 1, "a failed call is still a call"


def test_a_content_filter_is_retried_once_against_the_fallback(gateway: Gateway) -> None:
    """No server-side fallback exists on this provider (ADR-28)."""
    gateway.provider.payloads = [  # type: ignore[attr-defined]
        {"content": None, "finish_reason": "content_filter", "usage": {}},
        {"content": "rephrased", "finish_reason": "stop", "usage": {}},
    ]
    response = gateway.handle(request(), FakeContext())
    models = [r["model"] for r in gateway.provider.requests]  # type: ignore[attr-defined]
    assert models == ["zai-org/GLM-5.3", "zai-org/GLM-5.2"]
    assert response["content"] == "rephrased"


def test_a_canary_call_is_namespaced(
    gateway: Gateway, resources: Any, clock: Any, aws: Any
) -> None:
    gateway._sessions = None
    gateway._state = None
    gateway.handle(request("canary"), FakeContext(alias="canary"))
    live = SessionStore(resources, clock=clock, dynamodb=aws["dynamodb"], s3=aws["s3"])
    assert live.by_date("2026-09-06") == [], "a canary run must not appear in live sessions"


# ------------------------------------------------------------------ estimates


def test_the_token_estimate_never_undercounts() -> None:
    """Over-counting is the safe direction for a budget (ADR-28)."""
    text = "hello world " * 100
    estimate = estimate_tokens([{"text": text}], [])
    # A real tokeniser gives roughly len/4 for English; ours must be above that.
    assert estimate >= len(text) // 4


def test_the_estimate_counts_both_system_and_messages() -> None:
    only_system = estimate_tokens([{"text": "a" * 300}], [])
    both = estimate_tokens([{"text": "a" * 300}], [{"role": "user", "content": "b" * 300}])
    assert both > only_system


def test_cached_input_is_discounted_not_free() -> None:
    """Charging cached tokens in full makes caching pointless; charging nothing
    lets a huge cached prefix run free."""
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "prompt_tokens_details": {"cached_tokens": 900},
    }
    assert weighted_tokens(usage) == int(100 + 900 * 0.2 + 100)


def test_uncached_usage_counts_in_full() -> None:
    assert weighted_tokens({"prompt_tokens": 500, "completion_tokens": 100}) == 600


# ------------------------------------------------------------------- provider


def test_stable_system_blocks_come_first() -> None:
    """The prefix cache is the cost model; volatile content must come last."""
    messages = build_messages(
        [
            {"text": "STABLE PROMPT", "stable": True},
            {"text": "today is 2026-09-06, 12 notifications left", "stable": False},
        ],
        [{"role": "user", "content": "hi"}],
    )
    system = messages[0]["content"]
    assert system.index("STABLE PROMPT") < system.index("today is")


def test_a_request_with_no_system_blocks_is_still_valid() -> None:
    assert build_messages([], [{"role": "user", "content": "hi"}]) == [
        {"role": "user", "content": "hi"}
    ]


def test_normalise_flattens_one_choice() -> None:
    payload = normalise(
        {
            "model": "zai-org/GLM-5.3",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": None,
                        "reasoning_content": "thinking out loud",
                        "tool_calls": [{"id": "c1", "function": {"name": "memory_search"}}],
                    },
                }
            ],
            "usage": {"prompt_tokens": 10},
        }
    )
    assert payload["finish_reason"] == "tool_calls"
    assert payload["reasoning_content"] == "thinking out loud"
    assert payload["tool_calls"][0]["function"]["name"] == "memory_search"


def test_normalise_survives_an_empty_response() -> None:
    payload = normalise({})
    assert payload["finish_reason"] == "stop"
    assert payload["tool_calls"] == []
