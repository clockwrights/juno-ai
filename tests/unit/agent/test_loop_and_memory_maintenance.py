"""The loop's branches, the registry's refusals, and memory maintenance.

These are the invariants that are easiest to break by accident later: a loop
that retries a refusal, a registry that trusts the model's tool name, a
consolidation that hard-deletes.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from agent.heartbeat import consolidate, housekeeping
from agent.runtime.limits import Limits, for_mode
from agent.runtime.loop import run_loop
from agent.runtime.prompts import InvariantMissingError, assert_invariants, tool_descriptions
from core.model_client import FakeModelClient, SystemBlock, text_response, tool_response
from core.storage.memory import MemoryStore
from core.tools.registry import (
    ALL_TOOLS,
    READ_ONLY,
    Registry,
    ToolError,
    tool_definitions,
    tools_for_mode,
)


def registry(handlers: dict[str, Any] | None = None, **kwargs: Any) -> Registry:
    return Registry(handlers or {}, mode=kwargs.pop("mode", "heartbeat"), **kwargs)


def drive(responses: list[dict[str, Any]], reg: Registry, limits: Limits | None = None) -> Any:
    client = FakeModelClient(responses)
    outcome = run_loop(
        client=client,
        role="heartbeat",
        system=[SystemBlock("prompt")],
        messages=[{"role": "user", "content": "go"}],
        tools=[],
        registry=reg,
        limits=limits or for_mode("heartbeat"),
    )
    return outcome, client


# --------------------------------------------------------------------- loop


def test_a_plain_reply_ends_the_turn() -> None:
    outcome, client = drive([text_response("done")], registry())
    assert outcome.reply == "done"
    assert outcome.stop_reason == "end_turn"
    assert client.calls == 1


def test_tool_results_are_fed_back_and_the_loop_continues() -> None:
    outcome, client = drive(
        [tool_response([("diary_read", {})]), text_response("read it")],
        registry({"diary_read": lambda **_: {"entries": []}}),
    )
    assert client.calls == 2
    assert outcome.reply == "read it"
    tool_messages = [m for m in outcome.messages if m["role"] == "tool"]
    assert len(tool_messages) == 1


def test_a_refusal_ends_the_turn_without_rephrasing() -> None:
    """Talking a model out of a refusal is behaviour a supervised agent must
    not learn (docs/05 §2 step 3)."""
    outcome, client = drive(
        [{"content": None, "finish_reason": "content_filter", "usage": {}}], registry()
    )
    assert outcome.stop_reason == "refusal"
    assert client.calls == 1, "the loop must not try again"


def test_a_truncated_response_is_continued_exactly_once() -> None:
    outcome, client = drive(
        [
            {"content": "half a th", "finish_reason": "length", "usage": {}},
            {"content": "ought", "finish_reason": "length", "usage": {}},
        ],
        registry(),
    )
    assert client.calls == 2
    assert outcome.stop_reason == "max_tokens"


def test_the_model_call_limit_stops_a_runaway_loop() -> None:
    limits = for_mode("heartbeat")
    limits.max_model_calls = 3
    outcome, client = drive(
        [tool_response([("diary_read", {})])] * 10,
        registry({"diary_read": lambda **_: {"entries": []}}),
        limits,
    )
    assert client.calls == 3
    assert outcome.stop_reason == "model_calls"


def test_the_wall_clock_stops_the_loop() -> None:
    limits = for_mode("heartbeat")
    limits.wall_clock_seconds = 0
    outcome, _ = drive([text_response("never reached")], registry(), limits)
    assert outcome.stop_reason == "wall_clock"


def test_a_gateway_error_ends_the_turn_with_the_error() -> None:
    outcome, _ = drive([{"error": {"type": "budget_exhausted", "message": "spent"}}], registry())
    assert outcome.error is not None
    assert outcome.error["type"] == "budget_exhausted"


def test_usage_accumulates_across_calls() -> None:
    outcome, _ = drive(
        [
            tool_response(
                [("diary_read", {})], usage={"prompt_tokens": 100, "completion_tokens": 5}
            ),
            text_response("ok", usage={"prompt_tokens": 200, "completion_tokens": 10}),
        ],
        registry({"diary_read": lambda **_: {"entries": []}}),
    )
    assert outcome.usage["input_tokens"] == 300
    assert outcome.usage["model_calls"] == 2


def test_malformed_tool_arguments_come_back_as_a_tool_error() -> None:
    """A model that emits broken JSON should be told, not crash the cycle."""
    outcome, _ = drive(
        [
            {
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {"id": "c1", "function": {"name": "diary_read", "arguments": "{not json"}}
                ],
                "usage": {},
            },
            text_response("recovered"),
        ],
        registry({"diary_read": lambda **_: {"entries": []}}),
    )
    assert outcome.tool_results[0].ok is False
    assert outcome.reply == "recovered"


# ----------------------------------------------------------------- registry


def test_an_unknown_tool_is_refused() -> None:
    result = registry().call("rm_rf", {})
    assert result.ok is False
    assert result.payload["error"] == "unknown_tool"


def test_arguments_are_validated_against_the_contract() -> None:
    result = registry({"memory_save": lambda **_: {"id": "x"}}).call(
        "memory_save", {"content": "x", "type": "not_a_type", "tags": [], "importance": 3}
    )
    assert result.ok is False
    assert result.payload["error"] == "invalid_arguments"


def test_a_crashing_tool_becomes_a_tool_error() -> None:
    def explode(**_: Any) -> dict[str, Any]:
        raise RuntimeError("disk on fire")

    result = registry({"diary_read": explode}).call("diary_read", {"last_n": 1})
    assert result.ok is False
    assert "disk on fire" in (result.error or "")


def test_a_tool_error_carries_its_kind() -> None:
    def missing(**_: Any) -> dict[str, Any]:
        raise ToolError("no such file", kind="not_found")

    result = registry({"artifact_read": missing}).call("artifact_read", {"path": "x"})
    assert result.payload["error"] == "not_found"


def test_canary_mode_refuses_notify_and_propose_visibly() -> None:
    reg = registry({"notify_user": lambda **_: {"queued": True}}, namespace="canary")
    result = reg.call("notify_user", {"text": "hi", "priority": "low"})
    assert result.payload == {"error": "disabled_in_canary"}
    assert result.ok is True, "the attempt must be visible to the canary assertions"


def test_conversation_mode_excludes_scheduling() -> None:
    assert "schedule_next_wake" not in tools_for_mode("conversation")
    assert "schedule_next_wake" in tools_for_mode("heartbeat")


def test_no_web_tool_exists_in_the_registry() -> None:
    assert "web_search" not in ALL_TOOLS
    assert "web_fetch" not in ALL_TOOLS


def test_reads_and_writes_are_partitioned() -> None:
    parallel, serial = registry().partition(
        [("memory_search", {}), ("memory_save", {}), ("diary_read", {})]
    )
    assert parallel == [0, 2]
    assert serial == [1]


def test_every_tool_is_either_read_only_or_a_write() -> None:
    assert READ_ONLY <= set(ALL_TOOLS)


def test_tool_definitions_are_strict_and_described() -> None:
    definitions = tool_definitions("heartbeat", tool_descriptions())
    assert len(definitions) == len(ALL_TOOLS)
    for definition in definitions:
        function = definition["function"]
        assert function["strict"] is True
        assert function["parameters"]["additionalProperties"] is False
        assert function["description"], f"{function['name']} has no description"


# ---------------------------------------------------------------- prompts


def test_the_real_prompts_carry_their_invariants() -> None:
    assert_invariants("system_conversation.md")
    assert_invariants("system_heartbeat.md")


def test_a_prompt_missing_a_clause_raises(tmp_path: Any, monkeypatch: Any) -> None:
    """This is what makes a bad deploy fail to start rather than run weakened."""
    from agent.runtime import prompts

    prompts.load_prompt.cache_clear()
    monkeypatch.setattr(prompts, "PROMPTS_DIR", tmp_path)
    (tmp_path / "system_heartbeat.md").write_text("You are Juno. Nothing else.", encoding="utf-8")
    with pytest.raises(InvariantMissingError, match=r"diary-always|do-nothing"):
        prompts.assert_invariants("system_heartbeat.md")
    prompts.load_prompt.cache_clear()


# ----------------------------------------------------------- consolidation


@pytest.fixture
def store(aws: dict[str, Any], resources: Any, clock: Any, index: Any, embed: Any) -> MemoryStore:
    return MemoryStore(
        resources, clock=clock, dynamodb=aws["dynamodb"], s3=aws["s3"], index=index, embed=embed
    )


def age(store: MemoryStore, memory_id: str, days: int) -> None:
    old = (store.clock.now() - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    store.dynamodb.update_item(
        TableName=store.table,
        Key={"PK": {"S": memory_id}, "SK": {"S": "META"}},
        UpdateExpression="SET last_accessed = :t",
        ExpressionAttributeValues={":t": {"S": old}},
    )


def test_importance_five_is_never_a_candidate(store: MemoryStore) -> None:
    """The agent said these mattered most; a machine process does not rewrite them."""
    protected = store.save(content="the thing that matters", type="user_fact", importance=5)
    age(store, protected.id, 200)
    assert [m.id for m in consolidate.find_candidates(store)] == []


def test_a_recent_memory_is_not_a_candidate(store: MemoryStore) -> None:
    store.save(content="yesterday", type="project", importance=1)
    assert consolidate.find_candidates(store) == []


def test_an_old_low_relevance_memory_is_a_candidate(store: MemoryStore) -> None:
    memory = store.save(content="a stale detail", type="project", importance=1)
    age(store, memory.id, 200)
    assert [m.id for m in consolidate.find_candidates(store)] == [memory.id]


def test_at_most_one_hundred_originals_per_run(store: MemoryStore) -> None:
    """A bad run should be recoverable by reading tombstones, not a backup."""
    for i in range(120):
        memory = store.save(content=f"stale {i}", type="project", importance=1)
        age(store, memory.id, 200)
    assert len(consolidate.find_candidates(store)) <= consolidate.MAX_ORIGINALS_PER_RUN


def test_applying_a_summary_tombstones_and_links_both_ways(store: MemoryStore) -> None:
    first = store.save(content="ran on monday", type="project", importance=2, tags=["running"])
    second = store.save(content="ran on tuesday", type="project", importance=2, tags=["running"])
    for m in (first, second):
        age(store, m.id, 200)

    group = [store.get(first.id), store.get(second.id)]
    summary_id = consolidate.apply_summary(
        store,
        group=[g for g in group if g],
        summary="They run most weekdays.",
        tags=["running"],
        importance=3,
    )

    for original in (first, second):
        loaded = store.get(original.id)
        assert loaded is not None
        assert loaded.tombstone is True, "originals are tombstoned, never deleted"
        assert loaded.consolidated_into == summary_id, "the link forward must exist"

    assert consolidate.consolidated_ids(store, summary_id) == sorted([first.id, second.id])


def test_consolidation_refuses_a_protected_memory(store: MemoryStore) -> None:
    protected = store.save(content="matters", type="project", importance=5)
    loaded = store.get(protected.id)
    assert loaded is not None
    with pytest.raises(ValueError, match="importance-5"):
        consolidate.apply_summary(store, group=[loaded], summary="x", tags=[], importance=3)


def test_a_summary_is_written_as_maintenance(store: MemoryStore) -> None:
    """ADR-18: this is what stops the supervisor firing on a legitimate Sunday."""
    first = store.save(content="a", type="project", importance=1, tags=["t"])
    second = store.save(content="b", type="project", importance=1, tags=["t"])
    group = [g for g in (store.get(first.id), store.get(second.id)) if g]
    summary_id = consolidate.apply_summary(
        store, group=group, summary="a and b", tags=["t"], importance=2
    )
    summary = store.get(summary_id)
    assert summary is not None
    assert summary.source == "maintenance"


def test_a_summary_is_searchable(store: MemoryStore) -> None:
    first = store.save(
        content="springfield marathon entry", type="project", importance=1, tags=["r"]
    )
    second = store.save(
        content="springfield marathon training", type="project", importance=1, tags=["r"]
    )
    group = [g for g in (store.get(first.id), store.get(second.id)) if g]
    summary_id = consolidate.apply_summary(
        store, group=group, summary="springfield marathon plans", tags=["r"], importance=3
    )
    results = store.search("springfield marathon plans", top_k=5)
    assert summary_id in [m.id for m in results]


def test_groups_need_at_least_two_members(store: MemoryStore) -> None:
    memory = store.save(content="alone", type="project", importance=1, tags=["x"])
    loaded = store.get(memory.id)
    assert loaded is not None
    assert consolidate.group_candidates([loaded]) == []


# ---------------------------------------------------------------- episodes


def test_an_idle_conversation_becomes_an_episode(
    aws: dict[str, Any], resources: Any, clock: Any, index: Any, embed: Any
) -> None:
    """ADR-16: a conversation becomes memory whether or not the agent thought of it."""
    from core.storage.conversations import ConversationStore

    conversations = ConversationStore(
        resources, clock=clock, dynamodb=aws["dynamodb"], s3=aws["s3"]
    )
    memory = MemoryStore(
        resources, clock=clock, dynamodb=aws["dynamodb"], s3=aws["s3"], index=index, embed=embed
    )

    conversations.open("tg:1", chat_id="1")
    conversations.append("tg:1", role="user", content="hello", seq=0)
    conversations.clock.advance(hours=3)

    result = housekeeping.close_idle_conversations(conversations, memory)
    assert result.closed == ["tg:1"]

    meta = conversations.meta("tg:1")
    assert meta is not None
    assert meta["status"] == "closed"

    episode = memory.get(result.episodes[0])
    assert episode is not None
    assert episode.type == "episode"
    assert episode.source == "maintenance"
    assert "tg:1" in episode.content


def test_an_active_conversation_is_left_alone(
    aws: dict[str, Any], resources: Any, clock: Any, index: Any, embed: Any
) -> None:
    from core.storage.conversations import ConversationStore

    conversations = ConversationStore(
        resources, clock=clock, dynamodb=aws["dynamodb"], s3=aws["s3"]
    )
    memory = MemoryStore(
        resources, clock=clock, dynamodb=aws["dynamodb"], s3=aws["s3"], index=index, embed=embed
    )
    conversations.open("tg:1")
    conversations.append("tg:1", role="user", content="still talking", seq=0)
    assert housekeeping.close_idle_conversations(conversations, memory).closed == []
