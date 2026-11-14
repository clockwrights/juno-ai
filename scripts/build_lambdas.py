"""Build the Lambda deployment assets, without Docker (ADR-26).

    uv run python scripts/build_lambdas.py

Produces two directories under `build/lambda/`:

- `app/`     — every first-party handler except the sandbox, plus the third-party
               dependencies. One asset shared by nine functions, which differ
               only in their `Handler` string. Sharing it means CDK hashes and
               uploads the dependency tree once instead of nine times, and it
               makes it impossible for one function to run against a stale copy
               of `core/`.
- `sandbox/` — `sandbox/handler.py` alone. The sandbox executes agent-authored
               code (docs/05); giving it the agent's modules and the provider
               SDK would hand that code a map of the system for nothing, since
               it imports only the standard library.

The layout of `app/` mirrors the repository root, because the runtime resolves
`contracts/`, `agent/prompts/` and `invariants/` by walking up from the module
that needs them (`core/contracts.py`, `agent/runtime/prompts.py`).

Why the dependencies are shipped rather than taken from the runtime: the Lambda
Python 3.12 image bundles a boto3 that predates S3 Vectors, and the memory index
is the one call that would fail — at runtime, in the deployed stage, on the code
path that is hardest to test. Pinning our own copy costs upload time and removes
a class of surprise.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build" / "lambda"
APP = BUILD / "app"
SANDBOX = BUILD / "sandbox"

#: Copied into `app/` verbatim. Data directories are here for the same reason
#: the code is: a schema that is not in the asset is a validation error in
#: production and nowhere else.
APP_TREES = ("core", "agent", "gates", "supervisor", "contracts", "invariants")

#: aarch64, because every function is arm64 (docs/02 §1). Getting this wrong
#: produces an asset that imports fine on this laptop and fails to load
#: `pydantic_core` in the deployed function.
PLATFORM = "aarch64-manylinux2014"
PYTHON_VERSION = "3.12"

#: Never shipped: compiled caches differ between machines and would change the
#: asset hash on every build, and `pip`'s own bookkeeping is not needed at run
#: time.
JUNK = ("__pycache__", "*.pyc", "*.pyo", "*.dist-info/RECORD", "*.dist-info/INSTALLER")


def run(*args: str) -> None:
    print("  $", " ".join(args))
    subprocess.run(args, check=True, cwd=ROOT)


def prune(root: Path) -> None:
    """Drop build noise, so two builds of the same commit hash identically."""
    for path in sorted(root.rglob("__pycache__"), reverse=True):
        shutil.rmtree(path, ignore_errors=True)
    for pattern in ("*.pyc", "*.pyo"):
        for path in root.rglob(pattern):
            path.unlink(missing_ok=True)
    for name in ("RECORD", "INSTALLER", "direct_url.json"):
        for path in root.glob(f"*.dist-info/{name}"):
            path.unlink(missing_ok=True)


def copy_tree(name: str) -> None:
    source = ROOT / name
    if not source.is_dir():
        raise SystemExit(f"missing source tree: {name}")
    shutil.copytree(
        source,
        APP / name,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests"),
    )


def build_app(*, skip_deps: bool) -> None:
    requirements = BUILD / "requirements.txt"
    if not skip_deps:
        # Fresh, because `--target` merges into whatever is already there and a
        # removed dependency would otherwise live on in the asset forever.
        shutil.rmtree(APP, ignore_errors=True)
        APP.mkdir(parents=True)
        BUILD.mkdir(parents=True, exist_ok=True)
        print("dependencies:")
        export = subprocess.run(
            ["uv", "export", "--no-dev", "--no-emit-project", "--format", "requirements-txt"],
            check=True,
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        requirements.write_text(export.stdout, encoding="utf-8", newline="\n")
        run(
            "uv",
            "pip",
            "install",
            "--requirements",
            str(requirements),
            "--target",
            str(APP),
            "--python-platform",
            PLATFORM,
            "--python-version",
            PYTHON_VERSION,
            "--only-binary",
            ":all:",
            "--no-installer-metadata",
            "--quiet",
        )
    else:
        APP.mkdir(parents=True, exist_ok=True)

    print("first-party:")
    for name in APP_TREES:
        print(f"  {name}/")
        copy_tree(name)
    prune(APP)


def build_sandbox() -> None:
    print("sandbox:")
    shutil.rmtree(SANDBOX, ignore_errors=True)
    SANDBOX.mkdir(parents=True)
    # Flattened to `handler.py`, so the sandbox asset has no package structure
    # at all and `Handler` stays `handler.handler`.
    shutil.copy2(ROOT / "sandbox" / "handler.py", SANDBOX / "handler.py")
    print("  handler.py")


def size_mb(root: Path) -> float:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) / 1_000_000


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-deps",
        action="store_true",
        help="refresh only the first-party code in an existing build (fast iteration)",
    )
    args = parser.parse_args()

    build_app(skip_deps=args.skip_deps)
    build_sandbox()

    print(f"\napp     {size_mb(APP):7.1f} MB  {BUILD.relative_to(ROOT)}/app")
    print(f"sandbox {size_mb(SANDBOX):7.1f} MB  {BUILD.relative_to(ROOT)}/sandbox")
    if size_mb(APP) > 250:
        # Lambda's hard limit on the unzipped asset. Failing here is far better
        # than failing in `cdk deploy` after the upload.
        print("\nERROR: the app asset exceeds Lambda's 250 MB unzipped limit", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
