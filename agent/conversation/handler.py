"""`agent-conversation` — one turn (docs/02 §3.1).

The handler never raises to its caller. `gate-dispatcher` has to be able to
answer the owner whatever happened, so every failure comes back as a response
with a fallback reply and an `error` object (docs/03 §2.1).
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

_log = jlog.get_logger("agent.conversation")

PROMPT = "system_conversation.md"
system_prompt(PROMPT)

FALLBACK = {
    "model_error": "Something went wrong reaching my own model. Try me again in a moment.",
    "budget_exhausted": (
        "I have spent my model budget for today. I will be back tomorrow, and "
        "you can raise the budget if this keeps happening."
    ),
    "timeout": "That took too long and I cut it off. Ask me again, maybe more narrowly.",
    "tool_error": "One of my tools failed and I could not finish that properly.",
    "crash": "I broke while answering that. The owner has been alerted.",
}

#: The owner asking for canonical work is what unlocks `artifacts/` for a turn.
#: Deliberately narrow: idle cycles never get it (the staging-rule invariant).
_ARTIFACT_REQUEST_HINTS = (
    "save it to artifacts",
    "put it in artifacts",
    "promote",
    "make it official",
    "publish it",
)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return run_turn(event, build_wiring(event))


def run_turn(event: dict[str, Any], wiring: Wiring) -> dict[str, Any]:
    validate("conversation-request", event)
    trace_id = str(event["trace_id"])
    jlog.set_trace_id(trace_id)

    conversation_id = str(event["conversation_id"])
    message = event["message"]
    budgets = event["budgets"]

    outcome = LoopOutcome()
    notes_delivered: list[str] = []

    try:
        wiring.tools.may_write_artifacts = _asked_for_artifacts(str(message["text"]))

        notes = wiring.tools.state.pending_notes()
        notes_delivered = [n["note_id"] for n in notes]

        blocks = ctx_builder.build_context(
            static_prompt=system_prompt(PROMPT),
            tools_prompt=wiring.tools_prompt,
            profile=wiring.tools.profile.read().content,
            diary_entries=wiring.tools.diary.last(2),
            notes=notes,
            budgets=ctx_builder.Budgets(
                notifications_remaining=int(budgets["notifications_remaining"]),
                tokens_remaining_today=int(budgets["tokens_remaining_today"]),
            ),
            now_iso=wiring.now_iso,
            mode="conversation",
        )

        history = ctx_builder.transcript_to_messages(
            wiring.tools.conversations.transcript(conversation_id)
        )
        history.append({"role": "user", "content": str(message["text"])})

        registry = Registry(wiring.handlers, mode="conversation", namespace=wiring.namespace)

        outcome = run_loop(
            client=wiring.model,
            role="conversation",
            system=blocks,
            messages=history,
            tools=tool_definitions("conversation", tool_descriptions()),
            registry=registry,
            limits=for_mode("conversation"),
            trace_id=trace_id,
        )

        if outcome.error is None and notes_delivered:
            # Marked delivered only once a reply actually exists, so a failed
            # turn does not silently swallow a note.
            wiring.tools.state.mark_delivered(notes_delivered)
        else:
            notes_delivered = []

    except Exception as exc:
        jlog.error(_log, "conversation_failed", error=str(exc))
        outcome.error = {"type": "crash", "message": str(exc)[:500]}
        notes_delivered = []

    reply = outcome.reply.strip()
    if outcome.error or not reply:
        reply = reply or FALLBACK.get(
            (outcome.error or {}).get("type", "model_error"), FALLBACK["model_error"]
        )

    response: dict[str, Any] = {
        **envelope(wiring.stage, trace_id),
        "reply": reply,
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
        "notes_delivered": notes_delivered,
    }
    if outcome.error:
        response["error"] = outcome.error

    validate("conversation-response", response)
    return response


def _asked_for_artifacts(text: str) -> bool:
    lowered = text.lower()
    return any(hint in lowered for hint in _ARTIFACT_REQUEST_HINTS)


def _memory_ops(outcome: LoopOutcome) -> dict[str, int]:
    counts = {"saved": 0, "updated": 0, "forgotten": 0}
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
