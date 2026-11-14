"""No secret ever reaches a Lambda environment variable (docs/12 §3).

Secrets are SSM SecureString parameters read at cold start (ADR-21). An
environment variable would put them in the console, in `GetFunctionConfiguration`
for anyone with read access, and in CloudFormation drift output.
"""

from __future__ import annotations

import re
from typing import Any

from infra.tests.conftest import ours, resources

#: Shapes that look like credentials regardless of the variable's name.
SECRET_VALUE_PATTERNS = (
    re.compile(r"^[A-Za-z0-9_\-]{32,}$"),  # bare API keys
    re.compile(r"^\d{6,}:[A-Za-z0-9_\-]{30,}$"),  # telegram bot tokens
    re.compile(r"^(gh[pousr]|github_pat)_"),  # github tokens
    re.compile(r"^sk-"),  # openai-style keys
)

SECRET_NAME_HINTS = ("key", "token", "secret", "password", "credential")

#: Names allowed to contain a hint word because they hold an identifier, not a value.
ALLOWED_NAMES = {"WEBHOOK_SECRET_PARAM", "PARAM_EPOCH"}


def _env_vars(built: dict[str, Any]) -> list[tuple[str, str, str]]:
    out = []
    for _stack, _lid, fn in ours(resources(built, "AWS::Lambda::Function")):
        name = fn["Properties"].get("FunctionName", "<generated>")
        env = fn["Properties"].get("Environment", {}).get("Variables", {})
        for key, value in env.items():
            out.append((str(name), key, value if isinstance(value, str) else ""))
    return out


def test_no_environment_variable_looks_like_a_secret(dev: dict[str, Any]) -> None:
    for fn, key, value in _env_vars(dev):
        if not value:
            continue
        for pattern in SECRET_VALUE_PATTERNS:
            assert not pattern.match(value), f"{fn}.{key} looks like a credential"


def test_no_environment_variable_is_named_like_a_secret(dev: dict[str, Any]) -> None:
    for fn, key, _value in _env_vars(dev):
        if key in ALLOWED_NAMES:
            continue
        lowered = key.lower()
        for hint in SECRET_NAME_HINTS:
            assert hint not in lowered, f"{fn}.{key} is named like a secret"


def test_only_listed_roles_may_read_each_parameter(dev: dict[str, Any]) -> None:
    """docs/09 §5 fixes the reader set for every parameter."""
    allowed = {
        "together-api-key": {"gate-model", "gate-canary"},
        "telegram-agent-bot": {"gate-telegram", "gate-dispatcher", "gate-notify"},
        "telegram-ops-bot": {"ops-alarms", "supervisor", "gate-proposal"},
        "github-proposal-token": {"gate-proposal"},
        "webhook-secret": {"gate-telegram"},
    }
    role_logical_to_name = {
        lid: r["Properties"]["RoleName"]
        for _s, lid, r in resources(dev, "AWS::IAM::Role")
        if isinstance(r["Properties"].get("RoleName"), str)
    }
    for _stack, _lid, policy in resources(dev, "AWS::IAM::Policy"):
        props = policy["Properties"]
        for statement in props["PolicyDocument"]["Statement"]:
            action = statement.get("Action", [])
            actions = [action] if isinstance(action, str) else action
            if not any(str(a).startswith("ssm:GetParameter") for a in actions):
                continue
            rendered = str(statement.get("Resource"))
            for param, readers in allowed.items():
                if param not in rendered:
                    continue
                for ref in props.get("Roles", []):
                    name = role_logical_to_name.get(ref.get("Ref", ""), "")
                    short = name.removeprefix("dev-").removesuffix("-role")
                    assert short in readers, f"{short} may read {param}; only {readers} may"


def test_agent_roles_hold_no_ssm_permission_at_all(dev: dict[str, Any]) -> None:
    """ADR-03: the agent holds no secrets, so it needs no way to fetch one."""
    role_logical_to_name = {
        lid: r["Properties"]["RoleName"]
        for _s, lid, r in resources(dev, "AWS::IAM::Role")
        if isinstance(r["Properties"].get("RoleName"), str)
    }
    for _stack, _lid, policy in resources(dev, "AWS::IAM::Policy"):
        props = policy["Properties"]
        roles = {
            role_logical_to_name.get(ref.get("Ref", ""), "")
            .removeprefix("dev-")
            .removesuffix("-role")
            for ref in props.get("Roles", [])
        }
        if not roles & {"agent-conversation", "agent-heartbeat"}:
            continue
        for statement in props["PolicyDocument"]["Statement"]:
            action = statement.get("Action", [])
            actions = [action] if isinstance(action, str) else action
            assert not any(str(a).startswith("ssm:") for a in actions), roles
