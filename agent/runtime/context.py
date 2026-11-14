"""Assembling what the model sees (docs/05 §3).

The ordering rule is the entire cost model: everything byte-identical across
calls comes first, everything that changes comes last. Under ADR-28 there are no
cache breakpoints to place — the provider caches a matching prefix
automatically — so the discipline is enforced by construction here and asserted
by a test that compares the stable prefix across calls.

Putting today's date or a remaining-token count in a stable block is the single
easiest way to make the bill quietly triple, which is why `stable` is an
explicit flag rather than something inferred.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.model_client import SystemBlock


@dataclass
class Budgets:
    notifications_remaining: int
    tokens_remaining_today: int
    wall_clock_seconds: int | None = None


def build_context(
    *,
    static_prompt: str,
    tools_prompt: str,
    profile: str,
    diary_entries: list[dict[str, str]],
    notes: list[dict[str, str]],
    budgets: Budgets,
    now_iso: str,
    mode: str,
    extra_volatile: str = "",
) -> list[SystemBlock]:
    """Build the system blocks for one call.

    Three blocks, in order:

    1. The prompt and tool descriptions. Identical on every call forever.
    2. Profile, recent diary, pending notes. Identical for the whole of a
       conversation, and usually for a whole day.
    3. Date, budgets, mode. Different on every single call.
    """
    stable_prompt = f"{static_prompt.strip()}\n\n{tools_prompt.strip()}".strip()

    slowly_changing = _slowly_changing(profile, diary_entries, notes)
    volatile = _volatile(budgets, now_iso, mode, extra_volatile)

    blocks = [SystemBlock(text=stable_prompt, stable=True)]
    if slowly_changing:
        blocks.append(SystemBlock(text=slowly_changing, stable=True))
    blocks.append(SystemBlock(text=volatile, stable=False))
    return blocks


def _slowly_changing(
    profile: str, diary_entries: list[dict[str, str]], notes: list[dict[str, str]]
) -> str:
    parts: list[str] = []
    if profile.strip():
        parts.append(f"## What you know about the owner\n\n{profile.strip()}")
    if diary_entries:
        rendered = "\n\n".join(f"### {entry['sk']}\n{entry['content']}" for entry in diary_entries)
        parts.append(f"## Your recent diary\n\n{rendered}")
    if notes:
        rendered = "\n".join(f"- {note['text']}" for note in notes)
        parts.append(f"## Notes you left for the owner\n\n{rendered}")
    return "\n\n".join(parts)


def _volatile(budgets: Budgets, now_iso: str, mode: str, extra: str) -> str:
    lines = [
        "## Right now",
        "",
        f"- The time is {now_iso}.",
        f"- You are running in {mode} mode.",
        f"- Notifications left today: {budgets.notifications_remaining}.",
        f"- Model tokens left today: {budgets.tokens_remaining_today:,}.",
    ]
    if budgets.wall_clock_seconds is not None:
        lines.append(f"- Wall clock for this cycle: {budgets.wall_clock_seconds} seconds.")
    if extra.strip():
        lines += ["", extra.strip()]
    return "\n".join(lines)


def stable_text(blocks: list[SystemBlock]) -> str:
    """The cacheable prefix, for tests and for `scripts/inspect_request.py`."""
    return "".join(block.text for block in blocks if block.stable)


def transcript_to_messages(transcript: list[Any]) -> list[dict[str, Any]]:
    """Turn stored conversation messages into API messages.

    Stored content is whatever was sent originally, so a plain string and a
    structured content list both round-trip.
    """
    messages: list[dict[str, Any]] = []
    for message in transcript:
        role = getattr(message, "role", None) or message.get("role")
        content = getattr(message, "content", None)
        if content is None:
            content = message.get("content")
        messages.append({"role": role, "content": content})
    return messages
