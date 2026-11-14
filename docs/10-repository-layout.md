# 10 — Repository Layout

One repository, one language, one CDK app. Directory ownership is the security boundary for self-modification, so it is enforced three ways: the `gate-proposal` allowlist, the CI path check, and GitHub branch protection with CODEOWNERS.

## Top-level directories

| Path | Layer | Contents | Agent may propose changes |
|---|---|---|---|
| `agent/` | 1 | Conversation and heartbeat handlers, runtime loop, prompts, tool implementations | Yes |
| `core/` | 1 | Shared library: storage, model client, tool registry, contracts types | Yes |
| `sandbox/` | 3 | `sandbox-executor` handler and its pinned packages | Yes, CODEOWNER approval always |
| `supervisor/` | 2 | Supervisor handler, rubric and prompt | No |
| `gates/` | 3 | One package per gate (`telegram`, `dispatcher`, `governor`, `notify`, `model`, `proposal`, `canary`) | No |
| `infra/` | 3 | CDK app (`app.py`), stacks, IAM allowlists and the infrastructure assertions in `infra/tests/` | No |
| `contracts/` | 3 | JSON Schemas for every boundary, canary assertions, state keys | No |
| `invariants/` | 3 | One file per prompt clause that must stay verbatim | No |
| `ops/` | 3 | Admin library shared by `scripts/` and the panel: readers, actions, params, backup, lifecycle | No |
| `scripts/` | 3 | Operator commands: deploy, params, pause/resume, stop/start, backup/restore, destroy, contract generators | No |
| `panel/` | 3 | Local control panel ([15-control-panel.md](15-control-panel.md)) | No |
| `tests/` | — | Unit, contract and invariant tests (`tests/unit/**` may accompany a proposal) | With the code it tests |
| `docs/` | — | This design | No |
| `.github/` | 3 | CI workflow and CODEOWNERS | No |

Root files: `pyproject.toml` and `uv.lock` (Python toolchain, one lockfile), `package.json` (pins the CDK CLI so local synth and CI agree), `Makefile` (`make ci` runs what CI runs), `cdk.json`, `.env.example` (per-deployment settings, copied to the gitignored `.env`).

The allowlist itself is `gates/proposal/allowlist.py`; [08-self-modification.md](08-self-modification.md) §2 has the exact rules.

## CODEOWNERS

```
/contracts/    @<your-handle>
/invariants/   @<your-handle>
/infra/        @<your-handle>
/gates/        @<your-handle>
/supervisor/   @<your-handle>
/scripts/      @<your-handle>
/ops/          @<your-handle>
/panel/        @<your-handle>
/.github/      @<your-handle>
/sandbox/      @<your-handle>
/docs/         @<your-handle>
```

The handle is set in `.github/CODEOWNERS`. That account must have write access to the repository for these entries to be honoured: CODEOWNERS silently ignores a user who cannot approve.

Branch protection on `main`: require PR, require CI status checks, require CODEOWNER review for the paths above, dismiss stale approvals on new commits, no force push, no direct push (the proposal token cannot push to `main`).

## Tooling

- Python 3.12, `uv` for dependency management, single lockfile.
- `ruff` (lint + format), `mypy --strict`.
- `pytest` with markers `unit` (default), `contract`, `integration`, `live` (uses real model, opt-in).
- `moto` for AWS fakes; `InMemoryIndex` for vectors; `FakeModelClient` with cassette fixtures for the model.
- CDK v2 in Python. `cdk synth` runs in CI on every PR.
- Pre-commit hooks: ruff, mypy, schema validation, `datamodel-codegen` staleness check.
