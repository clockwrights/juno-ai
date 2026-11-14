"""Write the stage's SSM parameters, without a secret ever leaving this machine.

    uv run python scripts/params.py init  --stage prod
    uv run python scripts/params.py show  --stage prod
    uv run python scripts/params.py set   --stage prod github-proposal-token

`init` reads `TOGETHER_API_KEY` from the environment, prompts for the bot token
without echoing it, asks Telegram for the owner's chat id, generates the webhook
secret, and writes all seven parameters (docs/14 §7). It is idempotent: values
already set are kept unless `--overwrite` is given.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ops import params  # noqa: E402
from ops.admin import AdminSession, StageRefusalError, resolve_stage  # noqa: E402

TOGETHER_ENV = "TOGETHER_API_KEY"


def _prompt_secret(label: str, *, optional: bool = False) -> str:
    """Read a secret from the terminal, never from a command line argument.

    An argument would land in the shell history and in the process list, which
    is exactly the exposure this whole module exists to avoid.
    """
    suffix = " (blank to skip)" if optional else ""
    value = getpass.getpass(f"{label}{suffix}: ").strip()
    if not value and not optional:
        raise SystemExit(f"refused: {label} is required")
    return value


def cmd_init(args: argparse.Namespace, session: AdminSession, stage: object) -> int:
    together = os.environ.get(TOGETHER_ENV, "").strip()
    if together:
        print(f"{TOGETHER_ENV}: found in the environment ({len(together)} chars)")
    else:
        print(f"{TOGETHER_ENV} is not set in this shell.")
        together = _prompt_secret("Together API key")

    bot_token = _prompt_secret("Telegram bot token")
    github = "" if args.no_github else _prompt_secret("GitHub token for VP8", optional=True)

    result = params.init(
        session,
        stage,  # type: ignore[arg-type]
        together_key=together,
        bot_token=bot_token,
        chat_id=args.chat_id or "",
        github_token=github,
        overwrite=args.overwrite,
    )
    print(result.render())
    print("\nBounce the readers if the stage is already deployed: scripts/bounce.py")
    return 0


def cmd_set(args: argparse.Namespace, session: AdminSession, stage: object) -> int:
    parameter = params.BY_NAME.get(args.name)
    if parameter is None:
        print(f"unknown parameter {args.name!r}", file=sys.stderr)
        return 2
    if parameter.secure:
        value = _prompt_secret(parameter.name)
    else:
        value = (args.value or input(f"{parameter.name}: ")).strip()
    outcome = params.put(session, stage, args.name, value)  # type: ignore[arg-type]
    print(f"{args.name}: {outcome} ({params.describe(args.name, value)})")
    return 0


def cmd_show(args: argparse.Namespace, session: AdminSession, stage: object) -> int:
    print(params.show(session, stage).render())  # type: ignore[arg-type]
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stage", required=True)
    parser.add_argument("--region", default=None)
    parser.add_argument("--account", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="write every parameter")
    init.add_argument("--chat-id", default="", help="skip the Telegram lookup")
    init.add_argument("--overwrite", action="store_true", help="replace values already set")
    init.add_argument("--no-github", action="store_true", help="do not ask for the VP8 token")
    init.set_defaults(run=cmd_init)

    one = sub.add_parser("set", help="write one parameter")
    one.add_argument("name")
    one.add_argument("--value", default="", help="non-secret parameters only")
    one.set_defaults(run=cmd_set)

    show = sub.add_parser("show", help="what is set, without revealing it")
    show.set_defaults(run=cmd_show)

    args = parser.parse_args(argv)
    stage = resolve_stage(args.stage, args.account, args.region)
    try:
        session = AdminSession(stage)
        return int(args.run(args, session, stage))
    except (StageRefusalError, params.ParameterError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
