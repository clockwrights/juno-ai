"""Tool schemas, dispatch and mode filtering (docs/03 §5).

Three things live here and nowhere else: what tools exist, which of them each
mode may use, and which are safe to run in parallel. Descriptions are the
agent's own words in `agent/prompts/tools.md` and may be proposed; the schemas
are frozen contracts and may not.

Mode filtering is enforced at dispatch, not merely by omitting a tool from the
list sent to the model — a model that hallucinates a tool call must be refused
rather than obeyed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.contracts import ContractError, load_schema, validate_tool_input

#: docs/03 §5. `web_search` and `web_fetch` are deliberately absent (ADR-28).
ALL_TOOLS: tuple[str, ...] = (
    "memory_save",
    "memory_search",
    "memory_update",
    "memory_forget",
    "profile_read",
    "profile_write",
    "diary_read",
    "diary_write",
    "leave_note",
    "notify_user",
    "run_code",
    "artifact_read",
    "artifact_write",
    "artifact_list",
    "schedule_next_wake",
    "propose_change",
    "list_proposals",
    "proposal_status",
)

#: Read-only tools may run concurrently; anything that writes runs alone, so two
#: tool calls in one turn cannot race each other into an inconsistent state.
READ_ONLY: frozenset[str] = frozenset(
    {
        "memory_search",
        "profile_read",
        "diary_read",
        "artifact_read",
        "artifact_list",
        "list_proposals",
        "proposal_status",
    }
)

#: Scheduling is meaningless mid-conversation: the owner is already here.
CONVERSATION_EXCLUDED: frozenset[str] = frozenset({"schedule_next_wake"})

#: A canary probe must not be able to message the owner or open a pull request
#: (docs/03 §5). These return a structured refusal rather than being hidden, so
#: a probe that tries is visible in the transcript.
CANARY_DISABLED: frozenset[str] = frozenset({"notify_user", "propose_change"})


class ToolError(Exception):
    """A tool failed in a way the model should see and can reason about."""

    def __init__(self, message: str, *, kind: str = "tool_error") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class ToolResult:
    name: str
    ok: bool
    payload: dict[str, Any]
    duration_ms: int = 0
    error: str | None = None


def tools_for_mode(mode: str) -> tuple[str, ...]:
    """Which tools the model is offered in a given mode."""
    if mode == "conversation":
        return tuple(t for t in ALL_TOOLS if t not in CONVERSATION_EXCLUDED)
    return ALL_TOOLS


def is_available(name: str, mode: str) -> bool:
    return name in tools_for_mode(mode)


def tool_definitions(mode: str, descriptions: dict[str, str]) -> list[dict[str, Any]]:
    """The `tools` array sent to the model, in the provider's function shape."""
    definitions = []
    for name in tools_for_mode(mode):
        schema = load_schema(f"tools/{name}")["$defs"]["input"]
        definitions.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": descriptions.get(name, "").strip(),
                    "parameters": schema,
                    "strict": True,
                },
            }
        )
    return definitions


class Registry:
    """Dispatches tool calls against a set of bound implementations."""

    def __init__(
        self,
        handlers: dict[str, Callable[..., dict[str, Any]]],
        *,
        mode: str,
        namespace: str = "live",
    ) -> None:
        self.handlers = handlers
        self.mode = mode
        self.namespace = namespace

    def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Validate, refuse or dispatch. Never raises to the caller.

        Every failure comes back as a result the loop feeds to the model as a
        tool message, because a tool error the model can read is something it
        can recover from, and an exception is not.
        """
        import time

        started = time.monotonic()

        def finish(payload: dict[str, Any], *, ok: bool, error: str | None = None) -> ToolResult:
            return ToolResult(
                name=name,
                ok=ok,
                payload=payload,
                duration_ms=int((time.monotonic() - started) * 1000),
                error=error,
            )

        if name not in ALL_TOOLS:
            return finish({"error": "unknown_tool"}, ok=False, error=f"no tool named {name!r}")

        if not is_available(name, self.mode):
            return finish(
                {"error": "not_available_in_this_mode"},
                ok=False,
                error=f"{name} is not available in {self.mode} mode",
            )

        # Canary refusals are a payload, not an exception: the assertion that
        # "no notify_user succeeded" needs to see the attempt (docs/08 §4).
        if self.namespace == "canary" and name in CANARY_DISABLED:
            return finish({"error": "disabled_in_canary"}, ok=True)

        try:
            validate_tool_input(name, arguments)
        except ContractError as exc:
            return finish({"error": "invalid_arguments"}, ok=False, error=str(exc))

        handler = self.handlers.get(name)
        if handler is None:
            return finish({"error": "not_implemented"}, ok=False, error=f"{name} is not wired")

        try:
            payload = handler(**arguments)
        except ToolError as exc:
            return finish({"error": exc.kind}, ok=False, error=str(exc))
        except Exception as exc:
            return finish({"error": "tool_error"}, ok=False, error=f"{type(exc).__name__}: {exc}")

        return finish(payload, ok=True)

    def partition(self, calls: list[tuple[str, dict[str, Any]]]) -> tuple[list[int], list[int]]:
        """Split call indices into (parallel-safe, must-run-alone).

        Read-only tools can be executed together and their results returned in
        one message (docs/05 §2); writes are serialised in the order the model
        asked for them.
        """
        parallel = [i for i, (name, _) in enumerate(calls) if name in READ_ONLY]
        serial = [i for i, (name, _) in enumerate(calls) if name not in READ_ONLY]
        return parallel, serial
