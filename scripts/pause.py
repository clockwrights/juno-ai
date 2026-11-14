"""Stop the agent: rules disabled, concurrency zero, PAUSE set (docs/14 §1).

uv run python scripts/pause.py --stage prod --reason "investigating diary drift"
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
    parser.add_argument("--reason", required=True)

    args = parser.parse_args(argv)

    stage = resolve_stage(args.stage, args.account, args.region)
    session = AdminSession(stage)
    try:
        result = actions.pause(session, stage, reason=args.reason)
    except StageRefusalError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(result.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
