"""What a proposal may touch (docs/08 §2).

The first of the three independent enforcements. The other two are
`scripts/check_proposal_paths.py` in CI and GitHub branch protection with
CODEOWNERS. Each is independent because a single check is a single thing to get
wrong, and this one is the only one that runs before a branch even exists.

Note what is *not* here: `gates/`, `infra/`, `contracts/`, `invariants/`,
`supervisor/`, `scripts/`, `ops/`, `panel/`, `.github/` and `docs/`. Every
guardrail in the system lives in one of those, which is the entire point.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch

ALLOWED_PATTERNS: tuple[str, ...] = (
    "agent/*",
    "core/*",
    "sandbox/*",
    "tests/unit/*",
    "tests/canary/fixtures/*",
)

#: No flag can ever auto-merge these (docs/08 §3.3): the sandbox is where
#: agent-written code actually executes.
ALWAYS_HUMAN_PATTERNS: tuple[str, ...] = ("sandbox/*",)

#: Prompts-only changes are the one category `auto_merge_prompts` can cover.
PROMPT_PATTERNS: tuple[str, ...] = ("agent/prompts/*",)

MAX_DIFF_BYTES = 200 * 1024


@dataclass
class PathVerdict:
    allowed: bool
    refused_paths: tuple[str, ...] = ()
    needs_codeowner: bool = False
    prompts_only: bool = False
    reason: str = ""


def is_allowed(path: str) -> bool:
    clean = path.lstrip("./")
    return any(fnmatch(clean, pattern) for pattern in ALLOWED_PATTERNS)


def check_paths(paths: list[str]) -> PathVerdict:
    if not paths:
        return PathVerdict(allowed=False, reason="the diff touches no files")

    refused = tuple(p for p in paths if not is_allowed(p))
    if refused:
        return PathVerdict(
            allowed=False,
            refused_paths=refused,
            reason=f"outside the allowlist: {', '.join(refused)}",
        )

    needs_codeowner = any(
        fnmatch(p.lstrip("./"), pattern) for p in paths for pattern in ALWAYS_HUMAN_PATTERNS
    )
    prompts_only = all(
        any(fnmatch(p.lstrip("./"), pattern) for pattern in PROMPT_PATTERNS) for p in paths
    )
    return PathVerdict(allowed=True, needs_codeowner=needs_codeowner, prompts_only=prompts_only)


def check_diff(diff: str) -> PathVerdict:
    """Parse a unified diff and rule on it (docs/08 §3.1 steps 1 and 2)."""
    if len(diff.encode("utf-8")) > MAX_DIFF_BYTES:
        return PathVerdict(allowed=False, reason=f"the diff is over {MAX_DIFF_BYTES} bytes")

    if _looks_binary(diff):
        return PathVerdict(allowed=False, reason="binary files are not accepted")

    paths = paths_in_diff(diff)
    escaping = tuple(p for p in paths if _escapes_repo(p))
    if escaping:
        return PathVerdict(
            allowed=False,
            refused_paths=escaping,
            reason=f"paths outside the repository root: {', '.join(escaping)}",
        )
    return check_paths(paths)


def paths_in_diff(diff: str) -> list[str]:
    paths: set[str] = set()
    for line in diff.splitlines():
        if line.startswith(("+++ ", "--- ")):
            target = line[4:].strip().split("\t")[0]
            if target in ("/dev/null", ""):
                continue
            paths.add(target.removeprefix("a/").removeprefix("b/"))
    return sorted(paths)


def _escapes_repo(path: str) -> bool:
    """`..` or an absolute path would write outside the checkout."""
    clean = path.replace("\\", "/")
    return clean.startswith("/") or ".." in clean.split("/")


def _looks_binary(diff: str) -> bool:
    markers = ("GIT binary patch", "Binary files ", "\x00")
    return any(marker in diff for marker in markers)
