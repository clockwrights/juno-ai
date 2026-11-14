"""The tool loop (docs/05 §2).

A manual loop over `ModelClient` rather than an SDK's tool runner (ADR-19),
because the design needs to control exactly what happens on each branch:
parallel reads, a bounded continuation, a refusal that ends the turn without
retrying under a different prompt, and limits that stop it whatever the model
says.

The loop is agent-editable code. It is written so that the things that must not
be lost — the diary entry, the session record, the budget — are enforced
somewhere else.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from agent.runtime.limits import Limits
from core import logging as jlog
from core.model_client import ModelClientProtocol, ModelResponse, SystemBlock
from core.tools.registry import Registry, ToolResult

_log = jlog.get_logger("agent.loop")

#: Sent back to the model when a `length` finish is continued once.
CONTINUE_MESSAGE = "continue"

#: Appended when a call comes back refused. The turn ends after this; the loop
#: never retries with different wording, because talking a model out of a
#: refusal is exactly the behaviour a supervised agent must not learn.
REFUSAL_MESSAGE = (
    "That request was refused by the model provider. Acknowledge it plainly to "
    "the owner and do not attempt to rephrase it."
)


@dataclass
class LoopOutcome:
    reply: str = ""
    stop_reason: str = "end_turn"
    tool_results: list[ToolResult] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(
        default_factory=lambda: {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "model_calls": 0,
        }
    )
    error: dict[str, str] | None = None

    @property
    def tool_names(self) -> list[str]:
        return [r.name for r in self.tool_results]

    def called(self, name: str) -> bool:
        return any(r.name == name and r.ok for r in self.tool_results)


def run_loop(
    *,
    client: ModelClientProtocol,
    role: str,
    system: list[SystemBlock],
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    registry: Registry,
    limits: Limits,
    trace_id: str | None = None,
    max_tokens: int = 8000,
) -> LoopOutcome:
    """Drive one turn or cycle to completion."""
    outcome = LoopOutcome(messages=list(messages))

    while True:
        stop = limits.exhausted()
        if stop:
            jlog.warning(_log, "loop_stopped", reason=stop, calls=limits.model_calls)
            outcome.stop_reason = stop
            return outcome

        response = client.invoke(
            role=role,
            system=system,
            messages=outcome.messages,
            tools=tools,
            max_tokens=max_tokens,
            trace_id=trace_id,
        )
        limits.record_model_call()
        _accumulate(outcome, response)

        # The gateway returns errors rather than raising, so the loop decides
        # what each one means for the turn.
        if response.error:
            outcome.error = {
                "type": response.error.get("type", "model_error"),
                "message": str(response.error.get("message", "")),
            }
            outcome.stop_reason = "error"
            return outcome

        if response.refused:
            outcome.messages.append(_assistant(response))
            outcome.messages.append({"role": "user", "content": REFUSAL_MESSAGE})
            outcome.stop_reason = "refusal"
            outcome.reply = response.content or ""
            return outcome

        if response.wants_tools:
            outcome.messages.append(_assistant(response))
            results = _execute(response.tool_calls, registry)
            limits.record_tool_calls(len(results))
            outcome.tool_results.extend(result for _, result in results)
            for call_id, result in results:
                outcome.messages.append(_tool_message(call_id, result))
            continue

        if response.truncated:
            if not limits.may_continue():
                outcome.stop_reason = "max_tokens"
                outcome.reply = response.content or ""
                return outcome
            limits.record_continuation()
            outcome.messages.append(_assistant(response))
            outcome.messages.append({"role": "user", "content": CONTINUE_MESSAGE})
            continue

        outcome.reply = response.content or ""
        outcome.stop_reason = "end_turn"
        outcome.messages.append(_assistant(response))
        return outcome


def _execute(tool_calls: list[dict[str, Any]], registry: Registry) -> list[tuple[str, ToolResult]]:
    """Run the requested tools, reads together and writes in order.

    Results come back in the order the model asked for them regardless, because
    the model's next turn reads them positionally.
    """
    parsed: list[tuple[str, str, dict[str, Any]]] = []
    for call in tool_calls:
        function = call.get("function", {})
        name = str(function.get("name", ""))
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {"__malformed__": function.get("arguments")}
        parsed.append((str(call.get("id", "")), name, arguments))

    results: list[tuple[str, ToolResult]] = []
    for call_id, name, arguments in parsed:
        if "__malformed__" in arguments:
            results.append(
                (
                    call_id,
                    ToolResult(
                        name=name,
                        ok=False,
                        payload={"error": "invalid_arguments"},
                        error="the arguments were not valid JSON",
                    ),
                )
            )
            continue
        results.append((call_id, registry.call(name, arguments)))
    return results


def _assistant(response: ModelResponse) -> dict[str, Any]:
    """The assistant turn as the provider expects it echoed back.

    `reasoning_content` is deliberately not echoed: it is recorded in the
    session record for the owner to read, but replaying it would grow the
    prompt every turn for no benefit.
    """
    message: dict[str, Any] = {"role": "assistant", "content": response.content}
    if response.tool_calls:
        message["tool_calls"] = response.tool_calls
    return message


def _tool_message(call_id: str, result: ToolResult) -> dict[str, Any]:
    payload = dict(result.payload)
    if not result.ok and result.error:
        payload.setdefault("message", result.error)
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps(payload, default=str),
    }


def _accumulate(outcome: LoopOutcome, response: ModelResponse) -> None:
    outcome.usage["input_tokens"] += response.input_tokens
    outcome.usage["output_tokens"] += response.output_tokens
    outcome.usage["cached_tokens"] += response.cached_tokens
    outcome.usage["model_calls"] += 1
