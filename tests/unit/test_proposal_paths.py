"""The CI path check (docs/08 §2, docs/11 VP8 acceptance).

Tested as a pure function over a synthetic file list so it runs without a git
branch, which is what the build plan asks for.
"""

from __future__ import annotations

import pytest

from scripts.check_proposal_paths import check, is_allowed


@pytest.mark.parametrize(
    "path",
    [
        "agent/tools/notes.py",
        "core/storage/memory.py",
        "sandbox/handler.py",
        "agent/prompts/system_heartbeat.md",
        "tests/unit/test_memory.py",
        "tests/canary/fixtures/message.json",
    ],
)
def test_allowed_paths(path: str) -> None:
    assert is_allowed(path)


@pytest.mark.parametrize(
    "path",
    [
        "infra/stacks/agent.py",
        "gates/model/policy.py",
        "supervisor/handler.py",
        "contracts/model-request.schema.json",
        "invariants/diary-always.md",
        ".github/workflows/ci.yml",
        "scripts/destroy.py",
        "ops/admin.py",
        "docs/13-decisions.md",
        "pyproject.toml",
        "Makefile",
    ],
)
def test_refused_paths(path: str) -> None:
    """These are the paths that hold the guardrails, so the agent cannot touch them."""
    assert not is_allowed(path)


def test_a_mixed_change_is_refused_for_the_bad_path_only() -> None:
    refused, _ = check(["agent/tools/notes.py", "infra/stacks/agent.py"])
    assert refused == ["infra/stacks/agent.py"]


def test_sandbox_changes_are_flagged_for_a_codeowner() -> None:
    """No flag can ever auto-merge a sandbox change (docs/08 §3.3)."""
    refused, human = check(["sandbox/handler.py"])
    assert refused == []
    assert human == ["sandbox/handler.py"]


def test_an_empty_diff_is_fine() -> None:
    assert check([]) == ([], [])
