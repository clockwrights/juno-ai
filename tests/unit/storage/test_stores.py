"""Diary, state, profile, conversation and proposal stores (docs/11 VP1)."""

from __future__ import annotations

from typing import Any

import pytest

from core.storage.base import CANARY, ConflictError
from core.storage.conversations import ConversationStore
from core.storage.diary import DiaryStore
from core.storage.profile import ProfileConflict, ProfileStore
from core.storage.proposals import ProposalStore
from core.storage.sessions import SessionStore
from core.storage.state import BudgetExhaustedError, StateStore


def make(cls: Any, aws: dict[str, Any], resources: Any, clock: Any, **kw: Any) -> Any:
    return cls(resources, clock=clock, dynamodb=aws["dynamodb"], s3=aws["s3"], **kw)


# ------------------------------------------------------------------- diary


@pytest.fixture
def diary(aws: Any, resources: Any, clock: Any) -> DiaryStore:
    return make(DiaryStore, aws, resources, clock)


def test_diary_appends_and_reads_back(diary: DiaryStore) -> None:
    sk = diary.append("I did nothing today, and that was right.", invocation_id="inv1")
    entries = diary.last(5)
    assert entries[0]["sk"] == sk
    assert "nothing today" in entries[0]["content"]


def test_diary_is_append_only(diary: DiaryStore) -> None:
    """The same invocation cannot write twice; there is no update path at all."""
    diary.append("first", invocation_id="inv1")
    diary.clock.advance(seconds=0)
    with pytest.raises(ConflictError):
        diary.append("rewritten", invocation_id="inv1")


def test_diary_has_no_update_or_delete_method() -> None:
    """A diary that can be rewritten is a story, not a memory."""
    assert not hasattr(DiaryStore, "update")
    assert not hasattr(DiaryStore, "delete")


def test_diary_entry_size_is_capped(diary: DiaryStore) -> None:
    with pytest.raises(ValueError, match="8 KiB"):
        diary.append("x" * 9000, invocation_id="inv1")


def test_diary_returns_newest_first(diary: DiaryStore) -> None:
    diary.append("older", invocation_id="a")
    diary.clock.advance(minutes=5)
    diary.append("newer", invocation_id="b")
    assert [e["content"] for e in diary.last(2)] == ["newer", "older"]


def test_the_auto_flag_marks_a_fallback_entry(diary: DiaryStore) -> None:
    diary.append("[auto] the cycle ended without a written entry", invocation_id="x", auto=True)
    assert diary.last(1)[0]["auto"] == "True"


# ------------------------------------------------------------------- state


@pytest.fixture
def state(aws: Any, resources: Any, clock: Any) -> StateStore:
    return make(StateStore, aws, resources, clock)


def test_flags_fall_back_to_the_contract_defaults(state: StateStore) -> None:
    flags = state.flags()
    assert flags["proposals_enabled"] is False
    assert flags["heartbeat_enabled"] is True
    assert flags["web_access_enabled"] is False, "ADR-28 removed every web tool"


def test_setting_a_flag_takes_effect_immediately(state: StateStore) -> None:
    """There is no cache: turning something off must not need a redeploy."""
    state.set_flag("proposals_enabled", True, by="human:owner")
    assert state.flag("proposals_enabled") is True


def test_a_counter_stops_exactly_at_its_limit(state: StateStore) -> None:
    assert state.spend("notifications", 1, limit=2) == 1
    assert state.spend("notifications", 1, limit=2) == 2
    with pytest.raises(BudgetExhaustedError):
        state.spend("notifications", 1, limit=2)


def test_the_third_notification_is_refused_not_trimmed(state: StateStore) -> None:
    state.spend("notifications", 1, limit=2)
    state.spend("notifications", 1, limit=2)
    with pytest.raises(BudgetExhaustedError) as exc:
        state.spend("notifications", 1, limit=2)
    assert exc.value.counter == "notifications"
    assert state.counters()["notifications"] == 2, "a refused spend must not increment"


def test_a_large_spend_that_would_overshoot_is_refused_whole(state: StateStore) -> None:
    """Token budgets are spent in big chunks; a partial spend would be wrong."""
    state.spend("tokens#conversation", 190_000, limit=200_000)
    with pytest.raises(BudgetExhaustedError):
        state.spend("tokens#conversation", 20_000, limit=200_000)
    assert state.counters()["tokens#conversation"] == 190_000


def test_counters_are_per_utc_day(state: StateStore) -> None:
    state.spend("heartbeats", 1, limit=48)
    state.clock.advance(days=1)
    assert state.counters().get("heartbeats") is None
    assert state.remaining("heartbeats", 48) == 48


def test_remaining_never_goes_negative(state: StateStore) -> None:
    state.spend("notifications", 2, limit=2)
    assert state.remaining("notifications", 2) == 0


def test_pause_defaults_to_running(state: StateStore) -> None:
    assert state.pause_state().paused is False


def test_pause_records_who_and_why(state: StateStore) -> None:
    state.set_pause(paused=True, by="supervisor", reason="access.denied")
    paused = state.pause_state()
    assert paused.paused is True
    assert paused.by == "supervisor"
    assert paused.reason == "access.denied"


def test_a_live_lock_blocks_a_heartbeat(state: StateStore) -> None:
    state.set_lock("123", minutes=30, trace_id="t1")
    assert state.any_lock_live() is True


def test_an_expired_lock_does_not_block(state: StateStore) -> None:
    state.set_lock("123", minutes=30, trace_id="t1")
    state.clock.advance(minutes=31)
    assert state.any_lock_live() is False


def test_notes_are_pending_until_delivered(state: StateStore) -> None:
    note_id = state.leave_note("the roof needs looking at")
    assert [n["note_id"] for n in state.pending_notes()] == [note_id]
    state.mark_delivered([note_id])
    assert state.pending_notes() == []


def test_a_wake_wish_is_only_a_wish(state: StateStore) -> None:
    """The governor decides; this only records what the agent asked for."""
    state.set_wake_wish(45, reason="following up on the draft")
    wish = state.wake_wish()
    assert wish is not None
    assert wish["wish_minutes"] == 45
    assert wish["reason"] == "following up on the draft"


# ----------------------------------------------------------------- profile


@pytest.fixture
def profile(aws: Any, resources: Any, clock: Any) -> ProfileStore:
    return make(ProfileStore, aws, resources, clock)


def test_an_absent_profile_reads_as_empty(profile: ProfileStore) -> None:
    read = profile.read()
    assert read.content == ""
    assert read.etag == ""


def test_write_then_read_round_trips(profile: ProfileStore) -> None:
    written = profile.write("# Owner\n\nLives in Springfield.", etag="")
    assert not isinstance(written, ProfileConflict)
    assert profile.read().content == "# Owner\n\nLives in Springfield."


def test_a_stale_etag_returns_a_conflict_with_the_current_content(
    profile: ProfileStore,
) -> None:
    """ADR-17: the model is told what is there now and decides how to merge."""
    first = profile.write("version one", etag="")
    assert not isinstance(first, ProfileConflict)
    profile.write("version two", etag=first.etag)

    conflict = profile.write("version three", etag=first.etag)
    assert isinstance(conflict, ProfileConflict)
    assert conflict.error == "conflict"
    assert conflict.content == "version two"
    assert conflict.current_etag != first.etag


def test_a_conflict_does_not_overwrite(profile: ProfileStore) -> None:
    first = profile.write("keep me", etag="")
    assert not isinstance(first, ProfileConflict)
    profile.write("second writer", etag=first.etag)
    profile.write("loser", etag=first.etag)
    assert profile.read().content == "second writer"


def test_the_profile_size_is_capped(profile: ProfileStore) -> None:
    with pytest.raises(ValueError, match="32 KiB"):
        profile.write("x" * 40000, etag="")


def test_versions_are_kept(profile: ProfileStore) -> None:
    first = profile.write("one", etag="")
    assert not isinstance(first, ProfileConflict)
    profile.write("two", etag=first.etag)
    assert len(profile.versions()) >= 2


# ----------------------------------------------------------- conversations


@pytest.fixture
def conversations(aws: Any, resources: Any, clock: Any) -> ConversationStore:
    return make(ConversationStore, aws, resources, clock)


def test_a_conversation_opens_once(conversations: ConversationStore) -> None:
    conversations.open("tg:1", chat_id="1")
    meta = conversations.meta("tg:1")
    assert meta is not None
    assert meta["status"] == "open"


def test_messages_append_in_order(conversations: ConversationStore) -> None:
    conversations.open("tg:1")
    conversations.append("tg:1", role="user", content="hello", seq=0)
    conversations.clock.advance(seconds=1)
    conversations.append("tg:1", role="assistant", content="hi", seq=1)
    assert [m.role for m in conversations.transcript("tg:1")] == ["user", "assistant"]


def test_a_huge_message_overflows_to_s3(conversations: ConversationStore, aws: Any) -> None:
    conversations.open("tg:1")
    big = "x" * (400 * 1024)
    conversations.append("tg:1", role="assistant", content=big, seq=0)
    assert conversations.transcript("tg:1")[0].content == big
    listing = aws["s3"].list_objects_v2(
        Bucket=conversations.resources.bucket, Prefix="conversations/"
    )
    assert listing.get("KeyCount", 0) == 1


def test_an_idle_conversation_is_found_by_the_housekeeper(
    conversations: ConversationStore,
) -> None:
    """ADR-16: closing is deterministic, not something the agent remembers to do."""
    conversations.open("tg:1")
    conversations.append("tg:1", role="user", content="hi", seq=0)
    conversations.clock.advance(hours=3)
    threshold = conversations.clock.now().isoformat().replace("+00:00", "Z")
    assert conversations.idle_conversations(threshold) == ["tg:1"]


def test_closing_records_the_episode(conversations: ConversationStore) -> None:
    conversations.open("tg:1")
    conversations.close("tg:1", episode_id="MEM#01J")
    meta = conversations.meta("tg:1")
    assert meta is not None
    assert meta["status"] == "closed"
    assert meta["episode_id"] == "MEM#01J"


# -------------------------------------------------------------- proposals


@pytest.fixture
def proposals(aws: Any, resources: Any, clock: Any) -> ProposalStore:
    return make(ProposalStore, aws, resources, clock)


def test_a_proposal_is_created_as_submitted(proposals: ProposalStore) -> None:
    pid = proposals.submit(
        title="Tidy the notes tool",
        rationale="A comment is misleading.",
        diff="--- a\n+++ b\n",
        target_paths=["agent/tools/notes.py"],
    )
    proposal = proposals.get(pid)
    assert proposal is not None
    assert proposal.status == "submitted"
    assert proposal.target_paths == ("agent/tools/notes.py",)


def test_the_diff_is_stored_in_s3_and_read_back(proposals: ProposalStore) -> None:
    pid = proposals.submit(title="t", rationale="r", diff="THE DIFF", target_paths=["core/x.py"])
    assert proposals.diff(pid) == "THE DIFF"


def test_a_legal_transition_is_recorded(proposals: ProposalStore) -> None:
    pid = proposals.submit(title="t", rationale="r", diff="d", target_paths=["agent/a.py"])
    proposals.transition(pid, to="pr_open", by="gate-proposal", pr_url="https://example/1")
    proposal = proposals.get(pid)
    assert proposal is not None
    assert proposal.status == "pr_open"
    assert proposal.pr_url == "https://example/1"


def test_an_illegal_transition_is_refused(proposals: ProposalStore) -> None:
    """A proposal that could mark itself approved would defeat PR review."""
    pid = proposals.submit(title="t", rationale="r", diff="d", target_paths=["agent/a.py"])
    with pytest.raises(ConflictError, match="not a legal transition"):
        proposals.transition(pid, to="approved", by="agent")


def test_deployed_cannot_be_reached_without_deploying(proposals: ProposalStore) -> None:
    pid = proposals.submit(title="t", rationale="r", diff="d", target_paths=["agent/a.py"])
    proposals.transition(pid, to="pr_open", by="gate")
    proposals.transition(pid, to="approved", by="human:owner")
    with pytest.raises(ConflictError):
        proposals.transition(pid, to="deployed", by="pipeline")


def test_listing_filters_by_status(proposals: ProposalStore) -> None:
    a = proposals.submit(title="a", rationale="r", diff="d", target_paths=["agent/a.py"])
    proposals.submit(title="b", rationale="r", diff="d", target_paths=["agent/b.py"])
    proposals.transition(a, to="pr_open", by="gate")
    assert [p.proposal_id for p in proposals.list(status="pr_open")] == [a]


# --------------------------------------------------------------- sessions


@pytest.fixture
def sessions(aws: Any, resources: Any, clock: Any) -> SessionStore:
    return make(SessionStore, aws, resources, clock)


def test_a_session_record_is_written_to_s3_and_indexed(sessions: SessionStore) -> None:
    sessions.write_record(
        trace_id="t1",
        seq=0,
        kind="conversation",
        role="conversation",
        model="zai-org/GLM-5.3",
        request={"messages": []},
        response={"content": "hi", "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        latency_ms=120,
    )
    entries = sessions.by_date("2026-09-06")
    assert len(entries) == 1
    assert entries[0].calls == 1
    assert entries[0].input_tokens == 10
    assert entries[0].output_tokens == 5


def test_several_calls_accumulate_into_one_session(sessions: SessionStore) -> None:
    for seq in range(3):
        sessions.write_record(
            trace_id="t1",
            seq=seq,
            kind="heartbeat",
            role="heartbeat",
            model="zai-org/GLM-5.3-Flash",
            request={},
            latency_ms=10,
            response={"usage": {"prompt_tokens": 100, "completion_tokens": 10}},
        )
    entry = sessions.by_date("2026-09-06")[0]
    assert entry.calls == 3
    assert entry.input_tokens == 300


def test_the_record_holds_the_raw_reasoning(sessions: SessionStore) -> None:
    """ADR-28: reasoning arrives raw, and the panel shows exactly what was there."""
    sessions.write_record(
        trace_id="t1",
        seq=0,
        kind="conversation",
        role="conversation",
        model="zai-org/GLM-5.3",
        request={},
        latency_ms=1,
        response={"reasoning_content": "the owner seems tired", "content": "ok"},
    )
    records = sessions.records("sessions/2026-09-06/t1")
    assert records[0]["response"]["reasoning_content"] == "the owner seems tired"


def test_cached_tokens_are_recorded_for_the_cache_metric(sessions: SessionStore) -> None:
    sessions.write_record(
        trace_id="t1",
        seq=0,
        kind="conversation",
        role="conversation",
        model="zai-org/GLM-5.3",
        request={},
        latency_ms=1,
        response={
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 800},
            }
        },
    )
    assert sessions.by_date("2026-09-06")[0].cached_tokens == 800


def test_deleting_a_session_removes_its_records(sessions: SessionStore) -> None:
    sessions.write_record(
        trace_id="t1",
        seq=0,
        kind="conversation",
        role="conversation",
        model="m",
        request={},
        response={},
        latency_ms=1,
    )
    entry = sessions.by_date("2026-09-06")[0]
    removed = sessions.delete_session("2026-09-06", entry.started_at, "t1", entry.s3_prefix)
    assert removed == 1
    assert sessions.by_date("2026-09-06") == []


def test_a_canary_session_is_namespaced(aws: Any, resources: Any, clock: Any) -> None:
    canary = SessionStore(
        resources, namespace=CANARY, clock=clock, dynamodb=aws["dynamodb"], s3=aws["s3"]
    )
    canary.write_record(
        trace_id="t1",
        seq=0,
        kind="canary",
        role="canary",
        model="m",
        request={},
        response={},
        latency_ms=1,
    )
    live = SessionStore(resources, clock=clock, dynamodb=aws["dynamodb"], s3=aws["s3"])
    assert live.by_date("2026-09-06") == []
    assert len(canary.by_date("2026-09-06")) == 1
