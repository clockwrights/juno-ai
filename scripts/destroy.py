"""Remove a stage entirely (ADR-27, docs/14 §11.3).

    uv run python scripts/destroy.py --stage dev --dry-run
    uv run python scripts/destroy.py --stage prod --i-know

Order: stop, back up, `cdk destroy` in reverse dependency order, then sweep the
resources CloudFormation deliberately retained. Each step is skipped if already
done, so an interrupted run can simply be repeated.

Steps 1 and 2 are `stop.py` and `backup.py`, run as subprocesses so that the
one command really is the whole sequence. A backup that is checked for and never
taken is worse than no backup: it looks like protection.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ops.admin import (  # noqa: E402
    AdminSession,
    StageRefusalError,
    confirm_stage_name,
    guard_prod,
    resolve_stage,
)
from ops.lifecycle import plan_from_templates, sweep, sweep_orphan_log_groups  # noqa: E402

CDK_OUT = ROOT / "cdk.out"


def cdk_cli() -> str:
    """The pinned CDK CLI, in a form Windows can actually execute.

    `node_modules/.bin/cdk` is a POSIX shell shim; on Windows the runnable file
    is `cdk.cmd` next to it, and passing the shim to `subprocess` fails with
    "%1 is not a valid Win32 application" (WinError 193). Preferring the local
    install over any global one also keeps this on the version pinned in
    `package.json`, which is the whole point of pinning it.
    """
    local = ROOT / "node_modules" / ".bin"
    for candidate in ("cdk.cmd", "cdk.exe", "cdk"):
        path = local / candidate
        if path.exists():
            return str(path)
    found = shutil.which("cdk")
    if not found:
        raise SystemExit("no cdk CLI found; run `npm ci`")
    return found


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stage", required=True)
    parser.add_argument("--region", default=None)
    parser.add_argument("--account", default=None)
    parser.add_argument("--i-know", action="store_true", help="required for prod")
    parser.add_argument("--skip-backup", action="store_true", help="refused for prod")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, delete nothing")
    parser.add_argument("--yes", default=None, help="type the stage name to skip the prompt")
    return parser.parse_args(argv)


def synth(stage: str) -> None:
    """Synthesize so the plan reflects the app as it is now, not a stale cdk.out."""
    subprocess.run([cdk_cli(), "synth", "-c", f"stage={stage}", "-q"], cwd=ROOT, check=True)


def run_step(script: str, args: list[str]) -> int:
    """Run one lifecycle script in-process order, streaming its output."""
    print(f"\n--- {script} {' '.join(args)}")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script), *args], cwd=ROOT, check=False
    )
    return result.returncode


def cdk_destroy(stage: str) -> None:
    subprocess.run(
        [cdk_cli(), "destroy", "--all", "-c", f"stage={stage}", "--force"], cwd=ROOT, check=False
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    stage = resolve_stage(args.stage, args.account, args.region)

    try:
        guard_prod(stage, i_know=args.i_know, action="destroy")
        if stage.is_prod and args.skip_backup:
            raise StageRefusalError("--skip-backup is refused for prod")
    except StageRefusalError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    synth(stage.name)
    plan = plan_from_templates(stage, CDK_OUT)
    print(plan.render())

    if args.dry_run:
        print("\ndry run: nothing was deleted")
        return 0

    try:
        confirm_stage_name(
            stage,
            args.yes,
            prompt=f"\nThis permanently deletes stage {stage.name!r} and everything above.",
        )
    except StageRefusalError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    # Steps 1 and 2 of docs/14 §11.3, actually run. Stop first so nothing is
    # writing while the backup reads; back up second so the export is of a
    # quiet stage.
    common = ["--stage", stage.name]
    if args.region:
        common += ["--region", args.region]
    if args.account:
        common += ["--account", args.account]

    if run_step("stop.py", [*common, "--reason", "destroy"]) != 0:
        # A stage that cannot be stopped may still be taking work, and the
        # backup would be of a moving target.
        print("refused: stop failed; nothing has been deleted", file=sys.stderr)
        return 2

    if not args.skip_backup:
        backup_args = [*common, "--verify"]
        if stage.is_prod:
            backup_args.append("--i-know")
        if run_step("backup.py", backup_args) != 0:
            print("refused: backup failed; nothing has been deleted", file=sys.stderr)
            return 2

    cdk_destroy(stage.name)

    # The sweep runs as the caller, not as the stage's admin role (ADR-25's one
    # exception, recorded in ADR-35): `cdk destroy` has just deleted that role,
    # and AWS invalidates a deleted role's temporary credentials immediately —
    # so even a session opened beforehand fails with UnrecognizedClientException.
    # Whoever can assume the admin role can already do this directly.
    session = AdminSession(stage, assume=False)

    report = sweep(session, plan, dry_run=False)
    # After the declared resources, the ones AWS made for us.
    report += sweep_orphan_log_groups(session, stage.name, dry_run=False)
    for line in report:
        print(line)

    failures = [line for line in report if line.startswith(("FAILED", "SKIPPED"))]
    if failures:
        # Saying "destroyed" over a half-swept stage is how a bill survives a
        # teardown. Re-running this script picks up exactly what is left.
        print(
            f"\n{len(failures)} retained resource(s) NOT removed from stage "
            f"{stage.name!r}. Re-run this command; it resumes.",
            file=sys.stderr,
        )
        return 1

    print(f"\nstage {stage.name!r} destroyed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
