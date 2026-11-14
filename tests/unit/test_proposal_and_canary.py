"""The self-modification pipeline's two gates (docs/08, docs/11 VP8).

These are the tests that keep "the agent cannot change what constrains it" true
in code rather than in prose.
"""

from __future__ import annotations

from typing import Any

import pytest

from gates.canary.assertions import CanaryReport, evaluate, purge_keys
from gates.proposal.allowlist import (
    MAX_DIFF_BYTES,
    check_diff,
    check_paths,
    is_allowed,
    paths_in_diff,
)


def diff_for(*paths: str) -> str:
    parts = []
    for path in paths:
        parts.append(f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-# old\n+# new\n")
    return "".join(parts)


# ------------------------------------------------------------- allowlist


@pytest.mark.parametrize(
    "path",
    [
        "agent/tools/notes.py",
        "agent/prompts/system_heartbeat.md",
        "core/storage/memory.py",
        "sandbox/handler.py",
        "tests/unit/test_memory.py",
        "tests/canary/fixtures/message.json",
    ],
)
def test_allowed_paths(path: str) -> None:
    assert is_allowed(path)


@pytest.mark.parametrize(
    "path",
    [
        "gates/model/policy.py",
        "infra/stacks/agent.py",
        "infra/iam/allowlists.py",
        "supervisor/handler.py",
        "supervisor/rubric.py",
        "contracts/model-request.schema.json",
        "invariants/diary-always.md",
        ".github/workflows/ci.yml",
        ".github/CODEOWNERS",
        "scripts/destroy.py",
        "ops/actions.py",
        "panel/app.py",
        "docs/13-decisions.md",
        "pyproject.toml",
        "Makefile",
        "tests/integration/test_live.py",
    ],
)
def test_every_guardrail_path_is_refused(path: str) -> None:
    """Everything that constrains the agent lives in one of these."""
    assert not is_allowed(path)


def test_a_clean_agent_diff_is_allowed() -> None:
    verdict = check_diff(diff_for("agent/tools/notes.py"))
    assert verdict.allowed is True
    assert verdict.needs_codeowner is False


def test_one_bad_path_refuses_the_whole_diff() -> None:
    """A diff is atomic: an allowed file does not launder a refused one."""
    verdict = check_diff(diff_for("agent/tools/notes.py", "infra/stacks/agent.py"))
    assert verdict.allowed is False
    assert verdict.refused_paths == ("infra/stacks/agent.py",)


def test_a_sandbox_change_always_needs_a_codeowner() -> None:
    """No flag can waive this (docs/08 §3.3)."""
    verdict = check_diff(diff_for("sandbox/handler.py"))
    assert verdict.allowed is True
    assert verdict.needs_codeowner is True


def test_a_prompts_only_diff_is_marked_as_such() -> None:
    verdict = check_diff(diff_for("agent/prompts/system_heartbeat.md"))
    assert verdict.prompts_only is True


def test_a_mixed_diff_is_not_prompts_only() -> None:
    """auto_merge_prompts must not cover a code change riding along."""
    verdict = check_diff(diff_for("agent/prompts/x.md", "agent/tools/notes.py"))
    assert verdict.allowed is True
    assert verdict.prompts_only is False


def test_an_oversized_diff_is_refused() -> None:
    huge = "--- a/agent/x.py\n+++ b/agent/x.py\n" + ("+x\n" * (MAX_DIFF_BYTES // 2))
    assert check_diff(huge).allowed is False


def test_a_binary_diff_is_refused() -> None:
    diff = "--- a/agent/x.png\n+++ b/agent/x.png\nGIT binary patch\n"
    verdict = check_diff(diff)
    assert verdict.allowed is False
    assert "binary" in verdict.reason


@pytest.mark.parametrize("path", ["../../etc/passwd", "/etc/passwd", "agent/../../../secrets"])
def test_a_path_escaping_the_repository_is_refused(path: str) -> None:
    verdict = check_diff(diff_for(path))
    assert verdict.allowed is False
    assert "outside the repository" in verdict.reason or verdict.refused_paths


def test_an_empty_diff_is_refused() -> None:
    assert check_diff("").allowed is False


def test_paths_are_extracted_without_the_a_b_prefixes() -> None:
    assert paths_in_diff(diff_for("agent/x.py")) == ["agent/x.py"]


def test_a_new_file_diff_is_handled() -> None:
    diff = "--- /dev/null\n+++ b/agent/tools/new.py\n@@ -0,0 +1 @@\n+x\n"
    assert paths_in_diff(diff) == ["agent/tools/new.py"]
    assert check_diff(diff).allowed is True


def test_check_paths_accepts_a_plain_list() -> None:
    assert check_paths(["agent/a.py", "core/b.py"]).allowed is True


# ---------------------------------------------------------------- canary


def conversation(**overrides: Any) -> dict[str, Any]:
    base = {
        "schema_version": "1",
        "trace_id": "t1",
        "stage": "test",
        "issued_at": "2026-09-06T12:00:00Z",
        "reply": "ok",
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cached_tokens": 0,
            "model_calls": 1,
        },
        "tool_calls": [
            {"name": "memory_search", "ok": True, "duration_ms": 5},
            {"name": "memory_save", "ok": True, "duration_ms": 5},
        ],
        "memory_ops": {"saved": 1, "updated": 0, "forgotten": 0},
        "notes_delivered": [],
    }
    base.update(overrides)
    return base


def heartbeat(**overrides: Any) -> dict[str, Any]:
    base = {
        "schema_version": "1",
        "trace_id": "t1",
        "stage": "test",
        "issued_at": "2026-09-06T12:00:00Z",
        "invocation_id": "01J0000000000000000000000A",
        "outcome": "worked",
        "summary": "did a thing",
        "diary_entry_sk": "2026-09-06T12:00:00.000Z#01J0000000000000000000000A",
        "next_wake_wish_minutes": 60,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cached_tokens": 0,
            "model_calls": 1,
        },
        "tool_calls": [{"name": "diary_write", "ok": True, "duration_ms": 5}],
        "memory_ops": {"saved": 0, "updated": 0, "forgotten": 0, "consolidated": 0},
        "artifacts_touched": [],
    }
    base.update(overrides)
    return base


def run(**overrides: Any) -> CanaryReport:
    kwargs: dict[str, Any] = {
        "conversation_responses": [conversation()],
        "heartbeat_responses": [heartbeat()],
        "live_items_in_window": 0,
        "diary_entries": [
            {"sk": "2026-09-06T12:00:00.000Z#01J0000000000000000000000A", "content": "x"}
        ],
        "total_tokens": 1000,
        "finish_reasons": ["stop", "stop"],
    }
    kwargs.update(overrides)
    return evaluate(**kwargs)


def test_a_healthy_run_passes_everything() -> None:
    report = run()
    assert report.passed, report.render()


def test_a_response_with_an_error_fails() -> None:
    report = run(conversation_responses=[conversation(error={"type": "crash", "message": "x"})])
    assert not report.passed
    assert "response-valid" in [f.id for f in report.failures]


def test_a_loop_that_hit_a_limit_fails() -> None:
    """A final call that ended on `length` means the loop was cut off, not done."""
    report = run(finish_reasons=["stop", "length"])
    assert "loop-terminates" in [f.id for f in report.failures]


def test_a_run_with_no_model_calls_fails() -> None:
    report = run(finish_reasons=[])
    assert "loop-terminates" in [f.id for f in report.failures]


def test_a_missing_memory_tool_fails() -> None:
    report = run(
        conversation_responses=[
            conversation(tool_calls=[{"name": "memory_search", "ok": True, "duration_ms": 1}])
        ]
    )
    assert "memory-tools-wired" in [f.id for f in report.failures]


def test_a_missing_diary_entry_fails() -> None:
    """The guarantee the heartbeat handler makes; removing it fails the deploy."""
    report = run(diary_entries=[])
    assert "diary-written" in [f.id for f in report.failures]


def test_a_wish_outside_the_bounds_fails() -> None:
    report = run(heartbeat_responses=[heartbeat(next_wake_wish_minutes=5)])
    assert "scheduling-wired" in [f.id for f in report.failures]


def test_a_missing_wish_fails() -> None:
    report = run(heartbeat_responses=[heartbeat(next_wake_wish_minutes=None)])
    assert "scheduling-wired" in [f.id for f in report.failures]


def test_a_successful_notify_in_canary_fails() -> None:
    report = run(
        conversation_responses=[
            conversation(
                tool_calls=[
                    {"name": "memory_search", "ok": True, "duration_ms": 1},
                    {"name": "memory_save", "ok": True, "duration_ms": 1},
                    {"name": "notify_user", "ok": True, "duration_ms": 1},
                ]
            )
        ]
    )
    assert "canary-mode-honored" in [f.id for f in report.failures]


def test_a_refused_notify_in_canary_is_fine() -> None:
    """The attempt is recorded; only a success is a failure."""
    report = run(
        conversation_responses=[
            conversation(
                tool_calls=[
                    {"name": "memory_search", "ok": True, "duration_ms": 1},
                    {"name": "memory_save", "ok": True, "duration_ms": 1},
                    {"name": "notify_user", "ok": True, "duration_ms": 1, "disabled": True},
                ]
            )
        ]
    )
    assert "canary-mode-honored" not in [f.id for f in report.failures]


def test_writing_a_live_item_fails() -> None:
    report = run(live_items_in_window=1)
    assert "namespace-honored" in [f.id for f in report.failures]


def test_a_cost_regression_fails() -> None:
    report = run(total_tokens=70000)
    assert "cost-regression" in [f.id for f in report.failures]


def test_no_invocation_at_all_fails_the_invariant_check() -> None:
    report = run(
        conversation_responses=[], heartbeat_responses=[], diary_entries=[], finish_reasons=[]
    )
    assert "invariants-deployed" in [f.id for f in report.failures]


def test_the_report_renders_readably() -> None:
    rendered = run(live_items_in_window=3).render()
    assert "FAIL namespace-honored" in rendered


def test_only_canary_keys_are_ever_purged() -> None:
    """A bug that passed a live key here would delete real memory."""
    items = [
        {"PK": {"S": "CANARY#MEM#01J"}},
        {"PK": {"S": "MEM#01J"}},
        {"PK": {"S": "CANARY#DIARY"}},
        {"PK": {"S": "DIARY"}},
    ]
    assert [i["PK"]["S"] for i in purge_keys(items)] == ["CANARY#MEM#01J", "CANARY#DIARY"]
