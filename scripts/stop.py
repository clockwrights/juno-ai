"""Pause and remove the Telegram webhook, so nothing inbound is accepted (ADR-27).

uv run python scripts/stop.py --stage prod --reason "going away for a week"
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
    resolve_stage,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stage", required=True)
    parser.add_argument("--region", default=None)
    parser.add_argument("--account", default=None)
    parser.add_argument("--reason", default="stopped by the owner")

    args = parser.parse_args(argv)

    stage = resolve_stage(args.stage, args.account, args.region)
    session = AdminSession(stage)
    try:
        result = actions.stop(session, stage, reason=args.reason)
    except StageRefusalError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(result.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
