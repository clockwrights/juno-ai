"""Emit contracts/*.schema.json.

The schemas are the frozen boundary of the system (docs/03-contracts.md), so they
live in the repository as plain JSON files. This generator exists only so the
common envelope and the shared sub-objects are written identically in all of
them; it is run by hand when a *new* schema is added, never as part of a build.
Editing an existing schema means bumping `schema_version` and following the
frozen-contract rule in docs/03-contracts.md.

    uv run python scripts/gen_contracts.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "contracts"

DRAFT = "https://json-schema.org/draft/2020-12/schema"


def write_lf(path: Path, text: str) -> None:
    """Always LF, on every platform.

    git checks these files out as LF (`.gitattributes`), so a generator that
    wrote CRLF on Windows would make `check_contracts_fresh.py` report drift on
    a fresh clone with nothing actually wrong.
    """
    path.write_text(text, encoding="utf-8", newline="\n")


ISO = {"type": "string", "format": "date-time"}
ULID = {"type": "string", "pattern": "^[0-9A-HJKMNP-TV-Z]{26}$"}

ENVELOPE_PROPS: dict[str, Any] = {
    "schema_version": {"const": "1"},
    "trace_id": {"type": "string", "minLength": 1},
    "stage": {"type": "string", "pattern": "^[a-z0-9-]{1,24}$"},
    "issued_at": ISO,
}
ENVELOPE_REQUIRED = ["schema_version", "trace_id", "stage", "issued_at"]

USAGE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["input_tokens", "output_tokens", "cached_tokens", "model_calls"],
    "properties": {
        "input_tokens": {"type": "integer", "minimum": 0},
        "output_tokens": {"type": "integer", "minimum": 0},
        "cached_tokens": {"type": "integer", "minimum": 0},
        "model_calls": {"type": "integer", "minimum": 0},
    },
}

TOOL_CALLS = {
    "type": "array",
    "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["name", "ok", "duration_ms"],
        "properties": {
            "name": {"type": "string"},
            "ok": {"type": "boolean"},
            "duration_ms": {"type": "integer", "minimum": 0},
            "error": {"type": ["string", "null"]},
        },
    },
}

MEMORY_OPS = {
    "type": "object",
    "additionalProperties": False,
    "required": ["saved", "updated", "forgotten"],
    "properties": {
        "saved": {"type": "integer", "minimum": 0},
        "updated": {"type": "integer", "minimum": 0},
        "forgotten": {"type": "integer", "minimum": 0},
        "consolidated": {"type": "integer", "minimum": 0},
    },
}

MEMORY_TYPES = [
    "user_fact",
    "user_preference",
    "episode",
    "agent_learning",
    "project",
    "pending",
]

MODEL_ROLES = ["conversation", "heartbeat", "consolidation", "supervisor", "canary"]


def schema(name: str, title: str, props: dict[str, Any], required: list[str], **extra: Any) -> None:
    """Write one schema file with the common envelope merged in."""
    body: dict[str, Any] = {
        "$schema": DRAFT,
        "$id": f"urn:juno:contracts:{name}",
        "title": title,
        "type": "object",
        "additionalProperties": False,
        "required": ENVELOPE_REQUIRED + required,
        "properties": {**ENVELOPE_PROPS, **props},
    }
    body.update(extra)
    path = OUT / f"{name}.schema.json"
    write_lf(path, json.dumps(body, indent=2) + "\n")
    print(f"  {path.relative_to(ROOT)}")


def bare(name: str, body: dict[str, Any]) -> None:
    """Write a schema that does not carry the envelope."""
    body = {
        "$schema": DRAFT,
        "$id": f"urn:juno:contracts:{name}",
        **body,
    }
    path = OUT / f"{name}.schema.json"
    write_lf(path, json.dumps(body, indent=2) + "\n")
    print(f"  {path.relative_to(ROOT)}")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    (OUT / "tools").mkdir(exist_ok=True)
    print("contracts:")

    # ---------------------------------------------------------- 2.1 conversation
    schema(
        "conversation-request",
        "agent-conversation invocation request",
        {
            "mode": {"enum": ["live", "canary"]},
            "channel": {"enum": ["telegram", "panel"]},
            "conversation_id": {"type": "string", "minLength": 1},
            "message": {
                "type": "object",
                "additionalProperties": False,
                "required": ["message_id", "text", "received_at"],
                "properties": {
                    "message_id": {"type": "string", "minLength": 1},
                    "text": {"type": "string"},
                    "received_at": ISO,
                },
            },
            "budgets": {
                "type": "object",
                "additionalProperties": False,
                "required": ["notifications_remaining", "tokens_remaining_today"],
                "properties": {
                    "notifications_remaining": {"type": "integer", "minimum": 0},
                    "tokens_remaining_today": {"type": "integer", "minimum": 0},
                },
            },
        },
        ["mode", "channel", "conversation_id", "message", "budgets"],
    )

    schema(
        "conversation-response",
        "agent-conversation invocation response",
        {
            "reply": {"type": "string"},
            "usage": USAGE,
            "tool_calls": TOOL_CALLS,
            "memory_ops": MEMORY_OPS,
            "notes_delivered": {"type": "array", "items": {"type": "string"}},
            "error": {
                "type": ["object", "null"],
                "additionalProperties": False,
                "required": ["type", "message"],
                "properties": {
                    "type": {
                        "enum": [
                            "model_error",
                            "tool_error",
                            "budget_exhausted",
                            "timeout",
                            "crash",
                        ]
                    },
                    "message": {"type": "string"},
                },
            },
        },
        ["reply", "usage", "tool_calls", "memory_ops", "notes_delivered"],
    )

    # ------------------------------------------------------------ 2.2 heartbeat
    schema(
        "heartbeat-request",
        "agent-heartbeat invocation request",
        {
            "mode": {"enum": ["free", "maintenance", "canary"]},
            "invocation_id": ULID,
            "budgets": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "notifications_remaining",
                    "tokens_remaining_today",
                    "wall_clock_seconds",
                ],
                "properties": {
                    "notifications_remaining": {"type": "integer", "minimum": 0},
                    "tokens_remaining_today": {"type": "integer", "minimum": 0},
                    "wall_clock_seconds": {"type": "integer", "minimum": 1},
                },
            },
            "agenda": {
                "type": "array",
                "items": {
                    "enum": ["close_idle_conversations", "consolidate_memory", "canary_probe"]
                },
            },
        },
        ["mode", "invocation_id", "budgets", "agenda"],
    )

    schema(
        "heartbeat-response",
        "agent-heartbeat invocation response",
        {
            "invocation_id": ULID,
            "outcome": {"enum": ["did_nothing", "worked", "error"]},
            "summary": {"type": "string"},
            "diary_entry_sk": {"type": "string"},
            "next_wake_wish_minutes": {"type": ["integer", "null"], "minimum": 15, "maximum": 1440},
            "usage": USAGE,
            "tool_calls": TOOL_CALLS,
            "memory_ops": MEMORY_OPS,
            "artifacts_touched": {"type": "array", "items": {"type": "string"}},
        },
        [
            "invocation_id",
            "outcome",
            "summary",
            "diary_entry_sk",
            "next_wake_wish_minutes",
            "usage",
            "tool_calls",
            "memory_ops",
            "artifacts_touched",
        ],
    )

    # ---------------------------------------------------------- 2.3 gate-model
    schema(
        "model-request",
        "gate-model request (OpenAI-compatible subset, ADR-28)",
        {
            "role": {"enum": MODEL_ROLES},
            "system": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["text", "stable"],
                    "properties": {"text": {"type": "string"}, "stable": {"type": "boolean"}},
                },
            },
            "messages": {"type": "array", "items": {"type": "object"}},
            "tools": {"type": "array", "items": {"type": "object"}},
            "max_tokens": {"type": "integer", "minimum": 1, "maximum": 16000},
            "response_format": {"type": ["object", "null"]},
        },
        ["role", "system", "messages", "max_tokens"],
        # The caller must never choose the model; the gate does (ADR-05, ADR-28).
        **{"not": {"required": ["model"]}},
    )

    # ------------------------------------------ 2.3b gate-model, embed alias
    # A separate contract rather than a variant of model-request: an embedding
    # call has no system prompt, no messages and no tools, and forcing it
    # through the chat shape would mean loosening that schema's `required`
    # for every caller (ADR-29).
    schema(
        "embed-request",
        "gate-model embed request",
        {
            # The gateway routes on `role` before it validates, so it belongs
            # in the contract even though there is only ever one value.
            "role": {"const": "embed"},
            "texts": {
                "type": "array",
                "minItems": 1,
                "maxItems": 64,
                "items": {"type": "string", "maxLength": 8192},
            },
        },
        ["role", "texts"],
        **{"not": {"required": ["model"]}},
    )

    bare(
        "embed-response",
        {
            "title": "gate-model embed response",
            "type": "object",
            "required": ["schema_version", "trace_id"],
            "properties": {
                "schema_version": {"const": "1"},
                "trace_id": {"type": "string"},
                "embeddings": {
                    "type": ["array", "null"],
                    "items": {"type": "array", "items": {"type": "number"}},
                },
                "model": {"type": ["string", "null"]},
                "dimension": {"type": ["integer", "null"]},
                "gateway": {"type": ["object", "null"]},
                "error": {
                    "type": ["object", "null"],
                    "additionalProperties": False,
                    "required": ["type", "message"],
                    "properties": {
                        "type": {
                            "enum": [
                                "budget_exhausted",
                                "role_not_allowed",
                                "upstream_error",
                                "invalid_request",
                            ]
                        },
                        "message": {"type": "string"},
                        "retry_after_seconds": {"type": "integer", "minimum": 0},
                    },
                },
            },
        },
    )

    bare(
        "model-response",
        {
            "title": "gate-model response",
            "type": "object",
            "required": ["schema_version", "trace_id"],
            "properties": {
                "schema_version": {"const": "1"},
                "trace_id": {"type": "string"},
                "content": {"type": ["string", "null"]},
                "reasoning_content": {"type": ["string", "null"]},
                "tool_calls": {"type": ["array", "null"], "items": {"type": "object"}},
                "finish_reason": {"enum": ["stop", "length", "tool_calls", "content_filter", None]},
                "model": {"type": ["string", "null"]},
                "usage": {"type": ["object", "null"]},
                "gateway": {
                    "type": ["object", "null"],
                    "additionalProperties": False,
                    "required": ["model_used", "tokens_remaining_today", "latency_ms"],
                    "properties": {
                        "model_used": {"type": "string"},
                        "tokens_remaining_today": {"type": "integer"},
                        "latency_ms": {"type": "integer", "minimum": 0},
                    },
                },
                "error": {
                    "type": ["object", "null"],
                    "additionalProperties": False,
                    "required": ["type", "message"],
                    "properties": {
                        "type": {
                            "enum": [
                                "budget_exhausted",
                                "role_not_allowed",
                                "upstream_error",
                                "invalid_request",
                            ]
                        },
                        "message": {"type": "string"},
                        "retry_after_seconds": {"type": "integer", "minimum": 0},
                    },
                },
            },
        },
    )

    # ------------------------------------------------------------- 2.4 executor
    bare(
        "executor-request",
        {
            "title": "sandbox-executor request",
            "type": "object",
            "additionalProperties": False,
            "required": ["schema_version", "trace_id", "language", "code"],
            "properties": {
                "schema_version": {"const": "1"},
                "trace_id": {"type": "string"},
                "language": {"enum": ["python"]},
                "code": {"type": "string"},
                "timeout_s": {"type": "integer", "minimum": 1, "maximum": 60, "default": 30},
                "stdin": {"type": ["string", "null"]},
            },
        },
    )

    bare(
        "executor-response",
        {
            "title": "sandbox-executor response",
            "type": "object",
            "additionalProperties": False,
            "required": [
                "schema_version",
                "trace_id",
                "stdout",
                "stderr",
                "exit_code",
                "duration_ms",
                "truncated",
                "killed",
            ],
            "properties": {
                "schema_version": {"const": "1"},
                "trace_id": {"type": "string"},
                "stdout": {"type": "string"},
                "stderr": {"type": "string"},
                "exit_code": {"type": "integer"},
                "duration_ms": {"type": "integer", "minimum": 0},
                "truncated": {"type": "boolean"},
                "killed": {"type": "boolean"},
            },
        },
    )

    # ------------------------------------------------------------- 3. queues
    schema(
        "inbound-message",
        "inbound-messages FIFO body",
        {
            "channel": {"enum": ["telegram", "panel"]},
            "conversation_id": {"type": "string", "minLength": 1},
            "message_id": {"type": "string", "minLength": 1},
            "text": {"type": "string"},
            "received_at": ISO,
            "telegram": {
                "type": ["object", "null"],
                "additionalProperties": False,
                "required": ["chat_id", "update_id", "message_id"],
                "properties": {
                    "chat_id": {"type": "string"},
                    "update_id": {"type": "string"},
                    "message_id": {"type": "integer"},
                },
            },
        },
        ["channel", "conversation_id", "message_id", "text", "received_at"],
    )

    schema(
        "outbound-notification",
        "outbound-notifications body",
        {
            "kind": {"const": "notify"},
            "priority": {"enum": ["low", "normal", "urgent"]},
            "text": {"type": "string", "maxLength": 4000},
            "source": {"enum": ["conversation", "heartbeat"]},
        },
        ["kind", "priority", "text", "source"],
    )

    # ------------------------------------------------------------- 4. events
    schema(
        "event-detail",
        "agent-events detail envelope",
        {"detail_type": {"type": "string"}, "payload": {"type": "object"}},
        ["detail_type", "payload"],
    )

    # ------------------------------------------------ 7. proposals, 9. sessions
    bare(
        "proposal-record",
        {
            "title": "agent_proposals META item",
            "type": "object",
            "required": ["proposal_id", "title", "rationale", "status", "created_at"],
            "properties": {
                "proposal_id": {"type": "string"},
                "title": {"type": "string", "maxLength": 200},
                "rationale": {"type": "string"},
                "diff_s3_key": {"type": ["string", "null"]},
                "target_paths": {"type": "array", "items": {"type": "string"}},
                "status": {
                    "enum": [
                        "submitted",
                        "rejected_path",
                        "pr_open",
                        "gate_failed",
                        "approved",
                        "closed",
                        "deploying",
                        "deployed",
                        "rolled_back",
                    ]
                },
                "created_at": ISO,
                "updated_at": ISO,
                "pr_url": {"type": ["string", "null"]},
                "pr_number": {"type": ["integer", "null"]},
                "rejection_reason": {"type": ["string", "null"]},
                "deployed_version": {"type": ["string", "null"]},
            },
        },
    )

    schema(
        "session-record",
        "gate-model session record (one model call)",
        {
            "seq": {"type": "integer", "minimum": 0},
            "kind": {"enum": MODEL_ROLES},
            "role": {"enum": MODEL_ROLES},
            "model": {"type": "string"},
            "requested_at": ISO,
            "latency_ms": {"type": "integer", "minimum": 0},
            "request": {"type": "object"},
            "response": {"type": ["object", "null"]},
            "error": {"type": ["object", "null"]},
        },
        ["seq", "kind", "role", "model", "requested_at", "latency_ms", "request"],
    )

    bare(
        "supervisor-decision",
        {
            "title": "supervisor structured decision",
            "type": "object",
            "additionalProperties": False,
            "required": ["level", "action", "summary", "probable_cause"],
            "properties": {
                "level": {"type": "integer", "minimum": 0, "maximum": 2},
                "action": {"enum": ["log", "notify", "pause"]},
                "summary": {"type": "string", "maxLength": 2000},
                "probable_cause": {"type": "string", "maxLength": 2000},
            },
        },
    )

    print("done")


if __name__ == "__main__":
    main()
