"""Synthesis must need no AWS credentials (docs/12 §1, docs/16 §2).

This is the property that broke. `ec2.Vpc(max_azs=...)` asks the account which
availability zones exist — a *context lookup*: it needs credentials, it caches
the answer into a gitignored `cdk.context.json`, and once cached it is
invisible on the machine that cached it while CI fails every time.

A template assertion that needed an AWS round-trip would not be a unit test, so
the rule is that synthesis resolves everything locally.

These build the real `infra/app.py` rather than a copy, because a fixture that
reconstructed the app would drift from it exactly when that mattered, and they
read the assembly manifest off disk — which is the same artefact the CLI writes
and CI consumes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aws_cdk as cdk
import pytest

from infra.app import ACCOUNT_ENV, PLACEHOLDER_ACCOUNT, REGION, REGION_ENV, build_app
from ops import localenv

ACCOUNT = PLACEHOLDER_ACCOUNT


@pytest.fixture(autouse=True)
def _no_ambient_account(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The owner's machine has a `.env` and may export the variables; these
    tests must see neither."""
    monkeypatch.delenv(ACCOUNT_ENV, raising=False)
    monkeypatch.delenv(REGION_ENV, raising=False)
    monkeypatch.setattr(localenv, "ENV_FILE", tmp_path / "absent.env")


def manifest(stage: str, outdir: Path, **context: str) -> dict[str, Any]:
    app = cdk.App(outdir=str(outdir), context={"stage": stage, **context})
    build_app(app).synth()
    return dict(json.loads((outdir / "manifest.json").read_text(encoding="utf-8")))


def environments(assembly: dict[str, Any]) -> set[str]:
    return {
        artifact["environment"]
        for artifact in assembly["artifacts"].values()
        if artifact.get("type") == "aws:cloudformation:stack"
    }


@pytest.mark.parametrize("stage", ["dev", "prod"])
def test_synthesis_needs_no_context_lookups(stage: str, tmp_path: Path) -> None:
    """`missing` is CDK's own record of what it could not resolve locally.

    Anything in it means synthesis calls AWS, which is what fails in CI.
    """
    missing = manifest(stage, tmp_path).get("missing") or []
    assert missing == [], (
        f"synthesizing {stage} needs {len(missing)} AWS lookup(s), so it cannot run in "
        f"CI without credentials: {missing}. Resolve them locally instead — see "
        "StageStack.availability_zones for how the sandbox VPC's zones are pinned."
    )


@pytest.mark.parametrize("stage", ["dev", "prod"])
def test_the_account_and_region_are_pinned(stage: str, tmp_path: Path) -> None:
    """A shell configured elsewhere must not silently move the deployment."""
    assert environments(manifest(stage, tmp_path)) == {f"aws://{ACCOUNT}/{REGION}"}


def test_an_override_is_still_possible_but_deliberate(tmp_path: Path) -> None:
    """`-c region=` remains, so an ephemeral stage elsewhere is not impossible."""
    assembly = manifest("test-1", tmp_path, region="eu-west-1")
    assert environments(assembly) == {f"aws://{ACCOUNT}/eu-west-1"}


def test_the_account_comes_from_the_environment_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real id lives in the gitignored `.env` or a CI variable, never in git (ADR-37)."""
    monkeypatch.setenv(ACCOUNT_ENV, "123456789012")
    assert environments(manifest("prod", tmp_path)) == {f"aws://123456789012/{REGION}"}


def test_the_account_comes_from_the_gitignored_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text(f"# local\nOTHER=1\n{ACCOUNT_ENV}=123456789012\n", encoding="utf-8")
    monkeypatch.setattr(localenv, "ENV_FILE", env)
    assert environments(manifest("prod", tmp_path / "out")) == {f"aws://123456789012/{REGION}"}


def test_the_region_comes_from_the_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clone deploys wherever its owner chose, without editing code (ADR-38)."""
    env = tmp_path / ".env"
    env.write_text(f"{ACCOUNT_ENV}=123456789012\n{REGION_ENV}=us-east-1\n", encoding="utf-8")
    monkeypatch.setattr(localenv, "ENV_FILE", env)
    assert environments(manifest("prod", tmp_path / "out")) == {"aws://123456789012/us-east-1"}


def test_context_beats_the_environment_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ACCOUNT_ENV, "123456789012")
    assembly = manifest("prod", tmp_path, account="210987654321")
    assert environments(assembly) == {f"aws://210987654321/{REGION}"}


def test_a_real_deploy_refuses_the_placeholder_account(tmp_path: Path) -> None:
    """`require_real_code` marks a deploy build; it must name a real account."""
    with pytest.raises(SystemExit, match=ACCOUNT_ENV):
        manifest("prod", tmp_path, require_real_code="1")


def test_a_malformed_account_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ACCOUNT_ENV, "my-account")
    with pytest.raises(SystemExit, match="12 digits"):
        manifest("prod", tmp_path)


def test_a_stage_is_required() -> None:
    """A bare `cdk deploy` that silently meant prod would be a footgun."""
    with pytest.raises(SystemExit, match="stage"):
        build_app(cdk.App())


def test_no_stage_creates_a_ci_deploy_path(tmp_path: Path) -> None:
    """Deploys are local (ADR-38): no stack may create a GitHub OIDC provider
    or a role that a workflow could assume."""
    for stage in ("dev", "prod"):
        out = tmp_path / stage
        artifacts = set(manifest(stage, out)["artifacts"])
        assert f"{stage}-pipeline" not in artifacts
        templates = "".join(p.read_text(encoding="utf-8") for p in out.glob("*.template.json"))
        assert "token.actions.githubusercontent.com" not in templates
        assert "OpenIdConnectProvider" not in templates
