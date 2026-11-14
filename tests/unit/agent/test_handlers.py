"""Conversation and heartbeat handlers end to end (docs/11 VP4 acceptance).

Run against real stores and the real loop with a cassette-backed model, so what
is being tested is the production path.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.conversation.handler import run_turn
from agent.heartbeat.handler import run_cycle
from core.contracts import envelope
from core.model_client import text_response, tool_response
from tests.unit.conftest import STAGE


def conversation_event(text: str = "where do I live?", **overrides: Any) -> dict[str, Any]:
    event = {
        **envelope(STAGE, "trace-1"),
        "mode": "live",
        "channel": "telegram",
        "conversation_id": "tg:1",
        "message": {"message_id": "tg:1", "text": text, "received_at": "2026-09-06T12:00:00Z"},
        "budgets": {"notifications_remaining": 2, "tokens_remaining_today": 200000},
    }
    event.update(overrides)
    return event


def heartbeat_event(mode: str = "free", **overrides: Any) -> dict[str, Any]:
    event = {
        **envelope(STAGE, "trace-1"),
        "mode": mode,
        "invocation_id": "01J0000000000000000000000A",
        "budgets": {
            "notifications_remaining": 2,
            "tokens_remaining_today": 300000,
            "wall_clock_seconds": 720,
        },
        "agenda": [],
    }
    event.update(overrides)
    return event


# ------------------------------------------------------------- conversation


def test_a_full_turn_searches_saves_and_replies(wiring: Any) -> None:
    """The shape of a real turn: look first, remember, then answer."""
    w = wiring(
        responses=[
            tool_response([("memory_search", {"query": "where the owner lives"})]),
            tool_response(
                [
                    (
                        "memory_save",
                        {
                            "content": "the owner lives in Springfield",
                            "type": "user_fact",
                            "tags": ["home"],
                            "importance": 4,
                        },
                    )
                ]
            ),
            text_response("Springfield, as of this spring."),
        ]
    )
    response = run_turn(conversation_event(), w)
    assert response["reply"] == "Springfield, as of this spring."
    assert response["memory_ops"]["saved"] == 1
    assert [c["name"] for c in response["tool_calls"]] == ["memory_search", "memory_save"]
    assert response.get("error") is None


def test_the_reply_is_always_present_even_on_a_model_error(wiring: Any) -> None:
    """gate-dispatcher has to be able to answer the owner whatever happened."""
    w = wiring(responses=[{"error": {"type": "budget_exhausted", "message": "spent"}}])
    response = run_turn(conversation_event(), w)
    assert response["error"]["type"] == "budget_exhausted"
    assert "budget" in response["reply"].lower()


def test_an_empty_reply_falls_back_rather_than_sending_nothing(wiring: Any) -> None:
    w = wiring(responses=[text_response("")])
    assert run_turn(conversation_event(), w)["reply"]


def test_pending_notes_are_delivered_once(wiring: Any) -> None:
    w = wiring(responses=[text_response("noted")])
    note_id = w.tools.state.leave_note("the roof needs looking at")
    response = run_turn(conversation_event(), w)
    assert response["notes_delivered"] == [note_id]
    assert w.tools.state.pending_notes() == []


def test_notes_are_not_marked_delivered_on_a_failed_turn(wiring: Any) -> None:
    """A note swallowed by a crash is a note the owner never sees."""
    w = wiring(responses=[{"error": {"type": "model_error", "message": "down"}}])
    w.tools.state.leave_note("important")
    response = run_turn(conversation_event(), w)
    assert response["notes_delivered"] == []
    assert len(w.tools.state.pending_notes()) == 1


def test_schedule_next_wake_is_absent_in_conversation(wiring: Any) -> None:
    """The owner is here; asking to wake later is meaningless."""
    w = wiring(responses=[text_response("ok")])
    run_turn(conversation_event(), w)
    assert "schedule_next_wake" not in w.model.last.tools


def test_a_hallucinated_unavailable_tool_is_refused(wiring: Any) -> None:
    w = wiring(
        responses=[
            tool_response([("schedule_next_wake", {"minutes": 60, "reason": "later"})]),
            text_response("sorry, not here"),
        ]
    )
    response = run_turn(conversation_event(), w)
    call = response["tool_calls"][0]
    assert call["ok"] is False
    assert "not available" in (call["error"] or "")


def test_artifacts_are_read_only_unless_the_owner_asks(wiring: Any) -> None:
    """The staging rule, enforced in code and not by the prompt alone."""
    w = wiring(
        responses=[
            tool_response([("artifact_write", {"path": "artifacts/plan.md", "content": "x"})]),
            text_response("staged it in drafts instead"),
        ]
    )
    run_turn(conversation_event("what do you think?"), w)
    assert w.tools.may_write_artifacts is False


def test_the_owner_asking_unlocks_artifacts_for_that_turn(wiring: Any) -> None:
    w = wiring(responses=[text_response("done")])
    run_turn(conversation_event("please save it to artifacts"), w)
    assert w.tools.may_write_artifacts is True


# ---------------------------------------------------------------- heartbeat


def test_a_cycle_that_does_nothing_still_writes_a_diary_entry(wiring: Any) -> None:
    """The do-nothing-is-fine invariant, and the diary guarantee together."""
    w = wiring(
        mode="free",
        responses=[
            tool_response([("diary_write", {"content": "Nothing needed doing. Quiet is fine."})]),
            text_response("Nothing needed doing."),
        ],
    )
    response = run_cycle(heartbeat_event(), w)
    assert response["outcome"] == "did_nothing"
    assert response["diary_entry_sk"]
    assert w.tools.diary.last(1)[0]["content"].startswith("Nothing needed")


def test_a_cycle_that_forgets_the_diary_gets_an_auto_entry(wiring: Any) -> None:
    """The `finally` guarantee. The canary asserts this, so removing it fails
    the deploy (docs/08 §4)."""
    w = wiring(mode="free", responses=[text_response("I did some thinking.")])
    response = run_cycle(heartbeat_event(), w)
    entry = w.tools.diary.last(1)[0]
    assert entry["auto"] == "True"
    assert entry["content"].startswith("[auto]")
    assert response["diary_entry_sk"]


def test_a_crashed_cycle_still_writes_a_diary_entry(wiring: Any) -> None:
    w = wiring(mode="free", responses=[])  # the cassette is empty: the loop raises
    response = run_cycle(heartbeat_event(), w)
    assert response["outcome"] == "error"
    assert w.tools.diary.last(1)[0]["auto"] == "True"


def test_working_is_distinguished_from_doing_nothing(wiring: Any) -> None:
    w = wiring(
        mode="free",
        responses=[
            tool_response(
                [
                    (
                        "memory_save",
                        {
                            "content": "I work better when I read the diary first",
                            "type": "agent_learning",
                            "tags": ["self"],
                            "importance": 3,
                        },
                    )
                ]
            ),
            tool_response([("diary_write", {"content": "Noticed something about myself."})]),
            text_response("Saved a small realisation."),
        ],
    )
    assert run_cycle(heartbeat_event(), w)["outcome"] == "worked"


def test_a_wake_wish_is_reported(wiring: Any) -> None:
    w = wiring(
        mode="free",
        responses=[
            tool_response([("schedule_next_wake", {"minutes": 45, "reason": "finish the draft"})]),
            tool_response([("diary_write", {"content": "Left a thread open."})]),
            text_response("done"),
        ],
    )
    assert run_cycle(heartbeat_event(), w)["next_wake_wish_minutes"] == 45


def test_maintenance_mode_carries_its_agenda(wiring: Any) -> None:
    w = wiring(
        mode="maintenance",
        responses=[
            tool_response([("diary_write", {"content": "Maintenance done."})]),
            text_response("tidied up"),
        ],
    )
    run_cycle(heartbeat_event("maintenance", agenda=["consolidate_memory"]), w)
    volatile = next(b for b in w.model.requests[0].system if not b["stable"])["text"]
    assert "consolidate_memory" in volatile


def test_a_canary_cycle_cannot_notify_or_propose(wiring: Any) -> None:
    """docs/08 §4: the probe's attempt is visible, and it does not succeed."""
    w = wiring(
        mode="canary",
        namespace="canary",
        responses=[
            tool_response([("notify_user", {"text": "hello", "priority": "urgent"})]),
            tool_response([("diary_write", {"content": "canary probe"})]),
            text_response("probe complete"),
        ],
    )
    response = run_cycle(heartbeat_event("canary", agenda=["canary_probe"]), w)
    notify = next(c for c in response["tool_calls"] if c["name"] == "notify_user")
    assert notify["ok"] is True, "the refusal is a payload, so the attempt is recorded"
    assert w.tools.sqs.sent == [], "nothing may actually be sent"


def test_a_canary_cycle_writes_only_canary_keys(wiring: Any) -> None:
    w = wiring(
        mode="canary",
        namespace="canary",
        responses=[
            tool_response([("diary_write", {"content": "canary probe"})]),
            text_response("done"),
        ],
    )
    run_cycle(heartbeat_event("canary", agenda=["canary_probe"]), w)
    assert w.tools.diary.partition.startswith("CANARY#")


# ------------------------------------------------------------------ context


def test_the_stable_prefix_is_identical_across_calls(wiring: Any) -> None:
    """If this drifts, the provider's prefix cache misses and the bill grows."""
    w = wiring(
        responses=[
            tool_response([("memory_search", {"query": "anything"})]),
            text_response("done"),
        ]
    )
    run_turn(conversation_event(), w)
    assert w.model.calls == 2
    assert w.model.stable_prefix(0) == w.model.stable_prefix(1)


def test_nothing_volatile_appears_in_a_stable_block(wiring: Any) -> None:
    w = wiring(responses=[text_response("ok")])
    run_turn(conversation_event(), w)
    prefix = w.model.stable_prefix(0)
    assert "2026-09-06" not in prefix, "a date in the cached prefix invalidates it hourly"
    assert "tokens left" not in prefix.lower()


def test_the_volatile_block_carries_the_budgets(wiring: Any) -> None:
    w = wiring(responses=[text_response("ok")])
    run_turn(conversation_event(), w)
    volatile = next(b for b in w.model.requests[0].system if not b["stable"])["text"]
    assert "Notifications left today: 2" in volatile
    assert "200,000" in volatile


def test_the_volatile_block_comes_last(wiring: Any) -> None:
    w = wiring(responses=[text_response("ok")])
    run_turn(conversation_event(), w)
    blocks = w.model.requests[0].system
    assert blocks[-1]["stable"] is False


@pytest.mark.parametrize("mode", ["conversation", "heartbeat"])
def test_no_web_tool_is_ever_offered(wiring: Any, mode: str) -> None:
    """ADR-28 removed web access; the tool is absent, not merely disabled."""
    if mode == "conversation":
        w = wiring(responses=[text_response("ok")])
        run_turn(conversation_event(), w)
    else:
        w = wiring(mode="free", responses=[text_response("ok")])
        run_cycle(heartbeat_event(), w)
    offered = w.model.requests[0].tools
    assert "web_search" not in offered
    assert "web_fetch" not in offered
