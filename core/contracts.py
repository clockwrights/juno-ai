"""Schema registry and validation for every boundary in the system.

`contracts/` is frozen (docs/03). This module is the only way runtime code
reaches those files: it loads them once per process, validates payloads on both
sides of every boundary, and stamps the common envelope so no component invents
its own.

Validation is deliberately strict and deliberately *returned*, not raised, at
the outer edges — gates answer with a contract error rather than a stack trace
(docs/03 §2.3).
"""

from __future__ import annotations

import datetime as dt
import functools
import json
from pathlib import Path
from typing import Any, Final

import jsonschema
from ulid import ULID

from core.clock import SYSTEM_CLOCK, Clock

CONTRACTS_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "contracts"
SCHEMA_VERSION: Final[str] = "1"


class ContractError(ValueError):
    """A payload did not match its schema."""

    def __init__(self, schema_name: str, message: str) -> None:
        super().__init__(f"{schema_name}: {message}")
        self.schema_name = schema_name
        self.message = message


@functools.cache
def load_schema(name: str) -> dict[str, Any]:
    """Load `contracts/<name>.schema.json`, or `contracts/tools/<tool>.schema.json`.

    Tool schemas are addressed as `tools/<name>`.
    """
    path = CONTRACTS_DIR / f"{name}.schema.json"
    if not path.is_file():
        raise FileNotFoundError(f"no contract named {name!r} at {path}")
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


@functools.cache
def _validator(name: str) -> jsonschema.protocols.Validator:
    schema = load_schema(name)
    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)
    return cls(schema)


@functools.cache
def _tool_validator(tool: str, side: str) -> jsonschema.protocols.Validator:
    schema = load_schema(f"tools/{tool}")
    sub = schema["$defs"][side]
    # Keep $defs reachable so a sub-schema may reference its siblings.
    sub = {**sub, "$defs": schema["$defs"]}
    cls = jsonschema.validators.validator_for(sub)
    return cls(sub)


def validate(name: str, payload: Any) -> None:
    """Raise ContractError if `payload` does not satisfy `contracts/<name>`."""
    errors = sorted(_validator(name).iter_errors(payload), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        location = "/".join(str(p) for p in first.path) or "<root>"
        raise ContractError(name, f"{location}: {first.message}")


def is_valid(name: str, payload: Any) -> bool:
    try:
        validate(name, payload)
    except ContractError:
        return False
    return True


def validate_tool_input(tool: str, payload: Any) -> None:
    _raise_first(f"tools/{tool}#input", _tool_validator(tool, "input"), payload)


def validate_tool_output(tool: str, payload: Any) -> None:
    _raise_first(f"tools/{tool}#output", _tool_validator(tool, "output"), payload)


def _raise_first(name: str, validator: jsonschema.protocols.Validator, payload: Any) -> None:
    errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        location = "/".join(str(p) for p in first.path) or "<root>"
        raise ContractError(name, f"{location}: {first.message}")


def new_trace_id() -> str:
    """A fresh trace id. Only the first gate in a flow calls this (docs/03 §1)."""
    return str(ULID())


def envelope(
    stage: str, trace_id: str | None = None, clock: Clock = SYSTEM_CLOCK
) -> dict[str, Any]:
    """The common envelope every payload and event detail carries."""
    return {
        "schema_version": SCHEMA_VERSION,
        "trace_id": trace_id or new_trace_id(),
        "stage": stage,
        "issued_at": clock.now().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }


def parse_iso(value: str) -> dt.datetime:
    """Parse an ISO 8601 timestamp, accepting the trailing Z the contracts use."""
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def schema_names() -> list[str]:
    """Every top-level contract name, for the contract test suite."""
    return sorted(p.name.removesuffix(".schema.json") for p in CONTRACTS_DIR.glob("*.schema.json"))


def tool_names() -> list[str]:
    """Every tool contract name."""
    return sorted(
        p.name.removesuffix(".schema.json") for p in (CONTRACTS_DIR / "tools").glob("*.schema.json")
    )


@functools.lru_cache(maxsize=1)
def state_keys() -> dict[str, Any]:
    """The frozen `agent_state` key map (docs/03 §6)."""
    loaded: dict[str, Any] = json.loads(
        (CONTRACTS_DIR / "state-keys.json").read_text(encoding="utf-8")
    )
    return loaded


@functools.lru_cache(maxsize=1)
def canary_assertions() -> dict[str, Any]:
    """The canary assertion list (docs/08 §4)."""
    loaded: dict[str, Any] = json.loads(
        (CONTRACTS_DIR / "canary-assertions.json").read_text(encoding="utf-8")
    )
    return loaded


def flag_defaults() -> dict[str, bool]:
    defaults: dict[str, bool] = state_keys()["flag_defaults"]
    return dict(defaults)
