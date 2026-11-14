"""Deterministic housekeeping (ADR-16).

Closing an idle conversation into an episode is not something the agent chooses
to do. If it were, a conversation would become memory only when the agent
happened to think of it — which is exactly the failure v1 had. This runs every
maintenance cycle regardless of what the model decides the cycle is for.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field

from agent.heartbeat.consolidate import build_episode
from core import logging as jlog
from core.clock import Clock
from core.storage.conversations import ConversationStore, Message
from core.storage.memory import MemoryStore

_log = jlog.get_logger("agent.housekeeping")

#: How long a conversation must be quiet before it counts as over. Read from the
#: environment on `dev` so integration tests can shorten it (docs/11 VP6).
DEFAULT_IDLE_HOURS = 2


@dataclass
class HousekeepingResult:
    closed: list[str] = field(default_factory=list)
    episodes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def idle_threshold(clock: Clock, hours: int = DEFAULT_IDLE_HOURS) -> str:
    now = clock.now()
    return (now - dt.timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def close_idle_conversations(
    conversations: ConversationStore,
    memory: MemoryStore,
    *,
    hours: int = DEFAULT_IDLE_HOURS,
    summarise: Callable[[list[Message]], str] | None = None,
) -> HousekeepingResult:
    """Close every conversation idle past the threshold, writing an episode each.

    `summarise` is an optional callable taking the transcript and returning a
    paragraph. Without it the episode records what happened structurally, which
    is less useful but never fails — and a failed episode would mean a lost
    conversation.
    """
    result = HousekeepingResult()
    threshold = idle_threshold(conversations.clock, hours)

    for conversation_id in conversations.idle_conversations(threshold):
        try:
            meta = conversations.meta(conversation_id)
            if meta is None or meta["status"] != "open":
                continue
            transcript = conversations.transcript(conversation_id)
            turns = sum(1 for m in transcript if m.role == "user")

            summary = ""
            if summarise is not None:
                try:
                    summary = str(summarise(transcript))
                except Exception as exc:
                    jlog.warning(_log, "episode_summary_failed", error=str(exc))
            if not summary.strip():
                summary = _structural_summary(transcript, turns)

            episode = build_episode(
                conversation_id=conversation_id,
                opened_at=meta["opened_at"],
                closed_at=meta["last_activity_at"],
                summary=summary,
                turns=turns,
            )
            saved = memory.save(source="maintenance", **episode)
            conversations.close(conversation_id, episode_id=saved.id)

            result.closed.append(conversation_id)
            result.episodes.append(saved.id)
            jlog.info(
                _log, "conversation_closed", conversation_id=conversation_id, episode=saved.id
            )
        except Exception as exc:
            jlog.error(_log, "close_failed", conversation_id=conversation_id, error=str(exc))
            result.errors.append(f"{conversation_id}: {exc}")

    return result


def _structural_summary(transcript: list[Message], turns: int) -> str:
    """What can be said without a model call."""
    if not transcript:
        return "The conversation was opened but nothing was said."
    return (
        f"{turns} exchange{'s' if turns != 1 else ''}. "
        "No summary was written at the time, so this records only that it happened."
    )
