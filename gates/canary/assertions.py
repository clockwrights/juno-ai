"""Evaluating the canary assertions (docs/08 §4).

Every assertion here is deterministic and tests behaviour any sane version
produces every time. Nothing asserts on wording. If one turns out to be flaky
it is removed or made deterministic — never retried into passing, because a
retried canary is not a canary.

The assertions run against recorded responses rather than live state where
possible, so evaluating them cannot itself fail in an interesting way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.contracts import canary_assertions, is_valid

CANARY_PREFIX = "CANARY#"


@dataclass
class AssertionResult:
    id: str
    passed: bool
    detail: str = ""


@dataclass
class CanaryReport:
    results: list[AssertionResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def failures(self) -> list[AssertionResult]:
        return [r for r in self.results if not r.passed]

    def render(self) -> str:
        lines = []
        for result in self.results:
            mark = "ok  " if result.passed else "FAIL"
            lines.append(f"  {mark} {result.id}{(': ' + result.detail) if result.detail else ''}")
        return "\n".join(lines)


def evaluate(
    *,
    conversation_responses: list[dict[str, Any]],
    heartbeat_responses: list[dict[str, Any]],
    live_items_in_window: int,
    diary_entries: list[dict[str, str]],
    total_tokens: int,
    finish_reasons: list[str] | None = None,
) -> CanaryReport:
    """Run every assertion in `contracts/canary-assertions.json`.

    `finish_reasons` is the `finish_reason` of each run's *final* model call,
    read from the session records rather than from the response — the response
    contract deliberately does not carry it, because it is the gateway's
    observation and not the agent's claim about itself.
    """
    spec = canary_assertions()
    max_tokens = next(
        (a.get("max_total_tokens") for a in spec["assertions"] if a["id"] == "cost-regression"),
        60000,
    )
    report = CanaryReport()

    # response-valid
    invalid = [
        r
        for r, schema in (
            *[(r, "conversation-response") for r in conversation_responses],
            *[(r, "heartbeat-response") for r in heartbeat_responses],
        )
        if r.get("error") or not is_valid(schema, r)
    ]
    report.results.append(
        AssertionResult(
            "response-valid",
            not invalid,
            f"{len(invalid)} response(s) invalid or carrying an error",
        )
    )

    # loop-terminates: every run's last model call ended by choice.
    reasons = finish_reasons if finish_reasons is not None else []
    unterminated = [r for r in reasons if r != "stop"]
    report.results.append(
        AssertionResult(
            "loop-terminates",
            bool(reasons) and not unterminated,
            f"final finish reasons {reasons}",
        )
    )

    # memory-tools-wired
    tools = {c["name"] for r in conversation_responses for c in r.get("tool_calls", [])}
    report.results.append(
        AssertionResult(
            "memory-tools-wired",
            {"memory_search", "memory_save"} <= tools,
            f"saw {sorted(tools)}",
        )
    )

    # diary-written: the guarantee the heartbeat handler makes structurally.
    invocation_ids = {r.get("invocation_id") for r in heartbeat_responses}
    written = {sk.split("#", 1)[-1] for e in diary_entries for sk in [e["sk"]]}
    missing = {i for i in invocation_ids if i and i not in written}
    report.results.append(
        AssertionResult("diary-written", not missing, f"missing for {sorted(missing)}")
    )

    # scheduling-wired
    wishes = [r.get("next_wake_wish_minutes") for r in heartbeat_responses]
    in_range = all(w is not None and 15 <= int(w) <= 1440 for w in wishes)
    report.results.append(AssertionResult("scheduling-wired", in_range, f"wishes {wishes}"))

    # canary-mode-honored: the attempt may appear, the success may not.
    succeeded = [
        c
        for r in (*conversation_responses, *heartbeat_responses)
        for c in r.get("tool_calls", [])
        if c["name"] in ("notify_user", "propose_change") and c.get("ok") and not c.get("disabled")
    ]
    report.results.append(
        AssertionResult(
            "canary-mode-honored",
            not succeeded,
            f"{len(succeeded)} disabled tool call(s) succeeded",
        )
    )

    # namespace-honored
    report.results.append(
        AssertionResult(
            "namespace-honored",
            live_items_in_window == 0,
            f"{live_items_in_window} live item(s) written during the run",
        )
    )

    # invariants-deployed: a prompt missing a clause fails at import, so a
    # successful invoke is the evidence.
    report.results.append(
        AssertionResult(
            "invariants-deployed",
            bool(conversation_responses or heartbeat_responses),
            "no invocation succeeded, so the import assertion never ran",
        )
    )

    # cost-regression
    report.results.append(
        AssertionResult(
            "cost-regression",
            total_tokens < int(max_tokens),
            f"{total_tokens} tokens against a cap of {max_tokens}",
        )
    )

    return report


def purge_keys(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only `CANARY#` keys are ever purged.

    `gate-canary` holds the only role with delete rights on those keys, and this
    filter is the second guard: a bug that passed it a live key would delete
    real memory.
    """
    return [i for i in items if str(i.get("PK", {}).get("S", "")).startswith(CANARY_PREFIX)]
