"""Every invariant clause appears verbatim in the prompts it applies to.

This is the check that makes prompt self-modification safe to allow at all: a
proposal may reword anything except these clauses, and CI plus a Lambda import
assertion both refuse a prompt that has lost one (docs/08 §5).

VP0 has no prompts yet, so the suite validates the invariant files themselves and
skips the prompt check until VP4 creates `agent/prompts/`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INVARIANTS_DIR = ROOT / "invariants"
PROMPTS_DIR = ROOT / "agent" / "prompts"

FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)

EXPECTED_IDS = {
    "budget-awareness",
    "notify-criterion",
    "staging-rule",
    "do-nothing-is-fine",
    "no-infra-tampering",
    "diary-always",
    "memory-is-yours",
}


def parse(path: Path) -> tuple[dict[str, str], str]:
    """Split an invariant file into its frontmatter and its clause text."""
    match = FRONTMATTER.match(path.read_text(encoding="utf-8"))
    if not match:
        raise AssertionError(f"{path.name} has no frontmatter block")
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta, match.group(2).strip()


def invariant_files() -> list[Path]:
    return sorted(INVARIANTS_DIR.glob("*.md"))


def test_the_expected_clauses_exist() -> None:
    ids = {parse(p)[0]["id"] for p in invariant_files()}
    assert ids == EXPECTED_IDS


@pytest.mark.parametrize("path", invariant_files(), ids=lambda p: p.stem)
def test_invariant_is_well_formed(path: Path) -> None:
    meta, clause = parse(path)
    assert meta["id"] == path.stem, "the id must match the filename"
    assert meta["mode"] == "verbatim"
    assert meta["applies_to"].startswith("["), "applies_to must be a list"
    assert clause, "the clause body is empty"
    assert "\n\n" not in clause, "a clause is one paragraph so it can be matched verbatim"


@pytest.mark.parametrize("path", invariant_files(), ids=lambda p: p.stem)
def test_clause_appears_in_every_prompt_it_applies_to(path: Path) -> None:
    if not PROMPTS_DIR.is_dir():
        pytest.skip("agent/prompts/ arrives in VP4")
    meta, clause = parse(path)
    targets = [t.strip() for t in meta["applies_to"].strip("[]").split(",") if t.strip()]
    for target in targets:
        prompt = PROMPTS_DIR / target
        assert prompt.is_file(), f"{meta['id']} applies to a missing prompt {target}"
        assert clause in prompt.read_text(encoding="utf-8"), (
            f"{meta['id']} is not verbatim in {target}"
        )
