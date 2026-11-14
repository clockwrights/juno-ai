"""Contract tests (docs/12 §4).

Contracts are frozen once written, so these tests are less about catching bugs
today and more about making a future change to a schema impossible to do
quietly.
"""

from __future__ import annotations

import json

import jsonschema
import pytest

from core import contracts

pytestmark = pytest.mark.contract

EXPECTED_SCHEMAS = {
    "conversation-request",
    "conversation-response",
    "heartbeat-request",
    "heartbeat-response",
    "model-request",
    "model-response",
    "embed-request",
    "embed-response",
    "executor-request",
    "executor-response",
    "inbound-message",
    "outbound-notification",
    "event-detail",
    "proposal-record",
    "session-record",
    "supervisor-decision",
}

EXPECTED_TOOLS = {
    "memory_save",
    "memory_search",
    "memory_update",
    "memory_forget",
    "profile_read",
    "profile_write",
    "diary_read",
    "diary_write",
    "leave_note",
    "notify_user",
    "run_code",
    "artifact_read",
    "artifact_write",
    "artifact_list",
    "schedule_next_wake",
    "propose_change",
    "list_proposals",
    "proposal_status",
}


def test_every_expected_schema_exists() -> None:
    assert set(contracts.schema_names()) == EXPECTED_SCHEMAS


def test_every_expected_tool_schema_exists() -> None:
    assert set(contracts.tool_names()) == EXPECTED_TOOLS


def test_no_web_tool_exists() -> None:
    """ADR-28 removed web access; the tool is absent, not merely disabled."""
    assert "web_search" not in contracts.tool_names()
    assert "web_fetch" not in contracts.tool_names()


@pytest.mark.parametrize("name", sorted(EXPECTED_SCHEMAS))
def test_schema_is_valid_draft_2020_12(name: str) -> None:
    schema = contracts.load_schema(name)
    assert schema["$schema"].endswith("2020-12/schema")
    jsonschema.Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize("tool", sorted(EXPECTED_TOOLS))
def test_tool_schema_is_strict_compatible(tool: str) -> None:
    """Strict mode needs `additionalProperties: false` and every property required."""
    schema = contracts.load_schema(f"tools/{tool}")
    for side in ("input", "output"):
        sub = schema["$defs"][side]
        branches = sub.get("oneOf", [sub])
        for branch in branches:
            if branch.get("type") != "object":
                continue
            assert branch.get("additionalProperties") is False, f"{tool}.{side}"
            assert set(branch.get("required", [])) == set(branch.get("properties", {})), (
                f"{tool}.{side}: required must list every property"
            )


def test_envelope_is_identical_across_schemas() -> None:
    """One envelope, not fourteen near-copies (docs/03 §1)."""
    envelopes = {}
    for name in sorted(EXPECTED_SCHEMAS):
        props = contracts.load_schema(name).get("properties", {})
        if "issued_at" in props:
            envelopes[name] = json.dumps(
                {k: props[k] for k in ("schema_version", "trace_id", "stage", "issued_at")},
                sort_keys=True,
            )
    assert len(set(envelopes.values())) == 1, envelopes


def test_a_valid_conversation_request_passes() -> None:
    payload = {
        **contracts.envelope("dev", "01J0000000000000000000000A"),
        "mode": "live",
        "channel": "telegram",
        "conversation_id": "tg:123",
        "message": {"message_id": "tg:1", "text": "hello", "received_at": "2026-09-06T10:00:00Z"},
        "budgets": {"notifications_remaining": 2, "tokens_remaining_today": 200000},
    }
    contracts.validate("conversation-request", payload)


def test_an_unknown_field_is_rejected() -> None:
    payload = {
        **contracts.envelope("dev"),
        "mode": "live",
        "channel": "telegram",
        "conversation_id": "tg:123",
        "message": {"message_id": "tg:1", "text": "hi", "received_at": "2026-09-06T10:00:00Z"},
        "budgets": {"notifications_remaining": 2, "tokens_remaining_today": 1},
        "surprise": True,
    }
    with pytest.raises(contracts.ContractError):
        contracts.validate("conversation-request", payload)


def test_the_caller_may_not_choose_a_model() -> None:
    """ADR-05: naming the model is the gateway's job and nobody else's."""
    payload = {
        **contracts.envelope("dev"),
        "role": "conversation",
        "system": [{"text": "you are Juno", "stable": True}],
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1000,
        "model": "zai-org/GLM-5.3",
    }
    with pytest.raises(contracts.ContractError):
        contracts.validate("model-request", payload)


def test_tool_input_and_output_validate() -> None:
    contracts.validate_tool_input(
        "memory_save",
        {
            "content": "the owner lives in Springfield",
            "type": "user_fact",
            "tags": ["home"],
            "importance": 4,
        },
    )
    contracts.validate_tool_output("memory_save", {"id": "MEM#01J"})
    with pytest.raises(contracts.ContractError):
        contracts.validate_tool_input(
            "memory_save",
            {"content": "x", "type": "not_a_type", "tags": [], "importance": 3},
        )


def test_importance_is_bounded() -> None:
    with pytest.raises(contracts.ContractError):
        contracts.validate_tool_input(
            "memory_save", {"content": "x", "type": "user_fact", "tags": [], "importance": 9}
        )


def test_state_keys_and_canary_assertions_load() -> None:
    keys = contracts.state_keys()
    assert keys["canary_prefix"] == "CANARY#"
    assert "PAUSE" in keys["partitions"], "pause must be its own partition (docs/09 §1)"
    assert contracts.flag_defaults()["proposals_enabled"] is False
    assert contracts.flag_defaults()["web_access_enabled"] is False
    assertions = contracts.canary_assertions()
    assert assertions["runs_per_target"] == 3
    assert {a["id"] for a in assertions["assertions"]} >= {"diary-written", "namespace-honored"}
