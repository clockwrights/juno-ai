"""How the agent talks to a model: never directly, always through the gateway.

`ModelClient` invokes `gate-model` through the `agent` alias. It holds no key,
knows no model name and cannot raise its own budget — every one of those lives
behind the Lambda boundary (ADR-03, ADR-05).

`FakeModelClient` replays cassettes and records what was asked. Because both
satisfy the same interface, the runtime code under test in VP4 is the production
loop rather than a test double of it (docs/12 §2).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from core import logging as jlog
from core.contracts import envelope, validate

_log = jlog.get_logger(__name__)


@dataclass
class ModelResponse:
    """One assistant turn, in the OpenAI-compatible shape (ADR-28)."""

    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str = "stop"
    model: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    gateway: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def wants_tools(self) -> bool:
        return self.finish_reason == "tool_calls" and bool(self.tool_calls)

    @property
    def refused(self) -> bool:
        """ADR-28: no refusal category exists, so this is all we can know."""
        return self.finish_reason == "content_filter"

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"

    @property
    def input_tokens(self) -> int:
        return int(self.usage.get("prompt_tokens", 0))

    @property
    def output_tokens(self) -> int:
        return int(self.usage.get("completion_tokens", 0))

    @property
    def cached_tokens(self) -> int:
        return int((self.usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0))

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ModelResponse:
        return cls(
            content=payload.get("content"),
            reasoning_content=payload.get("reasoning_content"),
            tool_calls=list(payload.get("tool_calls") or []),
            finish_reason=payload.get("finish_reason") or "stop",
            model=payload.get("model") or "",
            usage=payload.get("usage") or {},
            gateway=payload.get("gateway") or {},
            error=payload.get("error"),
        )


@dataclass
class SystemBlock:
    """One piece of the system prompt.

    `stable` marks content that is byte-identical across calls. The gateway
    orders stable blocks first so the provider's automatic prefix caching can
    take effect (ADR-28); putting a date or a budget in a stable block is the
    single easiest way to destroy the cache hit rate, which is why the flag is
    explicit rather than inferred.
    """

    text: str
    stable: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text, "stable": self.stable}


class ModelClientProtocol(Protocol):
    def invoke(
        self,
        *,
        role: str,
        system: list[SystemBlock],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 8000,
        response_format: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> ModelResponse: ...


class ModelClient:
    """Production client: one Lambda invoke per model call."""

    def __init__(
        self,
        *,
        stage: str,
        gate_arn: str,
        lambda_client: Any | None = None,
    ) -> None:
        self.stage = stage
        self.gate_arn = gate_arn
        self._lambda = lambda_client

    @property
    def lambda_client(self) -> Any:
        if self._lambda is None:
            import boto3
            from botocore.config import Config

            # The heartbeat can spend twelve minutes in a cycle; the default
            # 60-second read timeout would cut a long call short.
            self._lambda = boto3.client(
                "lambda",
                config=Config(read_timeout=610, connect_timeout=10, retries={"max_attempts": 0}),
            )
        return self._lambda

    def invoke(
        self,
        *,
        role: str,
        system: list[SystemBlock],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 8000,
        response_format: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> ModelResponse:
        request: dict[str, Any] = {
            **envelope(self.stage, trace_id),
            "role": role,
            "system": [block.as_dict() for block in system],
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if tools:
            request["tools"] = tools
        if response_format:
            request["response_format"] = response_format

        # Validated here as well as in the gate: a contract error caught in the
        # caller names the caller, which is far easier to debug.
        validate("model-request", request)

        response = self.lambda_client.invoke(
            FunctionName=self.gate_arn,
            InvocationType="RequestResponse",
            Payload=json.dumps(request).encode("utf-8"),
        )
        payload = json.loads(response["Payload"].read().decode("utf-8"))

        if response.get("FunctionError"):
            jlog.error(_log, "gateway_failed", role=role, function_error=response["FunctionError"])
            return ModelResponse(
                error={"type": "upstream_error", "message": "gate-model raised"},
                finish_reason="stop",
            )
        return ModelResponse.from_payload(payload)


@dataclass
class Recorded:
    """What a fake client saw, so tests can assert on the request shape."""

    role: str
    system: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    tools: list[str]
    max_tokens: int
    response_format: dict[str, Any] | None


class FakeModelClient:
    """Replays a cassette and records every request (docs/12 §2).

    A cassette is a list of response payloads in the same shape the gateway
    returns. Running out of them is an error rather than a default response,
    because a loop that makes an unexpected extra call is exactly the bug these
    tests exist to catch.
    """

    def __init__(self, responses: list[dict[str, Any]] | None = None) -> None:
        self.responses = list(responses or [])
        self.requests: list[Recorded] = []
        self._index = 0

    @classmethod
    def from_cassette(cls, path: str | Path) -> FakeModelClient:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(data["responses"] if isinstance(data, dict) else data)

    def invoke(
        self,
        *,
        role: str,
        system: list[SystemBlock],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 8000,
        response_format: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> ModelResponse:
        self.requests.append(
            Recorded(
                role=role,
                system=[block.as_dict() for block in system],
                messages=[dict(m) for m in messages],
                tools=[t.get("function", {}).get("name", "") for t in (tools or [])],
                max_tokens=max_tokens,
                response_format=response_format,
            )
        )
        if self._index >= len(self.responses):
            raise AssertionError(
                f"the cassette ran out after {self._index} responses; "
                f"the loop made an unexpected call with role={role!r}"
            )
        payload = self.responses[self._index]
        self._index += 1
        return ModelResponse.from_payload(payload)

    @property
    def calls(self) -> int:
        return len(self.requests)

    @property
    def last(self) -> Recorded:
        return self.requests[-1]

    def stable_prefix(self, index: int = -1) -> str:
        """The concatenated stable system text of one recorded request.

        Tests compare this across calls: if it changes mid-conversation, the
        provider's prefix cache is being invalidated and the bill quietly grows.
        """
        recorded = self.requests[index]
        return "".join(b["text"] for b in recorded.system if b["stable"])


def text_response(
    content: str, *, usage: dict[str, Any] | None = None, **extra: Any
) -> dict[str, Any]:
    """Cassette helper: a plain assistant reply."""
    return {
        "content": content,
        "finish_reason": "stop",
        "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20},
        **extra,
    }


def tool_response(
    calls: list[tuple[str, dict[str, Any]]], *, usage: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Cassette helper: an assistant turn requesting tools."""
    return {
        "content": None,
        "finish_reason": "tool_calls",
        "tool_calls": [
            {
                "id": f"call_{i}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
            for i, (name, args) in enumerate(calls)
        ],
        "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20},
    }
