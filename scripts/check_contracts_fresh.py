"""Fail if `contracts/` no longer matches its generators.

The generators exist so the common envelope is identical in every schema
(docs/03 §1). If someone edits a schema by hand the two drift silently, and the
next generator run would quietly revert their change — so the check runs on
every commit that touches either side.

    uv run python scripts/check_contracts_fresh.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONTRACTS = ROOT / "contracts"


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        backup = Path(tmp) / "contracts"
        shutil.copytree(CONTRACTS, backup)

        for generator in ("gen_contracts.py", "gen_tool_contracts.py"):
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / generator)],
                capture_output=True,
                text=True,
                cwd=ROOT,
                check=False,
            )
            if result.returncode != 0:
                print(f"{generator} failed:\n{result.stderr}", file=sys.stderr)
                shutil.rmtree(CONTRACTS)
                shutil.copytree(backup, CONTRACTS)
                return 1

        drifted = _differences(backup, CONTRACTS)

        # Always restore what was committed; this checks, it does not rewrite.
        shutil.rmtree(CONTRACTS)
        shutil.copytree(backup, CONTRACTS)

    if drifted:
        print(
            "contracts/ has drifted from its generators:\n"
            + "\n".join(f"  {d}" for d in drifted)
            + "\n\nRegenerate with scripts/gen_contracts.py and "
            "scripts/gen_tool_contracts.py, or change the generator rather than "
            "the output. Remember the frozen-contract rule in docs/03.",
            file=sys.stderr,
        )
        return 1
    print("contracts/ matches its generators")
    return 0


def _differences(committed: Path, regenerated: Path) -> list[str]:
    """Compare parsed content, so this reports meaning rather than formatting.

    `filecmp` would compare bytes, which makes the check fail on a line-ending
    difference that git normalises away anyway — a false alarm that would teach
    people to ignore it.
    """

    def index(root: Path) -> dict[str, Any]:
        return {
            path.relative_to(root).as_posix(): json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(root.rglob("*.json"))
        }

    before, after = index(committed), index(regenerated)
    found = [f"{name}: only in the working tree" for name in sorted(before.keys() - after.keys())]
    found += [f"{name}: only after regenerating" for name in sorted(after.keys() - before.keys())]
    found += [
        f"{name}: differs"
        for name in sorted(before.keys() & after.keys())
        if before[name] != after[name]
    ]
    return found


if __name__ == "__main__":
    raise SystemExit(main())
