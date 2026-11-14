"""Reserved concurrency (docs/06 §8).

Every function has an explicit ceiling, so no failure mode — a retry storm, a
loop in agent code, a queue backing up — can fan the account out.
"""

from __future__ import annotations

from typing import Any

from infra.stacks.common import RESERVED_CONCURRENCY
from infra.tests.conftest import ours, resources


def test_every_function_has_a_reserved_concurrency(dev: dict[str, Any]) -> None:
    for _stack, logical_id, fn in ours(resources(dev, "AWS::Lambda::Function")):
        assert "ReservedConcurrentExecutions" in fn["Properties"], logical_id


def test_reserved_concurrency_matches_the_table(dev: dict[str, Any]) -> None:
    for _stack, _lid, fn in ours(resources(dev, "AWS::Lambda::Function")):
        name = str(fn["Properties"]["FunctionName"]).removeprefix("dev-")
        assert fn["Properties"]["ReservedConcurrentExecutions"] == RESERVED_CONCURRENCY[name], name


def test_no_gate_exceeds_two(dev: dict[str, Any]) -> None:
    """gate-model is allowed four; every other gate is capped at two."""
    for name, limit in RESERVED_CONCURRENCY.items():
        if name.startswith("gate-") and name != "gate-model":
            assert limit <= 2, f"{name} is {limit}"


def test_the_heartbeat_can_only_run_once(dev: dict[str, Any]) -> None:
    """Two concurrent cycles would double-spend the token budget and the diary."""
    assert RESERVED_CONCURRENCY["agent-heartbeat"] == 1


def test_the_reservations_fit_an_account_quota_of_thirty() -> None:
    """AWS keeps 10 unreserved, always, so the quota must exceed the sum by 10.

    The first deploy failed here: a fresh account's quota is 10, which leaves
    nothing to reserve. Recorded as a number so that adding a function makes the
    required quota visible rather than surprising.
    """
    from infra.stacks.common import RESERVED_CONCURRENCY

    assert sum(RESERVED_CONCURRENCY.values()) + 10 <= 30


def test_reserved_concurrency_is_only_dropped_when_explicitly_asked() -> None:
    """The escape hatch exists for a quota of 10; it must not be the default."""
    import aws_cdk as cdk

    from infra.stacks.common import reserved_concurrency

    default = cdk.App(context={"stage": "dev"})
    assert reserved_concurrency(cdk.Stack(default, "s"), "gate-model") == 4

    opted_out = cdk.App(context={"stage": "dev", "reserved_concurrency": "off"})
    assert reserved_concurrency(cdk.Stack(opted_out, "s"), "gate-model") is None
