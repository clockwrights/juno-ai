"""Local deploys stand in for the pipeline that was removed (ADR-38)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.deploy import deploy_refusal

ROOT = Path(__file__).resolve().parents[2]


def test_prod_deploys_from_a_clean_main() -> None:
    assert deploy_refusal("prod", "main", dirty=False) is None


@pytest.mark.parametrize(("branch", "dirty"), [("feature/x", False), ("main", True)])
def test_prod_refuses_anything_else(branch: str, dirty: bool) -> None:
    """Without a pipeline, this is what keeps "deployed" equal to "merged"."""
    assert deploy_refusal("prod", branch, dirty=dirty)


def test_an_ephemeral_stage_may_deploy_a_dirty_branch() -> None:
    assert deploy_refusal("test-1", "feature/x", dirty=True) is None


def test_no_workflow_can_deploy() -> None:
    """Deploy credentials in GitHub were the largest standing risk; none remain."""
    for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
        text = workflow.read_text(encoding="utf-8")
        assert "id-token: write" not in text, workflow.name
        assert "cdk deploy" not in text, workflow.name
        assert "configure-aws-credentials" not in text, workflow.name
