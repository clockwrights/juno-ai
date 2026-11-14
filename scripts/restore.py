"""Restore a backup into a fresh stage (ADR-27, docs/14 §11.1).

Deploy the target stage first with `make stage-up`. The restored stage always
comes up paused with `heartbeat_enabled = false`; waking it is a decision.

    uv run python scripts/restore.py --stage dev --from backups/prod/2026-...
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ops.admin import AdminSession, resolve_stage  # noqa: E402
from ops.backup import BackupRefusedError, restore  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stage", required=True)
    parser.add_argument("--from", dest="source", required=True, type=Path)
    parser.add_argument("--region", default=None)
    parser.add_argument("--account", default=None)
    parser.add_argument("--overwrite", action="store_true", help="allow a non-empty target")
    parser.add_argument("--only", choices=["dynamodb", "s3", "vectors"], default=None)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    stage = resolve_stage(args.stage, args.account, args.region)
    session = AdminSession(stage)
    try:
        for line in restore(
            session,
            stage,
            args.source,
            overwrite=args.overwrite,
            only=args.only,
            dry_run=args.dry_run,
        ):
            print(f"  {line}")
    except BackupRefusedError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
