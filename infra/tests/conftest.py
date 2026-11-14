"""Synthesize once per session and hand the templates to the assertion tests.

Synthesis is slow enough that doing it per test would make the suite unpleasant
to run, and every test here is a pure read of the result.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from infra.stacks.agent import AgentStack  # noqa: E402
from infra.stacks.foundation import FoundationStack  # noqa: E402
from infra.stacks.gates import GatesStack  # noqa: E402
from infra.stacks.sandbox import SandboxStack  # noqa: E402
from infra.stacks.storage import StorageStack  # noqa: E402
from infra.stacks.supervisor import SupervisorStack  # noqa: E402

ACCOUNT = "111122223333"
REGION = "eu-central-1"

#: CDK creates these itself, e.g. for `auto_delete_objects`.
#: They run only during a deploy and cost nothing, but they are not ours, so
#: every count assertion excludes them by logical id.
CDK_MANAGED_PREFIXES = ("Custom", "LogRetention")


def _build(stage: str) -> dict[str, Any]:
    app = cdk.App(context={"stage": stage})
    env = cdk.Environment(account=ACCOUNT, region=REGION)
    is_prod = stage == "prod"
    common: dict[str, Any] = {"env": env, "stage": stage, "is_prod": is_prod}

    foundation = FoundationStack(app, f"{stage}-foundation", **common)
    storage = StorageStack(app, f"{stage}-storage", foundation=foundation, **common)
    sandbox = SandboxStack(app, f"{stage}-sandbox", foundation=foundation, **common)
    gates = GatesStack(app, f"{stage}-gates", foundation=foundation, storage=storage, **common)
    agent = AgentStack(
        app,
        f"{stage}-agent",
        foundation=foundation,
        storage=storage,
        gates=gates,
        sandbox=sandbox,
        **common,
    )
    supervisor = SupervisorStack(
        app,
        f"{stage}-supervisor",
        foundation=foundation,
        storage=storage,
        gates=gates,
        agent=agent,
        **common,
    )

    stacks = {
        "foundation": foundation,
        "storage": storage,
        "sandbox": sandbox,
        "gates": gates,
        "agent": agent,
        "supervisor": supervisor,
    }
    return {
        "app": app,
        "stacks": stacks,
        "templates": {name: Template.from_stack(s) for name, s in stacks.items()},
    }


@pytest.fixture(scope="session")
def dev() -> dict[str, Any]:
    return _build("dev")


@pytest.fixture(scope="session")
def prod() -> dict[str, Any]:
    return _build("prod")


def resources(built: dict[str, Any], resource_type: str) -> list[tuple[str, str, dict[str, Any]]]:
    """Every resource of a type across all stacks, as (stack, logical_id, resource)."""
    found = []
    for stack_name, template in built["templates"].items():
        for logical_id, body in template.find_resources(resource_type).items():
            found.append((stack_name, logical_id, body))
    return found


def ours(entries: list[tuple[str, str, dict[str, Any]]]) -> list[tuple[str, str, dict[str, Any]]]:
    """Drop CDK's own custom-resource providers from a resource listing."""
    return [e for e in entries if not e[1].startswith(CDK_MANAGED_PREFIXES)]
