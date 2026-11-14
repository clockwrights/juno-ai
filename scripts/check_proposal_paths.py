"""Refuse a proposal branch that touches a path the agent may not change.

This is the CI half of the three-way enforcement in docs/08 §2: `gate-proposal`
rejects the path before a branch exists, this refuses it on the PR, and branch
protection with CODEOWNERS refuses it at merge. Each is independent, because a
single check is a single thing to get wrong.

    uv run python scripts/check_proposal_paths.py --base origin/main
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from fnmatch import fnmatch

#: docs/08 §2. Everything not listed here is refused.
ALLOWED = ("agent/*", "core/*", "sandbox/*", "tests/unit/*", "tests/canary/fixtures/*")

#: Changing these needs a CODEOWNER approval every time, no flag can waive it.
ALWAYS_HUMAN = ("sandbox/*",)


def changed_paths(base: str) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def is_allowed(path: str) -> bool:
    return any(fnmatch(path, pattern) for pattern in ALLOWED)


def check(paths: list[str]) -> tuple[list[str], list[str]]:
    """Return (refused, needs_codeowner)."""
    refused = [p for p in paths if not is_allowed(p)]
    human = [p for p in paths if any(fnmatch(p, pat) for pat in ALWAYS_HUMAN)]
    return refused, human


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="origin/main")
    args = parser.parse_args(argv)

    paths = changed_paths(args.base)
    refused, human = check(paths)

    for path in paths:
        marker = "REFUSED" if path in refused else "ok"
        print(f"  {marker:<8} {path}")

    if human:
        print("\nThese require a CODEOWNER approval regardless of any flag:")
        for path in human:
            print(f"  {path}")

    if refused:
        print(
            "\nA proposal may only change agent/, core/, sandbox/, tests/unit/ "
            "and tests/canary/fixtures/ (docs/08 §2).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
