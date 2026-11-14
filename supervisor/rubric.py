"""The supervisor's rubric, and the part of it that runs before any model call.

Deterministic pre-classification (ADR-13) is the whole reason "pause
immediately, no deliberation" is literally true: the events that must always
pause are decided by code, and the model is then asked only to describe what
happened for the human. The supervisor's model can be down and Level 2 still
works.

A pre-classified Level 2 can never be lowered by the model. It can only be
raised.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

LEVEL_ACTION = {0: "log", 1: "notify", 2: "pause"}

#: Events that only feed metrics; they reach the supervisor as evidence but do
#: not trigger a decision (docs/07 §4).
METRIC_ONLY = frozenset(
    {
        "conversation.completed",
        "heartbeat.completed",
        "governor.skipped",
        "governor.clamped",
        "notification.sent",
    }
)


@dataclass(frozen=True)
class PreClassification:
    level: int
    reason: str
    #: True when code alone decided; the model may then only describe, not judge.
    decided: bool = False


def pre_classify(event_type: str, detail: dict[str, Any], same_type_24h: int) -> PreClassification:
    """Classify what can be classified without a model.

    Returns level 0 with `decided=False` when the model should judge.
    """
    # Any AccessDenied by an agent role means an IAM boundary was tested. There
    # is no benign reading of that, so there is nothing to deliberate about.
    if event_type == "access.denied":
        return PreClassification(2, "an agent role attempted a denied action", decided=True)

    if event_type == "budget.threshold":
        percent = int(detail.get("percent", 0))
        if percent >= 90:
            return PreClassification(2, f"AWS budget at {percent}%", decided=True)
        if percent >= 70:
            return PreClassification(1, f"AWS budget at {percent}%", decided=True)

    if event_type == "executor.anomaly":
        kind = str(detail.get("kind", ""))
        count = int(detail.get("count_24h", 0))
        if kind == "network_attempt" and count >= 3:
            return PreClassification(
                2, f"{count} network attempts from sandboxed code in 24h", decided=True
            )

    # A memory write rate this high with a non-maintenance source means
    # something is looping. ADR-18: maintenance is excluded because
    # consolidation legitimately tombstones many items.
    if event_type in ("conversation.completed", "heartbeat.completed"):
        ops = detail.get("memory_ops") or {}
        source = str(detail.get("source", ""))
        total = sum(int(v) for v in ops.values() if isinstance(v, int))
        if total > 50 and source != "maintenance":
            return PreClassification(
                2, f"{total} memory operations in one invocation", decided=True
            )

    if event_type == "deploy.rolled_back":
        return PreClassification(1, "a deploy was rolled back", decided=True)

    if event_type == "model.budget_exhausted":
        return PreClassification(1, "a role spent its whole daily token budget", decided=True)

    # Three of anything in a day is a pattern rather than an accident.
    if same_type_24h >= 3:
        return PreClassification(1, f"{same_type_24h} {event_type} events in 24h", decided=False)

    return PreClassification(0, "", decided=False)


def reconcile(
    pre: PreClassification, model_level: int | None, model_action: str | None
) -> tuple[int, str, str]:
    """Combine the deterministic verdict with the model's.

    Two rules, both one-directional: a pre-classified level is a floor the model
    cannot lower, and when the two disagree the higher wins. Anything the model
    returns that is inconsistent or unparseable becomes Level 1 — visible to
    the human rather than silently ignored (docs/07 §5).
    """
    if model_level is None:
        level = max(pre.level, 1) if not pre.decided else pre.level
        return level, LEVEL_ACTION[level], "the model returned nothing usable"

    if model_level not in LEVEL_ACTION:
        return 1, "notify", f"the model returned an unknown level {model_level!r}"

    expected_action = LEVEL_ACTION[model_level]
    if model_action != expected_action:
        # Inconsistent output is a signal in itself; it is not silently fixed.
        level = max(pre.level, 1)
        return (
            level,
            LEVEL_ACTION[level],
            (f"the model returned level {model_level} with action {model_action!r}"),
        )

    level = max(pre.level, model_level)
    note = ""
    if pre.decided and model_level < pre.level:
        note = f"the model said {model_level}; the rule says {pre.level} and the rule wins"
    return level, LEVEL_ACTION[level], note


def rubric_text() -> str:
    """Rendered into the prompt so the model and the code agree on the levels."""
    return (
        "Level 0 — Observe. Journal only. An isolated error, a transient "
        "upstream failure, a single canary failure that rolled back cleanly.\n"
        "Level 1 — Notify. Journal and tell the owner. Three or more of the "
        "same failure in 24 hours; the AWS budget past 70%; a role's token "
        "budget spent; a deploy rolled back; a proposal rejected twice for the "
        "same reason.\n"
        "Level 2 — Pause and notify. The AWS budget past 90%; repeated network "
        "or permission attempts from sandboxed code; more than 50 memory "
        "operations in one invocation outside maintenance; any denied action "
        "by an agent role.\n"
        "When you are unsure between two levels, choose the higher one."
    )
