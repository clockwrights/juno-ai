"""Reading SSM parameters at cold start.

Secrets are SecureString parameters, never environment variables (ADR-21), so
they do not appear in the console, in `GetFunctionConfiguration`, or in
CloudFormation drift output. Cached per process because a Lambda that fetches
its token on every invocation is slower and noisier for no benefit; the cache
is cleared by bouncing the function (`scripts/bounce.py`), which is what the
rotation runbook does.
"""

from __future__ import annotations

import functools
from typing import Any

_client: Any = None


def _ssm() -> Any:
    global _client
    if _client is None:
        import boto3

        _client = boto3.client("ssm")
    return _client


@functools.lru_cache(maxsize=16)
def get_parameter(name: str, *, decrypt: bool = True) -> str:
    response = _ssm().get_parameter(Name=name, WithDecryption=decrypt)
    return str(response["Parameter"]["Value"])


def clear_cache() -> None:
    """Used by tests, and after a rotation within one warm process."""
    get_parameter.cache_clear()
