"""Restart a paused agent. Only a human can do this (ADR-09).

uv run python scripts/resume.py --stage prod [--ack <journal sk>]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ops import actions  # noqa: E402
from ops.admin import (  # noqa: E402
    AdminSession,
    StageRefusalError,
    confirm_stage_name,
    resolve_stage,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stage", required=True)
    parser.add_argument("--region", default=None)
    parser.add_argument("--account", default=None)
    parser.add_argument("--ack", default=None, help="acknowledge a Level 2 decision")
    parser.add_argument("--yes", default=None, help="type the stage name to skip the prompt")

    args = parser.parse_args(argv)

    stage = resolve_stage(args.stage, args.account, args.region)
    session = AdminSession(stage)
    try:
        confirm_stage_name(stage, args.yes, prompt=f"About to resume stage {stage.name!r}.")
        result = actions.resume(session, stage, ack=args.ack)
    except StageRefusalError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(result.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
