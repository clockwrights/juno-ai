"""`sandbox-executor` — runs agent-written code with nothing to steal.

The isolation is structural, not defensive: the function sits in a VPC with no
NAT, no internet gateway and no VPC endpoints, so there is no route anywhere,
and its role holds exactly two log actions, so there are no credentials worth
having. This handler adds the process-level part: a fresh `/tmp`, a scrubbed
environment, a hard timeout, memory limits and truncated output.

It is agent-editable (docs/02 §1), but every change needs a CODEOWNER approval
that no flag can waive (docs/08 §3.3).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

WORK_DIR = Path("/tmp")
SCRIPT = WORK_DIR / "main.py"

STDOUT_LIMIT = 64 * 1024
STDERR_LIMIT = 16 * 1024
DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 60
ADDRESS_SPACE_BYTES = 400 * 1024 * 1024

#: Everything the Lambda runtime puts in the environment. Removed before the
#: subprocess starts: there are no credentials to find, but leaving the runtime's
#: metadata endpoints and function name visible teaches agent-written code
#: things it has no reason to know.
_SCRUB_PREFIXES = ("AWS_", "LAMBDA_", "_X_AMZN", "AMZN_")


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    trace_id = str(event.get("trace_id", ""))
    code = event.get("code")
    if not isinstance(code, str) or not code.strip():
        return _response(trace_id, stderr="no code was supplied", exit_code=2)

    language = event.get("language", "python")
    if language != "python":
        return _response(trace_id, stderr=f"unsupported language {language!r}", exit_code=2)

    timeout = min(int(event.get("timeout_s") or DEFAULT_TIMEOUT), MAX_TIMEOUT)
    stdin = event.get("stdin") or ""

    _wipe_tmp()
    SCRIPT.write_text(code, encoding="utf-8")

    started = time.monotonic()
    killed = False
    try:
        completed = subprocess.run(
            [sys.executable, "-I", str(SCRIPT)],
            input=stdin.encode("utf-8"),
            capture_output=True,
            timeout=timeout,
            cwd=str(WORK_DIR),
            env=_clean_environment(),
            preexec_fn=_limits if os.name != "nt" else None,
            check=False,
        )
        stdout = completed.stdout
        stderr = completed.stderr
        exit_code = completed.returncode
    except subprocess.TimeoutExpired as expired:
        killed = True
        stdout = expired.stdout or b""
        stderr = expired.stderr or b""
        exit_code = 124
    except Exception as exc:
        return _response(trace_id, stderr=f"executor failed: {exc}", exit_code=1)

    duration_ms = int((time.monotonic() - started) * 1000)
    out, out_truncated = _truncate(stdout, STDOUT_LIMIT)
    err, err_truncated = _truncate(stderr, STDERR_LIMIT)

    # Nothing survives between invocations, so one run cannot stage something
    # for the next.
    _wipe_tmp()

    return _response(
        trace_id,
        stdout=out,
        stderr=err,
        exit_code=exit_code,
        duration_ms=duration_ms,
        truncated=out_truncated or err_truncated,
        killed=killed,
    )


def _response(
    trace_id: str,
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    duration_ms: int = 0,
    truncated: bool = False,
    killed: bool = False,
) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "trace_id": trace_id,
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "truncated": truncated,
        "killed": killed,
    }


def _wipe_tmp() -> None:
    """Clear `/tmp` on entry and exit.

    Lambda reuses execution environments, so without this a file written by one
    invocation would be readable by the next — which for agent-written code is a
    channel the design does not intend to exist.
    """
    for entry in WORK_DIR.iterdir():
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        except OSError:
            continue


def _clean_environment() -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not any(k.startswith(prefix) for prefix in _SCRUB_PREFIXES)
    }
    env.pop("STAGE", None)
    # A predictable minimum, so code that needs a PATH or a HOME still runs.
    env.setdefault("HOME", str(WORK_DIR))
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _limits() -> None:  # pragma: no cover - POSIX only, exercised in integration
    """Cap address space and disable core dumps in the child.

    `resource` exists only on POSIX, and Lambda is Linux; the import is local so
    the module still loads for the unit tests on other platforms.
    """
    import resource  # type: ignore[import-not-found,unused-ignore]

    resource.setrlimit(resource.RLIMIT_AS, (ADDRESS_SPACE_BYTES, ADDRESS_SPACE_BYTES))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _truncate(raw: bytes, limit: int) -> tuple[str, bool]:
    text = raw.decode("utf-8", errors="replace")
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n... truncated at {limit} characters", True
