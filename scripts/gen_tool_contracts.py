"""Emit contracts/tools/*.schema.json, contracts/state-keys.json and
contracts/canary-assertions.json.

Tool schemas are strict-compatible (docs/12 §4): `additionalProperties: false`
and `required` listing *every* property. Optional arguments are therefore
expressed as nullable types rather than by omission from `required`, which is
what the model API's strict mode demands.

    uv run python scripts/gen_tool_contracts.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "contracts" / "tools"
DRAFT = "https://json-schema.org/draft/2020-12/schema"


def write_lf(path: Path, text: str) -> None:
    """Always LF, on every platform.

    git checks these files out as LF (`.gitattributes`), so a generator that
    wrote CRLF on Windows would make `check_contracts_fresh.py` report drift on
    a fresh clone with nothing actually wrong.
    """
    path.write_text(text, encoding="utf-8", newline="\n")


MEMORY_TYPES = [
    "user_fact",
    "user_preference",
    "episode",
    "agent_learning",
    "project",
    "pending",
]

STR = {"type": "string"}
ISO = {"type": "string", "format": "date-time"}


def obj(props: dict[str, Any]) -> dict[str, Any]:
    """A strict object: every property required, nothing extra allowed."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(props),
        "properties": props,
    }


MEMORY_RESULT = obj(
    {
        "id": STR,
        "type": {"enum": MEMORY_TYPES},
        "content": STR,
        "tags": {"type": "array", "items": STR},
        "importance": {"type": "integer", "minimum": 1, "maximum": 5},
        "created_at": ISO,
        "last_accessed": ISO,
        "score": {"type": "number"},
    }
)

TOOLS: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    "memory_save": (
        obj(
            {
                "content": {"type": "string", "maxLength": 8192},
                "type": {"enum": MEMORY_TYPES},
                "tags": {"type": "array", "items": STR, "maxItems": 10},
                "importance": {"type": "integer", "minimum": 1, "maximum": 5},
            }
        ),
        obj({"id": STR}),
    ),
    "memory_search": (
        obj(
            {
                "query": STR,
                "type": {"anyOf": [{"enum": MEMORY_TYPES}, {"type": "null"}]},
                "tags": {"type": ["array", "null"], "items": STR},
                "top_k": {"type": ["integer", "null"], "minimum": 1, "maximum": 20},
            }
        ),
        obj({"results": {"type": "array", "items": MEMORY_RESULT}}),
    ),
    "memory_update": (
        obj({"id": STR, "content": {"type": "string", "maxLength": 8192}}),
        obj({"id": STR, "version": {"type": "integer", "minimum": 1}}),
    ),
    "memory_forget": (
        obj({"id": STR, "reason": STR}),
        obj({"ok": {"type": "boolean"}}),
    ),
    "profile_read": (
        obj({}),
        obj({"content": STR, "etag": STR}),
    ),
    "profile_write": (
        obj({"content": {"type": "string", "maxLength": 32768}, "etag": STR}),
        {
            "oneOf": [
                obj({"etag": STR}),
                obj({"error": {"const": "conflict"}, "current_etag": STR, "content": STR}),
            ]
        },
    ),
    "diary_read": (
        obj({"last_n": {"type": ["integer", "null"], "minimum": 1, "maximum": 20}}),
        obj({"entries": {"type": "array", "items": obj({"sk": STR, "content": STR})}}),
    ),
    "diary_write": (
        obj({"content": {"type": "string", "maxLength": 8192}}),
        obj({"sk": STR}),
    ),
    "leave_note": (
        obj({"text": {"type": "string", "maxLength": 4000}}),
        obj({"note_id": STR}),
    ),
    "notify_user": (
        obj(
            {
                "text": {"type": "string", "maxLength": 4000},
                "priority": {"enum": ["low", "normal", "urgent"]},
            }
        ),
        {
            "oneOf": [
                obj(
                    {
                        "queued": {"const": True},
                        "notifications_remaining_hint": {"type": "integer", "minimum": 0},
                    }
                ),
                obj({"error": {"const": "disabled_in_canary"}}),
            ]
        },
    ),
    "run_code": (
        obj(
            {
                "code": STR,
                "timeout_s": {"type": ["integer", "null"], "minimum": 1, "maximum": 60},
                "stdin": {"type": ["string", "null"]},
            }
        ),
        obj(
            {
                "stdout": STR,
                "stderr": STR,
                "exit_code": {"type": "integer"},
                "duration_ms": {"type": "integer", "minimum": 0},
                "truncated": {"type": "boolean"},
                "killed": {"type": "boolean"},
            }
        ),
    ),
    "artifact_read": (
        obj({"path": STR}),
        obj({"content": STR, "etag": STR}),
    ),
    "artifact_write": (
        obj({"path": STR, "content": STR, "etag": {"type": ["string", "null"]}}),
        {
            "oneOf": [
                obj({"etag": STR}),
                obj({"error": {"enum": ["conflict", "path_not_allowed"]}}),
            ]
        },
    ),
    "artifact_list": (
        obj({"prefix": {"type": ["string", "null"]}}),
        obj({"paths": {"type": "array", "items": STR}}),
    ),
    "schedule_next_wake": (
        obj({"minutes": {"type": "integer", "minimum": 15, "maximum": 1440}, "reason": STR}),
        obj({"recorded_minutes": {"type": "integer", "minimum": 15, "maximum": 1440}}),
    ),
    "propose_change": (
        obj(
            {
                "title": {"type": "string", "maxLength": 200},
                "rationale": STR,
                "diff": {"type": "string", "maxLength": 204800},
            }
        ),
        {
            "oneOf": [
                obj({"proposal_id": STR}),
                obj({"error": {"enum": ["proposals_disabled", "disabled_in_canary"]}}),
            ]
        },
    ),
    "list_proposals": (
        obj({"status": {"type": ["string", "null"]}}),
        obj({"proposals": {"type": "array", "items": {"type": "object"}}}),
    ),
    # The output mirrors the proposal record (docs/03 §7). Spelled out rather
    # than left as a free-form object so strict mode can enforce it.
    "proposal_status": (
        obj({"proposal_id": STR}),
        obj(
            {
                "proposal_id": STR,
                "title": STR,
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
                "rejection_reason": {"type": ["string", "null"]},
            }
        ),
    ),
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    print("tool contracts:")
    for name, (inp, out) in TOOLS.items():
        body = {
            "$schema": DRAFT,
            "$id": f"urn:juno:contracts:tools:{name}",
            "title": f"tool {name}",
            "$defs": {"input": inp, "output": out},
        }
        write_lf(OUT / f"{name}.schema.json", json.dumps(body, indent=2) + "\n")
        print(f"  contracts/tools/{name}.schema.json")

    # ------------------------------------------------ state keys (docs/03 §6)
    state_keys = {
        "comment": "Frozen map of agent_state keys. IAM LeadingKeys conditions are "
        "derived from the partition keys here; see docs/09 §1.",
        "schema_version": "1",
        "partitions": {
            "STATE": {
                "sort_keys": ["pause", "next_wake", "counters#<date>", "lock#<chat_id>"],
                "writers": {
                    "pause": ["supervisor", "ops-alarms", "admin"],
                    "next_wake": ["agent-heartbeat"],
                    "counters#<date>": ["gate-model", "gate-notify", "gate-governor"],
                    "lock#<chat_id>": ["gate-dispatcher"],
                },
            },
            "PAUSE": {
                "comment": "STATE#pause lives under its own partition so a LeadingKeys "
                "condition can deny the agent roles outright (docs/09 §1).",
                "sort_keys": ["current"],
                "writers": {"current": ["supervisor", "ops-alarms", "admin"]},
            },
            "NOTE": {
                "sort_keys": ["<ULID>"],
                "writers": {"<ULID>": ["agent-conversation", "agent-heartbeat", "gate-notify"]},
            },
            "FLAG": {
                "sort_keys": [
                    "proposals_enabled",
                    "auto_merge_prompts",
                    "web_access_enabled",
                    "heartbeat_enabled",
                ],
                "writers": {"*": ["admin"]},
            },
            "TG_UPDATE": {
                "comment": "Telegram dedup keys, TTL 24h.",
                "sort_keys": ["<update_id>"],
                "writers": {"<update_id>": ["gate-telegram"]},
            },
        },
        "flag_defaults": {
            "proposals_enabled": False,
            "auto_merge_prompts": False,
            "web_access_enabled": False,
            "heartbeat_enabled": True,
        },
        "canary_prefix": "CANARY#",
    }
    p = ROOT / "contracts" / "state-keys.json"
    write_lf(p, json.dumps(state_keys, indent=2) + "\n")
    print("  contracts/state-keys.json")

    # ------------------------------------------ canary assertions (docs/08 §4)
    canary = {
        "schema_version": "1",
        "comment": "Every assertion must pass 3/3 runs. Deterministic only: "
        "nothing here may depend on model wording (docs/08 §4).",
        "runs_per_target": 3,
        "assertions": [
            {
                "id": "response-valid",
                "applies_to": ["conversation", "heartbeat"],
                "description": "Response validates against its contract schema "
                "and carries no error.",
            },
            {
                "id": "loop-terminates",
                "applies_to": ["conversation", "heartbeat"],
                "description": "finish_reason of the final model call is 'stop'.",
            },
            {
                "id": "memory-tools-wired",
                "applies_to": ["conversation"],
                "description": "At least one memory_search and one memory_save in tool_calls.",
            },
            {
                "id": "diary-written",
                "applies_to": ["heartbeat"],
                "description": "A CANARY#DIARY entry exists for the heartbeat invocation_id.",
            },
            {
                "id": "scheduling-wired",
                "applies_to": ["heartbeat"],
                "description": "next_wake_wish_minutes present and within 15..1440.",
            },
            {
                "id": "canary-mode-honored",
                "applies_to": ["conversation", "heartbeat"],
                "description": "No notify_user or propose_change call succeeded.",
            },
            {
                "id": "namespace-honored",
                "applies_to": ["conversation", "heartbeat"],
                "description": "No item written outside CANARY# keys during the run window, "
                "checked by scanning namespace-created_at-index for live items.",
            },
            {
                "id": "invariants-deployed",
                "applies_to": ["conversation", "heartbeat"],
                "description": "Every loaded prompt contains its invariant clauses; the handler "
                "asserts at import, so a failing import fails the invoke.",
            },
            {
                "id": "cost-regression",
                "applies_to": ["conversation", "heartbeat"],
                "description": "Total tokens for the whole run below the budget below.",
                "max_total_tokens": 60000,
            },
        ],
    }
    p = ROOT / "contracts" / "canary-assertions.json"
    write_lf(p, json.dumps(canary, indent=2) + "\n")
    print("  contracts/canary-assertions.json")
    print("done")


if __name__ == "__main__":
    main()
