"""Deploy a stage from this machine (ADR-38). `make stage-up` runs this.

    uv run python scripts/deploy.py --stage prod
    uv run python scripts/deploy.py --stage test-1 --skip-prompts

There is no CI deploy: whoever cloned the repository deploys it into their own
account. What a pipeline used to guarantee is checked here instead:

1. `prod` is deployed only from a clean `main`, so what runs is what was merged.
   Any other stage may deploy a branch — that is what an ephemeral stage is for.
2. The Lambda assets are built, and `require_real_code` turns a missing build
   into a synthesis error rather than a deployed placeholder (ADR-26).
3. Reserved concurrency is switched off when the account's Lambda quota is too
   low to reserve anything, with a warning every time (ADR-34).
4. After the deploy, `agent/prompts/` is copied to
   `s3://<stage>-agent-<account>/prompts/deployed/<sha>/`, which is what the
   supervisor and the panel diff against (docs/14 §2).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ops.admin import AdminSession, Stage, resolve_stage  # noqa: E402
from scripts.destroy import cdk_cli  # noqa: E402

PROMPTS = ROOT / "agent" / "prompts"
#: ADR-34: reserving per-function ceilings needs this many concurrent executions.
QUOTA_NEEDED = 30


def deploy_refusal(stage: str, branch: str, dirty: bool) -> str | None:
    """Why this checkout may not deploy `stage`, or None when it may."""
    if stage != "prod":
        return None
    if branch != "main":
        return f"prod deploys only from main; this checkout is on {branch!r}"
    if dirty:
        return "prod deploys only from a clean tree; commit or stash first"
    return None


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()


def concurrency_flags(stage: Stage) -> list[str]:
    session = AdminSession(stage, assume=False)
    quota = int(
        session.client("lambda").get_account_settings()["AccountLimit"]["ConcurrentExecutions"]
    )
    if quota < QUOTA_NEEDED:
        print(
            f"WARNING: Lambda concurrency quota is {quota}; {QUOTA_NEEDED} is needed to reserve. "
            "Deploying without per-function ceilings (ADR-34)."
        )
        return ["-c", "reserved_concurrency=off"]
    return []


def publish_prompts(stage: Stage, sha: str) -> int:
    if not PROMPTS.is_dir():
        return 0
    s3 = AdminSession(stage).client("s3")
    count = 0
    for path in sorted(p for p in PROMPTS.rglob("*") if p.is_file()):
        key = f"prompts/deployed/{sha}/{path.relative_to(PROMPTS).as_posix()}"
        s3.put_object(Bucket=stage.bucket, Key=key, Body=path.read_bytes())
        count += 1
    return count


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stage", required=True)
    parser.add_argument("--region", default=None)
    parser.add_argument("--skip-prompts", action="store_true", help="do not copy prompts to S3")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    dirty = bool(git("status", "--porcelain"))
    refusal = deploy_refusal(args.stage, branch, dirty)
    if refusal:
        print(f"refusing: {refusal}")
        return 2
    sha = git("rev-parse", "HEAD") + ("-dirty" if dirty else "")

    stage = resolve_stage(args.stage, region=args.region)
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_lambdas.py")], cwd=ROOT, check=True
    )

    command = [
        cdk_cli(),
        "deploy",
        "--all",
        "-c",
        f"stage={stage.name}",
        "-c",
        "require_real_code=1",
    ]
    # The account is deliberately not passed: infra/app.py takes it from `.env`,
    # and CDK then refuses credentials that belong to a different account.
    command += ["-c", f"region={stage.region}"]
    command += concurrency_flags(stage)
    command += ["--require-approval", "never"]
    subprocess.run(command, cwd=ROOT, check=True)

    if not args.skip_prompts:
        print(f"published {publish_prompts(stage, sha)} prompt file(s) for {sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
