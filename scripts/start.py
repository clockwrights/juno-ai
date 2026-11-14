"""Reverse `stop.py`, webhook included (ADR-27).

uv run python scripts/start.py --stage prod --webhook-url https://...
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
    parser.add_argument("--ack", default=None)
    parser.add_argument("--webhook-url", default="")
    parser.add_argument("--yes", default=None)

    args = parser.parse_args(argv)

    stage = resolve_stage(args.stage, args.account, args.region)
    session = AdminSession(stage)
    try:
        confirm_stage_name(stage, args.yes, prompt=f"About to start stage {stage.name!r}.")
        result = actions.start(session, stage, ack=args.ack, webhook_url=args.webhook_url)
    except StageRefusalError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(result.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
