"""Read and set feature flags (docs/14 §3).

uv run python scripts/flags.py --stage prod list
uv run python scripts/flags.py --stage prod set proposals_enabled true
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ops import actions  # noqa: E402
from ops.admin import AdminSession, StageRefusalError, resolve_stage  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stage", required=True)
    parser.add_argument("--region", default=None)
    parser.add_argument("--account", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    setter = sub.add_parser("set")
    setter.add_argument("name")
    setter.add_argument("value", choices=["true", "false"])
    args = parser.parse_args(argv)

    stage = resolve_stage(args.stage, args.account, args.region)
    session = AdminSession(stage)

    try:
        if args.command == "list":
            for name, enabled in sorted(actions.list_flags(session, stage).items()):
                print(f"  {name:<22} {enabled}")
            return 0
        result = actions.set_flag(session, stage, args.name, args.value == "true")
    except StageRefusalError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(result.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
