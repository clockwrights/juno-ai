"""Consolidation: turning many old memories into fewer better ones (docs/04 §5).

This is the only place the agent rewrites its own past, so the invariants are
strict and enforced in code rather than by the prompt:

- nothing is ever hard-deleted; originals are tombstoned and linked forward,
- every summary links back to the originals it replaces, so the chain is
  followable in both directions,
- importance-5 memories are never consolidated — they are the ones the agent
  said mattered most,
- at most 100 originals per run, so a bad run is bounded,
- everything written is `source = maintenance`, which is what keeps the
  supervisor's memory-rate rule from firing on a legitimate Sunday (ADR-18).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core import logging as jlog
from core.storage.memory import Memory, MemoryStore

_log = jlog.get_logger("agent.consolidate")

#: A bad consolidation run should be recoverable by reading 100 tombstones, not
#: by restoring a backup.
MAX_ORIGINALS_PER_RUN = 100

#: Below this, a memory is a candidate for folding into a summary.
RELEVANCE_THRESHOLD = 1.0

#: And only if it has not been touched in this long.
MIN_AGE_DAYS = 45

#: The agent said these mattered most. They decay like anything else, but they
#: are never rewritten by a machine process.
PROTECTED_IMPORTANCE = 5

#: `pending` is excluded: an open task is not stale just because it is old.
MEMORY_TYPES_SCANNED = (
    "user_fact",
    "user_preference",
    "episode",
    "agent_learning",
    "project",
)


@dataclass
class ConsolidationRun:
    candidates: list[Memory] = field(default_factory=list)
    groups: list[list[Memory]] = field(default_factory=list)
    summaries: list[str] = field(default_factory=list)
    tombstoned: list[str] = field(default_factory=list)
    skipped_protected: int = 0

    @property
    def consolidated(self) -> int:
        return len(self.tombstoned)


def find_candidates(store: MemoryStore, *, limit: int = MAX_ORIGINALS_PER_RUN) -> list[Memory]:
    """Old, low-relevance, unprotected memories, oldest access first."""
    import datetime as dt

    now = store.clock.now()
    cutoff = now - dt.timedelta(days=MIN_AGE_DAYS)

    found: list[Memory] = []
    for memory_type in MEMORY_TYPES_SCANNED:
        for memory in store.list_by_type(memory_type, limit=200):
            if memory.importance >= PROTECTED_IMPORTANCE:
                continue
            if memory.tombstone or memory.consolidated_into:
                continue
            from core.contracts import parse_iso

            last = parse_iso(memory.last_accessed or memory.created_at)
            if last > cutoff:
                continue
            if memory.relevance(now) >= RELEVANCE_THRESHOLD:
                continue
            found.append(memory)

    found.sort(key=lambda m: m.last_accessed)
    return found[:limit]


def group_candidates(candidates: list[Memory]) -> list[list[Memory]]:
    """Group by type and shared tags.

    Deliberately mechanical rather than model-driven: which memories *belong*
    together is a judgement the model makes when it writes the summary, but
    which ones it is *allowed* to merge is a rule, so it stays in code.
    """
    buckets: dict[tuple[str, str], list[Memory]] = {}
    for memory in candidates:
        key = (memory.type, memory.tags[0] if memory.tags else "")
        buckets.setdefault(key, []).append(memory)
    return [group for group in buckets.values() if len(group) >= 2]


def apply_summary(
    store: MemoryStore,
    *,
    group: list[Memory],
    summary: str,
    tags: list[str],
    importance: int,
) -> str:
    """Write the summary, then tombstone and link each original.

    Order matters: the summary exists before anything is tombstoned, so an
    interrupted run leaves an extra memory rather than a dangling tombstone.
    """
    if any(m.importance >= PROTECTED_IMPORTANCE for m in group):
        raise ValueError("importance-5 memories are never consolidated")

    saved = store.save(
        content=summary,
        type=group[0].type,
        tags=sorted({t for m in group for t in m.tags} | set(tags))[:10],
        importance=min(5, max(1, importance)),
        source="maintenance",
    )

    for original in group:
        store.forget(original.id, reason=f"consolidated into {saved.id}")
        store.dynamodb.update_item(
            TableName=store.table,
            Key={"PK": {"S": original.id}, "SK": {"S": "META"}},
            UpdateExpression="SET consolidated_into = :s",
            ExpressionAttributeValues={":s": {"S": saved.id}},
        )

    jlog.info(
        _log,
        "consolidated",
        summary_id=saved.id,
        originals=len(group),
        memory_type=group[0].type,
    )
    return saved.id


def prepare(store: MemoryStore) -> ConsolidationRun:
    """Everything before the model is asked to write anything."""
    run = ConsolidationRun()
    all_old = find_candidates(store, limit=MAX_ORIGINALS_PER_RUN * 2)
    run.skipped_protected = sum(1 for m in all_old if m.importance >= PROTECTED_IMPORTANCE)
    run.candidates = all_old[:MAX_ORIGINALS_PER_RUN]
    run.groups = group_candidates(run.candidates)
    return run


def describe_groups(run: ConsolidationRun) -> str:
    """What the model is shown, so it can write summaries without a tool round-trip."""
    lines: list[str] = []
    for i, group in enumerate(run.groups):
        lines.append(f"### Group {i + 1} — {group[0].type}")
        for memory in group:
            lines.append(f"- [{memory.id}] (importance {memory.importance}) {memory.content}")
        lines.append("")
    return "\n".join(lines)


def summarise_group_ids(run: ConsolidationRun) -> list[list[str]]:
    return [[m.id for m in group] for group in run.groups]


def consolidated_ids(store: MemoryStore, summary_id: str) -> list[str]:
    """Which originals a summary replaced, for the panel and for tests."""
    out: list[str] = []
    for memory_type in MEMORY_TYPES_SCANNED:
        # Consolidated originals are tombstoned, so the tombstone-hiding
        # listing would find none of them.
        for memory in store.list_by_type_including_tombstones(memory_type, limit=500):
            if memory.consolidated_into == summary_id:
                out.append(memory.id)
    return sorted(out)


def build_episode(
    *, conversation_id: str, opened_at: str, closed_at: str, summary: str, turns: int
) -> dict[str, Any]:
    """The fixed schema for an episode memory (ADR-16, docs/11 VP4).

    Fixed because it is written by housekeeping rather than by the agent
    choosing to: a conversation becomes memory whether or not anyone
    remembered to summarise it.
    """
    return {
        "content": (
            f"## Conversation {conversation_id}\n\n"
            f"From {opened_at} to {closed_at}, {turns} turns.\n\n{summary.strip()}"
        ),
        "type": "episode",
        "tags": ["episode", conversation_id.split(":")[0]],
        "importance": 2,
    }
