"""Loading prompts, with the invariant check at import time.

If a deployed prompt has lost an invariant clause, importing the handler raises
and the Lambda fails to start. That is the third place the clause is enforced —
after CI and before the canary — and it is the one that makes a bad deploy fail
loudly rather than run with a weakened prompt (docs/08 §5).
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
INVARIANTS_DIR = Path(__file__).resolve().parent.parent.parent / "invariants"

_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


class InvariantMissingError(RuntimeError):
    """A deployed prompt has lost a clause that must be present verbatim."""


@functools.cache
def load_prompt(name: str) -> str:
    path = PROMPTS_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"no prompt named {name!r}")
    return path.read_text(encoding="utf-8")


@functools.cache
def _clauses() -> list[tuple[str, list[str], str]]:
    """(id, applies_to, clause) for every invariant."""
    out = []
    for path in sorted(INVARIANTS_DIR.glob("*.md")):
        match = _FRONTMATTER.match(path.read_text(encoding="utf-8"))
        if not match:
            continue
        meta = {}
        for line in match.group(1).splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                meta[key.strip()] = value.strip()
        targets = [
            t.strip() for t in meta.get("applies_to", "").strip("[]").split(",") if t.strip()
        ]
        out.append((meta.get("id", path.stem), targets, match.group(2).strip()))
    return out


def assert_invariants(name: str) -> None:
    """Raise unless every clause that applies to `name` is present verbatim."""
    text = load_prompt(name)
    missing = [cid for cid, targets, clause in _clauses() if name in targets and clause not in text]
    if missing:
        raise InvariantMissingError(
            f"{name} is missing invariant clause(s): {', '.join(sorted(missing))}"
        )


def system_prompt(name: str) -> str:
    """Load a prompt and refuse to return one that has lost an invariant."""
    assert_invariants(name)
    return load_prompt(name)


def tool_descriptions() -> dict[str, str]:
    """Split `tools.md` into per-tool descriptions.

    The prompt is written for a reader, so the parser is forgiving: a tool with
    no section simply gets an empty description rather than failing the deploy.
    """
    text = load_prompt("tools.md")
    descriptions: dict[str, str] = {}
    for block in re.split(r"\n(?=\*\*)", text):
        match = re.match(r"\*\*([a-z_ /]+)\*\*\s*[—-]\s*(.*)", block.strip(), re.DOTALL)
        if not match:
            continue
        body = " ".join(match.group(2).split())
        for name in match.group(1).split("/"):
            descriptions[name.strip()] = body
    return descriptions
