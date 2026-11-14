"""Talking to Telegram.

Only gates hold a bot token (ADR-03). This client is deliberately small: send a
message, split it if it is too long, and set a webhook. Anything cleverer would
be a place for the agent's text to influence what an API call does.

Under ADR-30 one bot serves both the agent and ops, so every operational
message carries a fixed marker line. That is a formatting requirement with a
test, not a convention: it is the only thing keeping the two voices apart in a
single chat.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from core import logging as jlog

_log = jlog.get_logger("gates.telegram")

API = "https://api.telegram.org"

#: Telegram's own limit is 4096; the margin leaves room for the marker line.
MESSAGE_LIMIT = 4000

#: ADR-30. Prefixed to every supervisor, alarm and proposal message so the
#: owner can always tell which voice is speaking.
OPS_MARKER = "⚠️ OPS"


class TelegramError(RuntimeError):
    pass


@dataclass
class TelegramClient:
    token: str
    opener: Any = None

    def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{API}/bot{self.token}/{method}"
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}
        )
        opener = self.opener or urllib.request.urlopen
        try:
            with opener(request, timeout=15) as response:
                return dict(json.loads(response.read().decode("utf-8")))
        except urllib.error.HTTPError as exc:
            raise TelegramError(f"{method} failed: {exc.code}") from exc
        except Exception as exc:
            raise TelegramError(f"{method} failed: {exc}") from exc

    def send_message(self, chat_id: str, text: str, *, ops: bool = False, stage: str = "") -> int:
        """Send one message, split into as many as it takes. Returns the count."""
        if ops:
            text = f"{OPS_MARKER} — {stage}\n\n{text}"
        chunks = split_message(text)
        for chunk in chunks:
            self._post(
                "sendMessage",
                {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True},
            )
        return len(chunks)

    def set_webhook(self, url: str, secret: str) -> None:
        self._post(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret,
                "allowed_updates": ["message"],
                "drop_pending_updates": True,
            },
        )

    def delete_webhook(self) -> None:
        """Used by `stop.py`: no webhook means no inbound at all (ADR-27)."""
        self._post("deleteWebhook", {"drop_pending_updates": False})

    def webhook_info(self) -> dict[str, Any]:
        return self._post("getWebhookInfo", {})


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split on paragraph, then line, then hard.

    A reply cut mid-sentence reads as a bug even when it is not, so the split
    prefers a boundary a reader would have chosen.
    """
    text = text.strip()
    if not text:
        return [""]
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        window = remaining[:limit]
        split = window.rfind("\n\n")
        if split < limit // 3:
            split = window.rfind("\n")
        if split < limit // 3:
            split = window.rfind(" ")
        if split < limit // 3:
            split = limit
        chunks.append(remaining[:split].strip())
        remaining = remaining[split:].strip()
    return [c for c in chunks if c]


def ops_message(stage: str, title: str, body: str, link: str = "") -> str:
    """Format an operational message. Never in the agent's voice (ADR-30)."""
    parts = [title.strip(), "", body.strip()]
    if link:
        parts += ["", link]
    return "\n".join(parts).strip()
