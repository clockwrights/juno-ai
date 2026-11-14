"""Owner-specific settings that never go in git (ADR-37, ADR-38).

The AWS account and region a clone deploys to belong to whoever cloned it, so
they live in the gitignored `.env` at the repository root (template:
`.env.example`), or in the environment. Both `infra/app.py` and the admin-side
scripts read them through here, so `make`, a bare `npx cdk` and every script
agree on where they are deploying.

Standard library only: `infra/app.py` imports this before anything AWS-shaped.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

ACCOUNT_KEY = "JUNO_AWS_ACCOUNT"
REGION_KEY = "JUNO_AWS_REGION"
DEFAULT_REGION = "eu-central-1"


def _from_env_file(key: str) -> str:
    """`KEY=value` lines only; no interpolation, no export, no quoting rules."""
    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        name, sep, value = line.strip().partition("=")
        if sep and name.strip() == key:
            return value.strip().strip("\"'")
    return ""


def setting(key: str) -> str:
    """The environment wins over `.env`; empty when neither has it."""
    return os.environ.get(key) or _from_env_file(key)


def region() -> str:
    return setting(REGION_KEY) or DEFAULT_REGION
