"""Full offline export of a stage (ADR-27, docs/14 §11.1).

uv run python scripts/backup.py --stage prod --verify
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ops.admin import AdminSession, resolve_stage  # noqa: E402
from ops.backup import BackupRefusedError, backup, verify  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stage", required=True)
    parser.add_argument("--region", default=None)
    parser.add_argument("--account", default=None)
    parser.add_argument("--out", default=None, type=Path)
    parser.add_argument("--include-sessions", action="store_true")
    parser.add_argument(
        "--include-secrets",
        action="store_true",
        help="write SecureString values in plaintext; needs --i-know",
    )
    parser.add_argument("--i-know", action="store_true")
    parser.add_argument("--verify", action="store_true", help="re-read and check the result")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.include_secrets and not args.i_know:
        print("refused: --include-secrets needs --i-know", file=sys.stderr)
        return 2

    stage = resolve_stage(args.stage, args.account, args.region)
    session = AdminSession(stage)

    try:
        result = backup(
            session,
            stage,
            out=args.out,
            include_sessions=args.include_sessions,
            include_secrets=args.include_secrets,
            i_know=args.i_know,
        )
    except BackupRefusedError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    print(f"backup written to {result.directory}")
    for table, count in sorted(result.items.items()):
        print(f"  {table:<22} {count}")
    print(f"  {'s3 objects':<22} {result.objects}")
    print(f"  {'vectors':<22} {result.vectors}")

    if args.verify:
        problems = verify(result.directory)
        if problems:
            for problem in problems:
                print(f"  PROBLEM {problem}", file=sys.stderr)
            return 1
        print(
            f"OK {result.total_items} items / {result.objects} objects / {result.vectors} vectors"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
