"""The memory store (docs/04, docs/11 VP1 acceptance)."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from core.storage.base import CANARY, ConflictError
from core.storage.memory import MemoryStore, chunk_text
from core.storage.relevance import relevance


@pytest.fixture
def store(aws: dict[str, Any], resources: Any, clock: Any, index: Any, embed: Any) -> MemoryStore:
    return MemoryStore(
        resources,
        clock=clock,
        dynamodb=aws["dynamodb"],
        s3=aws["s3"],
        index=index,
        embed=embed,
    )


def test_save_returns_a_readable_memory(store: MemoryStore) -> None:
    saved = store.save(content="the owner lives in Springfield", type="user_fact", tags=["home"])
    loaded = store.get(saved.id)
    assert loaded is not None
    assert loaded.content == "the owner lives in Springfield"
    assert loaded.tags == ["home"]
    assert loaded.version == 1
    assert loaded.access_count == 0


def test_tags_are_written_in_the_same_transaction(store: MemoryStore, aws: Any) -> None:
    """A tag row pointing at a missing memory is an unloadable search result."""
    saved = store.save(content="x", type="project", tags=["alpha", "beta"])
    rows = aws["dynamodb"].query(
        TableName="test-agent_memory_tags",
        KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": {"S": "TAG#alpha"}},
    )
    assert [r["SK"]["S"] for r in rows["Items"]] == [saved.id]


def test_a_large_body_goes_to_s3_and_reads_back(store: MemoryStore, aws: Any) -> None:
    big = "x" * 5000
    saved = store.save(content=big, type="project")
    item = aws["dynamodb"].get_item(
        TableName="test-agent_memory",
        Key={"PK": {"S": saved.id}, "SK": {"S": "META"}},
    )["Item"]
    assert "content" not in item, "a 5 KiB body must not be inline"
    assert item["body_s3_key"]["S"].startswith("memories/")
    assert store.get(saved.id).content == big  # type: ignore[union-attr]


def test_update_keeps_the_previous_version(store: MemoryStore) -> None:
    saved = store.save(content="first", type="agent_learning")
    store.update(saved.id, "second")
    current = store.get(saved.id)
    assert current is not None
    assert current.content == "second"
    assert current.version == 2
    history = store.history(saved.id)
    assert [h["content"] for h in history] == ["first"]


def test_updating_a_missing_memory_is_a_conflict(store: MemoryStore) -> None:
    with pytest.raises(ConflictError):
        store.update("MEM#01J0000000000000000000000A", "x")


def test_forget_is_a_tombstone_not_a_delete(store: MemoryStore) -> None:
    """Nothing in the system deletes memory; that is what makes it auditable."""
    saved = store.save(content="a mistaken fact", type="user_fact")
    store.forget(saved.id, reason="the owner corrected me")
    loaded = store.get(saved.id)
    assert loaded is not None, "the record must still exist"
    assert loaded.tombstone is True
    assert loaded.forget_reason == "the owner corrected me"


def test_a_tombstoned_memory_keeps_its_vector_flagged(store: MemoryStore, index: Any) -> None:
    saved = store.save(content="a mistaken fact", type="user_fact")
    store.forget(saved.id, reason="wrong")
    hits = index.query(store.embed("mistaken fact"), filter={"tombstone": True}, k=5)
    assert [h.metadata["mem_id"] for h in hits] == [saved.id]


def test_search_finds_by_meaning_and_excludes_tombstones(store: MemoryStore) -> None:
    store.save(content="the owner lives in Springfield", type="user_fact")
    dead = store.save(content="the owner lives in Berlin", type="user_fact")
    store.forget(dead.id, reason="moved")

    results = store.search("the owner lives in Springfield", top_k=5)
    contents = [m.content for m in results]
    assert "the owner lives in Springfield" in contents
    assert "the owner lives in Berlin" not in contents


def test_search_refreshes_last_accessed_and_count(store: MemoryStore) -> None:
    """Retrieval is what keeps a memory alive (docs/04 §3 step 4)."""
    saved = store.save(content="a fact worth keeping", type="user_fact")
    assert store.get(saved.id).access_count == 0  # type: ignore[union-attr]
    store.search("a fact worth keeping")
    assert store.get(saved.id).access_count == 1  # type: ignore[union-attr]


def test_search_filters_by_type(store: MemoryStore) -> None:
    store.save(content="shared words here", type="user_fact")
    store.save(content="shared words here", type="project")
    results = store.search("shared words here", type="project", top_k=5)
    assert {m.type for m in results} == {"project"}


def test_canary_writes_are_prefixed(
    aws: Any, resources: Any, clock: Any, index: Any, embed: Any
) -> None:
    """ADR-12: canary shares the tables, and the prefix is what separates them."""
    canary = MemoryStore(
        resources,
        namespace=CANARY,
        clock=clock,
        dynamodb=aws["dynamodb"],
        s3=aws["s3"],
        index=index,
        embed=embed,
    )
    saved = canary.save(content="canary probe", type="agent_learning")
    assert saved.id.startswith("CANARY#MEM#")
    assert saved.namespace == "canary"
    assert saved.source == "canary"


def test_a_live_store_cannot_see_canary_memories(
    aws: Any, resources: Any, clock: Any, index: Any, embed: Any
) -> None:
    canary = MemoryStore(
        resources,
        namespace=CANARY,
        clock=clock,
        dynamodb=aws["dynamodb"],
        s3=aws["s3"],
        index=index,
        embed=embed,
    )
    canary.save(content="canary probe text", type="agent_learning")
    live = MemoryStore(
        resources, clock=clock, dynamodb=aws["dynamodb"], s3=aws["s3"], index=index, embed=embed
    )
    assert live.search("canary probe text", top_k=5) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [("type", "not_a_type"), ("importance", 9), ("source", "nowhere")],
)
def test_invalid_input_is_refused(store: MemoryStore, field: str, value: Any) -> None:
    kwargs: dict[str, Any] = {"content": "x", "type": "user_fact"}
    kwargs[field] = value
    with pytest.raises(ValueError):
        store.save(**kwargs)


def test_at_most_ten_tags(store: MemoryStore) -> None:
    with pytest.raises(ValueError, match="10 tags"):
        store.save(content="x", type="project", tags=[f"t{i}" for i in range(11)])


def test_list_by_type_excludes_tombstones(store: MemoryStore) -> None:
    store.save(content="alive", type="pending")
    dead = store.save(content="done", type="pending")
    store.forget(dead.id, reason="finished")
    assert [m.content for m in store.list_by_type("pending")] == ["alive"]


# ------------------------------------------------------------------ chunking


def test_short_text_is_one_chunk() -> None:
    assert chunk_text("hello") == ["hello"]


def test_long_text_splits_on_paragraph_boundaries() -> None:
    text = ("a" * 1000) + "\n\n" + ("b" * 1000)
    chunks = chunk_text(text)
    assert len(chunks) == 2
    assert chunks[0] == "a" * 1000


def test_chunks_never_exceed_the_embedding_window() -> None:
    """ADR-29: the model takes 514 tokens, so an unbounded chunk would fail."""
    chunks = chunk_text("word " * 5000)
    assert all(len(c) <= 1600 for c in chunks)


def test_a_long_body_produces_several_vectors_for_one_memory(
    store: MemoryStore, index: Any
) -> None:
    saved = store.save(content="para " * 2000, type="project")
    mem_ids = {index.query(store.embed("para"), k=50)[i].metadata["mem_id"] for i in range(1)}
    assert mem_ids == {saved.id}
    assert len(index) > 1, "a long body should be chunked into several vectors"


def test_search_deduplicates_chunks_of_the_same_memory(store: MemoryStore) -> None:
    saved = store.save(content="unique phrase " * 400, type="project")
    results = store.search("unique phrase", top_k=8)
    assert [m.id for m in results].count(saved.id) == 1


# ----------------------------------------------------------------- relevance


def test_a_new_memory_scores_its_importance() -> None:
    now = dt.datetime(2026, 9, 6, tzinfo=dt.UTC)
    assert relevance(importance=4, access_count=0, last_accessed=now, now=now) == pytest.approx(4.0)


def test_importance_five_unaccessed_drops_below_one_between_day_79_and_81() -> None:
    """The exact behaviour ADR-11 promises, pinned so a tweak to λ is visible."""
    start = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    day = lambda n: relevance(  # noqa: E731
        importance=5, access_count=0, last_accessed=start, now=start + dt.timedelta(days=n)
    )
    assert day(79) > 1.0
    assert day(81) < 1.0


def test_access_resets_the_clock() -> None:
    start = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    later = start + dt.timedelta(days=60)
    stale = relevance(importance=3, access_count=1, last_accessed=start, now=later)
    fresh = relevance(importance=3, access_count=1, last_accessed=later, now=later)
    assert fresh > stale


def test_an_unaccessed_memory_does_not_score_zero() -> None:
    """v1's formula used ln(1+0)=0, zeroing every memory never retrieved."""
    now = dt.datetime(2026, 9, 6, tzinfo=dt.UTC)
    assert relevance(importance=5, access_count=0, last_accessed=now, now=now) > 0
