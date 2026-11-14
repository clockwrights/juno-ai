"""Backup and restore round-trip (ADR-27, docs/11 VP1 acceptance).

The guarantee being tested is narrow and important: what comes out of a restore
is what went into the backup, a truncated backup fails rather than restoring
half of itself, and a restored stage never wakes up on its own.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ops.admin import AdminSession, Stage
from ops.backup import (
    BackupRefusedError,
    backup,
    check_destination,
    restore,
    verify,
)
from tests.unit.conftest import BUCKET, STAGE

pytestmark = pytest.mark.usefixtures("aws")


@pytest.fixture
def stage() -> Stage:
    return Stage(name=STAGE, account="000000000000", region="eu-central-1")


@pytest.fixture
def session(aws: dict[str, Any], stage: Stage) -> AdminSession:
    admin = AdminSession(stage, assume=False)
    # Point the cached clients at the moto-backed ones the fixtures created.
    admin._clients.update({"dynamodb": aws["dynamodb"], "s3": aws["s3"]})
    return admin


def seed(aws: dict[str, Any]) -> None:
    aws["dynamodb"].put_item(
        TableName=f"{STAGE}-agent_memory",
        Item={
            "PK": {"S": "MEM#01J"},
            "SK": {"S": "META"},
            "type": {"S": "user_fact"},
            "content": {"S": "the owner lives in Springfield"},
            "importance": {"N": "4"},
            "created_at": {"S": "2026-09-06T10:00:00.000Z"},
        },
    )
    aws["dynamodb"].put_item(
        TableName=f"{STAGE}-agent_diary",
        Item={
            "PK": {"S": "DIARY"},
            "SK": {"S": "2026-09-06T10:00:00.000Z#inv1"},
            "content": {"S": "a quiet cycle"},
        },
    )
    aws["s3"].put_object(Bucket=BUCKET, Key="profile/profile.md", Body=b"# Owner")
    aws["s3"].put_object(Bucket=BUCKET, Key="drafts/notes.md", Body=b"draft text")


def test_backup_writes_a_manifest_with_matching_counts(
    session: AdminSession, stage: Stage, aws: Any, tmp_path: Path
) -> None:
    seed(aws)
    result = backup(session, stage, out=tmp_path / "b")
    manifest = json.loads((result.directory / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "1"
    assert manifest["tables"]["agent_memory"] == 1
    assert manifest["tables"]["agent_diary"] == 1
    assert verify(result.directory) == []


def test_backup_captures_s3_objects(
    session: AdminSession, stage: Stage, aws: Any, tmp_path: Path
) -> None:
    seed(aws)
    result = backup(session, stage, out=tmp_path / "b")
    assert (result.directory / "s3" / "profile" / "profile.md").read_bytes() == b"# Owner"
    assert (result.directory / "s3" / "drafts" / "notes.md").read_bytes() == b"draft text"


def test_sessions_are_excluded_unless_asked_for(
    session: AdminSession, stage: Stage, aws: Any, tmp_path: Path
) -> None:
    """Session records are large and expire on their own; a backup is not an archive."""
    aws["s3"].put_object(Bucket=BUCKET, Key="sessions/2026-09-06/t1/0000.json", Body=b"{}")
    plain = backup(session, stage, out=tmp_path / "plain")
    assert not (plain.directory / "s3" / "sessions").exists()

    with_sessions = backup(session, stage, out=tmp_path / "full", include_sessions=True)
    assert (with_sessions.directory / "s3" / "sessions").exists()


def test_securestring_values_are_absent_by_default(
    session: AdminSession, stage: Stage, aws: Any, tmp_path: Path
) -> None:
    result = backup(session, stage, out=tmp_path / "b")
    assert not (result.directory / "secrets.json").exists()
    params = json.loads((result.directory / "params.json").read_text(encoding="utf-8"))
    assert all(p.get("value") is None or p.get("type") != "SecureString" for p in params)


def test_a_truncated_backup_fails_verification(
    session: AdminSession, stage: Stage, aws: Any, tmp_path: Path
) -> None:
    """Better to refuse than to restore half a memory and call it done."""
    seed(aws)
    result = backup(session, stage, out=tmp_path / "b")
    path = result.directory / "dynamodb" / "agent_memory.jsonl"
    path.write_text(path.read_text(encoding="utf-8")[:40], encoding="utf-8")
    problems = verify(result.directory)
    assert problems
    assert "agent_memory" in problems[0]


def test_a_backup_with_a_missing_file_fails_verification(
    session: AdminSession, stage: Stage, aws: Any, tmp_path: Path
) -> None:
    seed(aws)
    result = backup(session, stage, out=tmp_path / "b")
    (result.directory / "dynamodb" / "agent_diary.jsonl").unlink()
    assert any("agent_diary" in p for p in verify(result.directory))


def test_restore_round_trips_every_item(
    session: AdminSession, stage: Stage, aws: Any, tmp_path: Path
) -> None:
    seed(aws)
    result = backup(session, stage, out=tmp_path / "b")

    aws["dynamodb"].delete_item(
        TableName=f"{STAGE}-agent_memory", Key={"PK": {"S": "MEM#01J"}, "SK": {"S": "META"}}
    )
    assert not aws["dynamodb"].scan(TableName=f"{STAGE}-agent_memory").get("Items")

    # The other tables still hold their seed data, so this is an overwrite; the
    # empty-target guard is tested on its own below.
    restore(session, stage, result.directory, overwrite=True)
    items = aws["dynamodb"].scan(TableName=f"{STAGE}-agent_memory")["Items"]
    assert items[0]["content"]["S"] == "the owner lives in Springfield"


def test_restore_refuses_a_non_empty_target(
    session: AdminSession, stage: Stage, aws: Any, tmp_path: Path
) -> None:
    seed(aws)
    result = backup(session, stage, out=tmp_path / "b")
    with pytest.raises(BackupRefusedError, match="not empty"):
        restore(session, stage, result.directory)


def test_overwrite_allows_a_non_empty_target(
    session: AdminSession, stage: Stage, aws: Any, tmp_path: Path
) -> None:
    seed(aws)
    result = backup(session, stage, out=tmp_path / "b")
    restore(session, stage, result.directory, overwrite=True)


def test_a_restored_stage_comes_up_paused(
    session: AdminSession, stage: Stage, aws: Any, tmp_path: Path
) -> None:
    """Waking a restored agent is a decision, never a side effect of restoring."""
    seed(aws)
    result = backup(session, stage, out=tmp_path / "b")
    restore(session, stage, result.directory, overwrite=True)

    pause = aws["dynamodb"].get_item(
        TableName=f"{STAGE}-agent_state",
        Key={"PK": {"S": "PAUSE"}, "SK": {"S": "current"}},
    )["Item"]
    assert pause["paused"]["BOOL"] is True

    flag = aws["dynamodb"].get_item(
        TableName=f"{STAGE}-agent_state",
        Key={"PK": {"S": "FLAG"}, "SK": {"S": "heartbeat_enabled"}},
    )["Item"]
    assert flag["enabled"]["BOOL"] is False


def test_a_dry_run_changes_nothing(
    session: AdminSession, stage: Stage, aws: Any, tmp_path: Path
) -> None:
    seed(aws)
    result = backup(session, stage, out=tmp_path / "b")
    aws["dynamodb"].delete_item(
        TableName=f"{STAGE}-agent_memory", Key={"PK": {"S": "MEM#01J"}, "SK": {"S": "META"}}
    )
    restore(session, stage, result.directory, dry_run=True, overwrite=True)
    assert not aws["dynamodb"].scan(TableName=f"{STAGE}-agent_memory").get("Items")


def test_restoring_an_unsupported_version_is_refused(
    session: AdminSession, stage: Stage, aws: Any, tmp_path: Path
) -> None:
    """The manifest is a contract; a future format must not be half-read."""
    seed(aws)
    result = backup(session, stage, out=tmp_path / "b")
    manifest_path = result.directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = "99"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BackupRefusedError, match="schema_version"):
        restore(session, stage, result.directory, overwrite=True)


@pytest.mark.parametrize(
    "folder", ["/home/me/Dropbox/backups", "C:/Users/me/OneDrive/agent", "/x/Google Drive/y"]
)
def test_a_synced_folder_is_refused(folder: str) -> None:
    """A backup holds the owner's memory in the clear."""
    with pytest.raises(BackupRefusedError, match="synced"):
        check_destination(Path(folder))


def test_a_synced_folder_is_allowed_with_i_know() -> None:
    check_destination(Path("/home/me/Dropbox/backups"), i_know=True)
