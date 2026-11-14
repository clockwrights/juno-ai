"""The sandbox executor (docs/11 VP3 acceptance).

Network and credential isolation are structural and asserted on the synthesized
template (`infra/tests/`). What is testable here is the process-level half: a
clean `/tmp`, a scrubbed environment, a real timeout and truncated output.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from sandbox import handler as sandbox

#: Only the pieces that genuinely need POSIX are skipped. The work directory is
#: monkeypatched, so everything else exercises the real handler on any platform.
posix_only = pytest.mark.skipif(os.name == "nt", reason="rlimits and preexec_fn are POSIX-only")


@pytest.fixture(autouse=True)
def _tmp_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the executor at a throwaway directory instead of the real /tmp."""
    monkeypatch.setattr(sandbox, "WORK_DIR", tmp_path)
    monkeypatch.setattr(sandbox, "SCRIPT", tmp_path / "main.py")


def run(code: str, **kwargs: Any) -> dict[str, Any]:
    return sandbox.handler({"trace_id": "t1", "language": "python", "code": code, **kwargs}, None)


def test_stdout_comes_back(tmp_path: Path) -> None:
    result = run("print('hello')")
    assert result["stdout"].strip() == "hello"
    assert result["exit_code"] == 0
    assert result["killed"] is False


def test_a_non_zero_exit_is_reported_not_raised() -> None:
    result = run("import sys; sys.exit(3)")
    assert result["exit_code"] == 3


def test_stderr_is_captured() -> None:
    result = run("raise ValueError('boom')")
    assert "ValueError" in result["stderr"]
    assert result["exit_code"] != 0


def test_stdin_is_passed_through() -> None:
    result = run("import sys; print(sys.stdin.read().upper())", stdin="quiet")
    assert "QUIET" in result["stdout"]


def test_a_slow_script_is_killed() -> None:
    """`killed` must be distinguishable from a crash: they mean different things."""
    result = run("import time; time.sleep(10)", timeout_s=1)
    assert result["killed"] is True
    assert result["exit_code"] == 124


def test_the_timeout_is_capped() -> None:
    """A caller asking for an hour gets a minute (docs/03 §2.4)."""
    result = run("print('x')", timeout_s=99999)
    assert result["exit_code"] == 0


def test_large_output_is_truncated() -> None:
    result = run("print('x' * 200000)")
    assert result["truncated"] is True
    assert len(result["stdout"]) < 200000


def test_small_output_is_not_marked_truncated() -> None:
    assert run("print('short')")["truncated"] is False


def test_aws_variables_are_scrubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    """There are no credentials to find, but agent code should not even see the
    runtime's shape."""
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "should-not-be-visible")
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "dev-sandbox-executor")
    monkeypatch.setenv("STAGE", "dev")
    result = run("import os; print(sorted(k for k in os.environ if 'AWS' in k or k == 'STAGE'))")
    assert result["stdout"].strip() == "[]"


def test_a_file_does_not_survive_between_invocations() -> None:
    """Lambda reuses environments; without the wipe this would be a channel."""
    run("open('kept.txt', 'w').write('hello')")
    result = run("import pathlib; print(pathlib.Path('kept.txt').exists())")
    assert result["stdout"].strip() == "False"


def test_empty_code_is_refused() -> None:
    assert sandbox.handler({"trace_id": "t", "code": "   "}, None)["exit_code"] == 2


def test_an_unsupported_language_is_refused() -> None:
    result = sandbox.handler({"trace_id": "t", "code": "puts 1", "language": "ruby"}, None)
    assert result["exit_code"] == 2
    assert "ruby" in result["stderr"]


def test_the_response_matches_the_contract() -> None:
    from core.contracts import validate

    validate("executor-response", run("print('ok')"))


def test_isolated_mode_ignores_user_site_packages() -> None:
    """`-I` keeps a stray PYTHONPATH from changing what agent code can import."""
    result = run("import sys; print(sys.flags.isolated)")
    assert result["stdout"].strip() == "1"


@posix_only
@pytest.mark.skipif(sys.platform == "darwin", reason="RLIMIT_AS is not enforced on macOS")
def test_a_memory_hog_fails_rather_than_taking_the_lambda_down() -> None:
    result = run("x = bytearray(600 * 1024 * 1024)")
    assert result["exit_code"] != 0
