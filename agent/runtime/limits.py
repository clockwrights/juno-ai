"""What stops a loop.

Four independent things, because any one of them can be the wrong one for a
given failure: wall clock, model calls, tool calls, and a hard cap on
continuations after a truncated response. None of these is a budget — budgets
live in the gateway, outside the agent's reach. These are the runtime's own
brakes, and a proposal *could* change them, which is precisely why the gateway
does not rely on them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Limits:
    """Bounds for one invocation."""

    wall_clock_seconds: int = 720
    max_model_calls: int = 24
    max_tool_calls: int = 60
    max_continuations: int = 1

    started_at: float = field(default_factory=time.monotonic)
    model_calls: int = 0
    tool_calls: int = 0
    continuations: int = 0

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def remaining_seconds(self) -> float:
        return max(0.0, self.wall_clock_seconds - self.elapsed())

    def record_model_call(self) -> None:
        self.model_calls += 1

    def record_tool_calls(self, n: int) -> None:
        self.tool_calls += n

    def record_continuation(self) -> None:
        self.continuations += 1

    def exhausted(self) -> str | None:
        """The reason to stop, or None to keep going.

        Returning the reason rather than a boolean means the diary entry and the
        `heartbeat.completed` event can say *why* a cycle ended, which is the
        difference between a useful log and a mystery.
        """
        if self.elapsed() >= self.wall_clock_seconds:
            return "wall_clock"
        if self.model_calls >= self.max_model_calls:
            return "model_calls"
        if self.tool_calls >= self.max_tool_calls:
            return "tool_calls"
        return None

    def may_continue(self) -> bool:
        """Whether a `length` finish may be continued once more."""
        return self.continuations < self.max_continuations


CONVERSATION_LIMITS = {
    "wall_clock_seconds": 90,
    "max_model_calls": 12,
    "max_tool_calls": 24,
}

HEARTBEAT_LIMITS = {
    "wall_clock_seconds": 720,
    "max_model_calls": 24,
    "max_tool_calls": 60,
}


def for_mode(mode: str, wall_clock_seconds: int | None = None) -> Limits:
    base = CONVERSATION_LIMITS if mode == "conversation" else HEARTBEAT_LIMITS
    limits = Limits(**base)
    if wall_clock_seconds is not None:
        limits.wall_clock_seconds = wall_clock_seconds
    return limits
