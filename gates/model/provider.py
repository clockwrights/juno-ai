"""The provider boundary.

Everything that knows Together's wire format lives here, so ADR-28's note that
"gate-model is the only component that changes" if the provider changes is
literally true — it is this file plus the model ids in `policy.py`.

The API key is read from SSM at cold start and never enters an environment
variable (ADR-21), so it does not appear in the console, in
`GetFunctionConfiguration`, or in CloudFormation drift output.
"""

from __future__ import annotations

import functools
from typing import Any, Protocol

from gates.model.policy import BASE_URL, EMBEDDING_MODEL


class Provider(Protocol):
    def build_request(
        self,
        *,
        model: str,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
        response_format: dict[str, Any] | None,
        reasoning: bool,
    ) -> dict[str, Any]: ...

    def complete(self, request: dict[str, Any]) -> dict[str, Any]: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def build_messages(
    system: list[dict[str, Any]], messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Fold the system blocks into one leading message, stable parts first.

    The ordering is the whole cache strategy under ADR-28: the provider caches
    a matching prefix automatically, so anything that changes per call must come
    after everything that does not. `stable` is declared by the caller rather
    than guessed, and this is where the declaration is honoured.
    """
    stable = [b["text"] for b in system if b.get("stable", True)]
    volatile = [b["text"] for b in system if not b.get("stable", True)]
    text = "\n\n".join([*stable, *volatile]).strip()
    if not text:
        return list(messages)
    return [{"role": "system", "content": text}, *messages]


class TogetherProvider:
    def __init__(
        self,
        *,
        api_key_parameter: str,
        base_url: str = BASE_URL,
        client: Any | None = None,
        ssm: Any | None = None,
    ) -> None:
        self.api_key_parameter = api_key_parameter
        self.base_url = base_url
        self._client = client
        self._ssm = ssm

    @functools.cached_property
    def api_key(self) -> str:
        import boto3

        ssm = self._ssm or boto3.client("ssm")
        response = ssm.get_parameter(Name=self.api_key_parameter, WithDecryption=True)
        return str(response["Parameter"]["Value"])

    @property
    def client(self) -> Any:
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=600.0)
        return self._client

    def build_request(
        self,
        *,
        model: str,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
        response_format: dict[str, Any] | None,
        reasoning: bool,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": model,
            "messages": build_messages(system, messages),
            "max_tokens": max_tokens,
        }
        if tools:
            request["tools"] = tools
            request["tool_choice"] = "auto"
        if response_format:
            request["response_format"] = response_format
        return request

    def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        completion = self.client.chat.completions.create(**request)
        raw = completion.model_dump() if hasattr(completion, "model_dump") else dict(completion)
        return normalise(raw)

    def embed(self, texts: list[str]) -> list[list[float]]:
        response = self.client.embeddings.create(model=EMBEDDING_MODEL, input=texts)
        raw = response.model_dump() if hasattr(response, "model_dump") else dict(response)
        return [item["embedding"] for item in raw["data"]]


def normalise(raw: dict[str, Any]) -> dict[str, Any]:
    """Flatten one completion into the shape `contracts/model-response` names.

    Callers should never have to know that a choice is a list of one, or where
    `reasoning_content` hangs.
    """
    choices = raw.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    return {
        "content": message.get("content"),
        "reasoning_content": message.get("reasoning_content"),
        "tool_calls": message.get("tool_calls") or [],
        "finish_reason": choice.get("finish_reason") or "stop",
        "model": raw.get("model"),
        "usage": raw.get("usage") or {},
    }
