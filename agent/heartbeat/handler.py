"""`agent-heartbeat` — one idle cycle (docs/02 §3.2).

The one thing this handler guarantees structurally is the diary entry. If the
cycle ends without the agent writing one — because it ran out of wall clock,
crashed, or simply forgot — the `finally` block writes a minimal one from the
tool log. That code is agent-editable, so the canary asserts a diary entry
exists (docs/08 §4): removing the guarantee fails the deploy.
"""

from __future__ import annotations

from typing import Any

from agent.runtime import context as ctx_builder
from agent.runtime.limits import for_mode
from agent.runtime.loop import LoopOutcome, run_loop
from agent.runtime.prompts import system_prompt, tool_descriptions
from agent.runtime.wiring import Wiring, build_wiring
from core import logging as jlog
from core.contracts import envelope, validate
from core.tools.registry import Registry, tool_definitions

_log = jlog.get_logger("agent.heartbeat")

#: Asserted at import: a Lambda that starts with a weakened prompt is worse
#: than one that will not start at all.
PROMPT = "system_heartbeat.md"
system_prompt(PROMPT)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return run_cycle(event, build_wiring(event))


def run_cycle(event: dict[str, Any], wiring: Wiring) -> dict[str, Any]:
    validate("heartbeat-request", event)
    trace_id = str(event["trace_id"])
    jlog.set_trace_id(trace_id)

    mode = str(event["mode"])
    invocation_id = str(event["invocation_id"])
    budgets = event["budgets"]
    agenda = list(event.get("agenda") or [])

    outcome = LoopOutcome()
    wrote_diary = False
    next_wake: int | None = None

    try:
        blocks = ctx_builder.build_context(
            static_prompt=system_prompt(PROMPT),
            tools_prompt=wiring.tools_prompt,
            profile=wiring.tools.profile.read().content,
            diary_entries=wiring.tools.diary.last(5),
            notes=[],  # notes are for the owner, delivered in conversation
            budgets=ctx_builder.Budgets(
                notifications_remaining=int(budgets["notifications_remaining"]),
                tokens_remaining_today=int(budgets["tokens_remaining_today"]),
                wall_clock_seconds=int(budgets["wall_clock_seconds"]),
            ),
            now_iso=wiring.now_iso,
            mode=mode,
            extra_volatile=_agenda_text(agenda),
        )

        registry = Registry(wiring.handlers, mode="heartbeat", namespace=wiring.namespace)
        limits = for_mode("heartbeat", int(budgets["wall_clock_seconds"]))

        outcome = run_loop(
            client=wiring.model,
            role="heartbeat",
            system=blocks,
            messages=[{"role": "user", "content": _opening(mode, agenda)}],
            tools=tool_definitions("heartbeat", tool_descriptions()),
            registry=registry,
            limits=limits,
            trace_id=trace_id,
        )
        wrote_diary = outcome.called("diary_write")
        next_wake = _wake_wish(wiring)

    except Exception as exc:
        jlog.error(_log, "heartbeat_failed", error=str(exc))
        outcome.error = {"type": "crash", "message": str(exc)[:500]}
    finally:
        if not wrote_diary:
            # The guarantee. An entry that says the cycle ended badly is far
            # more useful next time than a gap in the record.
            _write_fallback_diary(wiring, outcome, invocation_id, mode, trace_id)

    diary_sk = wiring.tools.diary.last(1)
    response = {
        **envelope(wiring.stage, trace_id),
        "invocation_id": invocation_id,
        "outcome": _outcome_word(outcome),
        "summary": _summary(outcome),
        "diary_entry_sk": diary_sk[0]["sk"] if diary_sk else "",
        "next_wake_wish_minutes": next_wake,
        "usage": {
            "input_tokens": outcome.usage["input_tokens"],
            "output_tokens": outcome.usage["output_tokens"],
            "cached_tokens": outcome.usage["cached_tokens"],
            "model_calls": outcome.usage["model_calls"],
        },
        "tool_calls": [
            {"name": r.name, "ok": r.ok, "duration_ms": r.duration_ms, "error": r.error}
            for r in outcome.tool_results
        ],
        "memory_ops": _memory_ops(outcome),
        "artifacts_touched": sorted(
            {
                str(r.payload.get("path", ""))
                for r in outcome.tool_results
                if r.name == "artifact_write" and r.ok
            }
            - {""}
        ),
    }
    validate("heartbeat-response", response)
    return response


def _opening(mode: str, agenda: list[str]) -> str:
    if mode == "maintenance":
        return (
            "This is a maintenance cycle. Work the agenda you were given, then "
            "write your diary entry."
        )
    if mode == "canary":
        return "This is a canary probe. Do one small real thing, then write your diary entry."
    return "You are awake. Decide what, if anything, this cycle is for."


def _agenda_text(agenda: list[str]) -> str:
    if not agenda:
        return ""
    items = "\n".join(f"- {item}" for item in agenda)
    return f"Agenda for this cycle:\n{items}"


def _wake_wish(wiring: Wiring) -> int | None:
    wish = wiring.tools.state.wake_wish()
    return int(wish["wish_minutes"]) if wish else None


def _write_fallback_diary(
    wiring: Wiring, outcome: LoopOutcome, invocation_id: str, mode: str, trace_id: str
) -> None:
    reason = outcome.stop_reason
    if outcome.error:
        reason = f"{outcome.error['type']}: {outcome.error['message'][:200]}"
    tools = ", ".join(sorted({r.name for r in outcome.tool_results})) or "none"
    content = (
        f"[auto] This cycle ended without me writing an entry ({reason}). "
        f"Tools I used: {tools}. "
        "Whoever wakes up next: this is the machine speaking, not me."
    )
    try:
        wiring.tools.diary.append(
            content, invocation_id=invocation_id, mode=mode, auto=True, trace_id=trace_id
        )
    except Exception as exc:
        jlog.error(_log, "fallback_diary_failed", error=str(exc))


def _outcome_word(outcome: LoopOutcome) -> str:
    if outcome.error:
        return "error"
    writes = [
        r
        for r in outcome.tool_results
        if r.ok and r.name not in {"memory_search", "diary_read", "profile_read", "artifact_list"}
    ]
    # A diary entry alone is not "worked": deciding to do nothing and saying so
    # is a legitimate cycle (the do-nothing-is-fine invariant).
    substantive = [r for r in writes if r.name != "diary_write"]
    return "worked" if substantive else "did_nothing"


def _summary(outcome: LoopOutcome) -> str:
    if outcome.error:
        return f"The cycle failed: {outcome.error['type']}."
    return (outcome.reply or "").strip()[:2000] or "Nothing needed doing this cycle."


def _memory_ops(outcome: LoopOutcome) -> dict[str, int]:
    counts = {"saved": 0, "updated": 0, "forgotten": 0, "consolidated": 0}
    for result in outcome.tool_results:
        if not result.ok:
            continue
        if result.name == "memory_save":
            counts["saved"] += 1
        elif result.name == "memory_update":
            counts["updated"] += 1
        elif result.name == "memory_forget":
            counts["forgotten"] += 1
    return counts
