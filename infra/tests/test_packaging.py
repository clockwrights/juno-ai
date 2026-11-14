"""Lambda packaging (ADR-26).

A wrong `Handler` string or a missing module is invisible until the function is
invoked in a deployed stage, where it appears as `Runtime.ImportModuleError` on
the one path nobody was watching. These tests make it a synthesis failure.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from infra.stacks import common
from infra.stacks.common import BUILD_DIR, HANDLERS, RESERVED_CONCURRENCY
from infra.tests.conftest import ours, resources

#: Deliberately still on the placeholder: no handler module has been written.
#: Written down so that finishing one of them without wiring it up fails here.
UNWRITTEN = {"gate-proposal", "gate-canary", "ops-alarms"}


def test_every_function_is_either_wired_or_a_known_gap() -> None:
    assert set(RESERVED_CONCURRENCY) == set(HANDLERS) | UNWRITTEN


@pytest.mark.parametrize("short_name", sorted(HANDLERS))
def test_the_handler_string_names_a_real_callable(short_name: str) -> None:
    """The failure this prevents is a typo that deploys and then cannot start."""
    dotted = HANDLERS[short_name]
    if short_name == "sandbox-executor":
        # Flattened into its own asset, so the module path in the table is not
        # importable from the repository root; the file is.
        dotted = "sandbox.handler.handler"
    module_path, _, attribute = dotted.rpartition(".")
    module = importlib.import_module(module_path)
    assert callable(getattr(module, attribute))


def test_the_sandbox_handler_imports_nothing_first_party() -> None:
    """Its asset holds one file (ADR-26); an import of `core` would break it."""
    source = (Path(common.ROOT) / "sandbox" / "handler.py").read_text(encoding="utf-8")
    for package in ("core", "agent", "gates", "supervisor"):
        assert f"from {package}" not in source
        assert f"import {package}" not in source


def test_the_built_asset_is_used_when_it_exists(dev: dict[str, Any]) -> None:
    if not (BUILD_DIR / "app").is_dir():
        pytest.skip("no build; run `make package`")
    for _stack, logical_id, body in ours(resources(dev, "AWS::Lambda::Function")):
        name = str(body["Properties"]["FunctionName"]).removeprefix("dev-")
        expected = HANDLERS.get(name, "handler.handler")
        assert body["Properties"]["Handler"] == expected, logical_id


def test_a_deploy_without_a_build_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """`require_real_code` is what stops `make stage-up` shipping placeholders."""
    monkeypatch.setattr(common, "BUILD_DIR", Path("no", "such", "build"))
    app = cdk.App(context={"stage": "dev", "require_real_code": "1"})
    with pytest.raises(RuntimeError, match="make package"):
        from infra.stacks.foundation import FoundationStack
        from infra.stacks.storage import StorageStack

        env = cdk.Environment(account="111122223333", region="eu-central-1")
        kw: dict[str, Any] = {"env": env, "stage": "dev", "is_prod": False}
        foundation = FoundationStack(app, "dev-foundation", **kw)
        storage = StorageStack(app, "dev-storage", foundation=foundation, **kw)
        from infra.stacks.gates import GatesStack

        Template.from_stack(
            GatesStack(app, "dev-gates", foundation=foundation, storage=storage, **kw)
        )
