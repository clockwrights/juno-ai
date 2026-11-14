"""Assert that a stage really is gone (ADR-27).

`cdk destroy` reporting success is not the same as the account being clean:
retained resources survive on purpose, and a partial sweep is easy to miss. This
walks the account for anything carrying the stage prefix and exits non-zero if
it finds something, which is what makes "nothing expensive was left behind" a
checkable claim rather than a hope.

    uv run python scripts/verify_destroyed.py --stage test-123
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ops.admin import Stage, resolve_stage  # noqa: E402
from ops.survey import survey_stage  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stage", required=True)
    parser.add_argument("--region", default=None)
    parser.add_argument("--account", default=None)
    parser.add_argument(
        "--expect",
        choices=["gone", "present"],
        default="gone",
        help="'present' inverts the check, for confirming a deploy landed",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    stage: Stage = resolve_stage(args.stage, args.account, args.region)
    found = survey_stage(stage)

    for kind, names in sorted(found.items()):
        for name in names:
            print(f"  {kind:<22} {name}")

    total = sum(len(v) for v in found.values())
    if args.expect == "gone":
        if total:
            print(
                f"\n{total} resource(s) still carry the {stage.name!r} prefix. "
                "Run scripts/destroy.py again, or delete them by hand.",
                file=sys.stderr,
            )
            return 1
        print(f"stage {stage.name!r} is gone: nothing left in the account.")
        return 0

    if not total:
        print(f"\nnothing found for stage {stage.name!r}", file=sys.stderr)
        return 1
    print(f"\nstage {stage.name!r} is present: {total} resource(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
